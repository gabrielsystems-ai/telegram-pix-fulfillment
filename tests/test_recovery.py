from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlalchemy.exc import OperationalError

from tests.test_catalog import product_order, setup_catalog
from tests.test_domain import NOW
from tests.test_product_access import bind, settle
from vip.config import Settings
from vip.models import OmegaCharge, Order, Product, RecoveryTask
from vip.recovery import DeliveryError, RecoveryService, opt_in, opt_out, record_start


class Sender:
    def __init__(self):
        self.calls = []
        self.error = None

    def send(self, telegram_id, text):
        self.calls.append((telegram_id, text))
        if self.error:
            raise self.error
        return 77


class RecoveryScenario:
    def __init__(self, db, *, enabled=True, approved=True):
        setup_catalog(db)
        self.db, self.now, self.sender = db, NOW, Sender()
        self.settings = Settings(
            _env_file=None, recovery_enabled=enabled, recovery_promotions_approved=approved
        )
        with db.transaction() as session:
            record_start(session, 123, now=self.now)
            opt_in(session, 123, now=self.now)

    def worker(self):
        return RecoveryService(self.settings, self.db, self.sender, clock=lambda: self.now)

    def task(self):
        with self.db.transaction() as session:
            return session.get(RecoveryTask, 123)


def test_first_start_schedules_once_in_utc_and_does_not_reset(db):
    s = RecoveryScenario(db)
    with db.transaction() as session:
        record_start(session, 123, now=NOW + 600)
    assert s.task().first_started_at == NOW
    assert s.task().due_at == NOW + 1200
    s.now += 1199
    s.worker().sync_once()
    assert s.sender.calls == []
    s.now += 1
    s.worker().sync_once()
    assert len(s.sender.calls) == 1
    assert s.task().state == "sent" and s.task().sent_at == NOW + 1200
    assert s.task().message_id == 77
    s.now += 86400
    s.worker().sync_once()
    assert len(s.sender.calls) == 1


def test_default_flags_disable_scheduling_worker_and_real_promotion(db):
    assert not Settings(_env_file=None).recovery_enabled
    assert not Settings(_env_file=None).recovery_promotions_approved
    s = RecoveryScenario(db, enabled=False)
    s.now += 1200
    s.worker().sync_once()
    assert s.task().state == "pending" and not s.sender.calls


def test_unapproved_promotion_qualifies_without_sending_or_charging(db):
    s = RecoveryScenario(db, approved=False)
    s.now += 1200
    s.worker().sync_once()
    assert s.task().state == "eligible"
    assert s.task().eligible_at == s.now and not s.sender.calls
    with db.transaction() as session:
        assert session.get(Order, "unknown") is None


def test_previous_age_block_does_not_require_resetting_recovery(db):
    s = RecoveryScenario(db, approved=False)
    with db.transaction() as session:
        session.get(RecoveryTask, 123).last_error = "adult_assurance_required"
    s.now += 1200
    s.worker().sync_once()
    assert s.task().state == "eligible" and s.task().last_error is None
    assert s.task().due_at == NOW + 1200 and s.sender.calls == []


@pytest.mark.parametrize(
    "action", ["purchased", "pending", "uncertain", "opt_out", "blocked", "inactive"]
)
def test_ineligible_users_are_never_sent_an_offer(db, action):
    s = RecoveryScenario(db)
    if action in {"purchased", "pending", "uncertain"}:
        oid = product_order(db)
        if action == "purchased":
            bind(db, settle(db, oid))
        elif action == "uncertain":
            with db.transaction() as session:
                session.get(Order, oid).expires_at = NOW + 1
                session.add(
                    OmegaCharge(order_id=oid, identifier=oid, state="uncertain", created_at=NOW)
                )
    with db.transaction() as session:
        if action == "opt_out":
            opt_out(session, 123, now=NOW)
        elif action == "blocked":
            session.get(RecoveryTask, 123).blocked_at = NOW
        elif action == "inactive":
            session.get(Product, "recovery_bundle").active = False
    s.now += 1200
    s.worker().sync_once()
    assert s.sender.calls == []
    if action == "purchased":
        assert s.task().state == "canceled"


def test_no_promotional_consent_means_no_send_but_retains_eligibility(db):
    s = RecoveryScenario(db)
    with db.transaction() as session:
        session.get(RecoveryTask, 123).opted_in_at = None
    s.now += 1200
    s.worker().sync_once()
    assert not s.sender.calls and s.task().state == "eligible"


def test_two_workers_and_restarts_cannot_send_twice(db):
    s = RecoveryScenario(db)
    s.now += 1200
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: s.worker().sync_once(), range(2)))
    s.worker().sync_once()
    assert len(s.sender.calls) == 1


@pytest.mark.parametrize(
    "kind,state", [("blocked", "canceled"), ("ambiguous", "uncertain"), ("retry", "failed")]
)
def test_send_failure_states_survive_restart_without_blind_duplicate(db, kind, state):
    s = RecoveryScenario(db)
    s.sender.error = DeliveryError(kind, retry_after=7200)
    s.now += 1200
    s.worker().sync_once()
    assert s.task().state == state and s.task().sent_at is None
    s.sender.error = None
    s.now += 3600
    s.worker().sync_once()
    assert len(s.sender.calls) == 1
    s.now += 3601
    s.worker().sync_once()
    assert len(s.sender.calls) == (2 if kind == "retry" else 1)


def test_interrupted_send_is_uncertain_and_never_replayed(db):
    s = RecoveryScenario(db)
    with db.transaction() as session:
        task = session.get(RecoveryTask, 123)
        task.state, task.send_attempt_at = "sending", NOW + 1200
    s.now += 1600
    s.worker().sync_once()
    assert s.task().state == "uncertain" and not s.sender.calls


def test_opt_out_is_permanent_across_start_and_opt_in(db):
    s = RecoveryScenario(db)
    with db.transaction() as session:
        opt_out(session, 123, now=NOW)
        record_start(session, 123, now=NOW + 1)
        opt_in(session, 123, now=NOW + 2)
    s.now += 1200
    s.worker().sync_once()
    assert s.task().state == "canceled" and not s.sender.calls


def test_templates_reject_links_and_default_matches_supplied_copy():
    assert "Consulte os produtos e valores" in Settings(_env_file=None).recovery_template
    with pytest.raises(ValueError):
        Settings(_env_file=None, recovery_template="Comprar em https://invalid.example/checkout")


def test_acknowledged_send_with_failed_local_commit_is_never_replayed(db, monkeypatch):
    s = RecoveryScenario(db)
    s.now += 1200
    transaction = db.transaction
    failed = False

    @contextmanager
    def fail_after_send():
        nonlocal failed
        with transaction() as session:
            yield session
            if s.sender.calls and not failed:
                failed = True
                raise OperationalError("synthetic commit failure", None, None)

    monkeypatch.setattr(db, "transaction", fail_after_send)
    with pytest.raises(OperationalError):
        s.worker().sync_once()
    monkeypatch.setattr(db, "transaction", transaction)
    assert s.task().state == "sending" and s.task().sent_at is None
    s.now += 301
    s.worker().sync_once()
    assert s.task().state == "uncertain" and len(s.sender.calls) == 1


def test_confirmed_purchase_between_claim_and_send_cancels_delivery(db, monkeypatch):
    s = RecoveryScenario(db)
    s.now += 1200
    transaction = db.transaction
    purchased = False

    @contextmanager
    def purchase_after_claim():
        nonlocal purchased
        with transaction() as session:
            yield session
            task = session.get(RecoveryTask, 123)
            sending = task and task.state == "sending"
        if sending and not purchased:
            purchased = True
            oid = product_order(db)
            settle(db, oid)

    monkeypatch.setattr(db, "transaction", purchase_after_claim)
    s.worker().sync_once()
    monkeypatch.setattr(db, "transaction", transaction)
    assert purchased and not s.sender.calls and s.task().state == "canceled"


@pytest.mark.parametrize(
    "code,kind",
    [
        ("telegram_forbidden", "blocked"),
        ("telegram_rate_limited", "retry"),
        ("telegram_unavailable", "ambiguous"),
    ],
)
def test_recovery_transport_classifies_errors_without_exposing_payload(code, kind):
    from vip.telegram_gateway import AiogramRecoverySender, GroupError

    class UnavailableGateway:
        def call(self, method):
            raise GroupError(code, retry_after=7200)

    with pytest.raises(DeliveryError) as error:
        AiogramRecoverySender(UnavailableGateway()).send(123, "synthetic message")
    assert error.value.kind == kind
    assert error.value.retry_after == 7200
    assert "synthetic message" not in str(error.value)
