import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from tests.test_omega_pix import buyer, charge_response, configured, transaction_response
from vip.errors import DomainError, GatewayError
from vip.linking import issue_link_token, redeem_link_token
from vip.models import AuditEvent, OmegaCharge, OmegaEvent, Order, Payment, Subscription
from vip.orders import create_order
from vip.payments.omega import OmegaGateway
from vip.payments.operations import OmegaPayments
from vip.plans import create_plan
from vip.subscriptions import memberships
from vip.users import register_user

NOW = 1_800_000_000


class Harness:
    def __init__(self, db):
        self.db = db
        self.now = NOW
        self.post_count = 0
        self.get_count = 0
        self.timeout = False
        self.changes = {}
        self.request_body = None
        gateway = OmegaGateway(configured(), transport=httpx.MockTransport(self.handle))
        self.service = OmegaPayments(db, gateway, clock=lambda: self.now)
        with db.transaction() as session:
            plan = create_plan(
                session,
                name="Synthetic",
                amount_minor=2990,
                duration_days=30,
                telegram_group_id=-100123,
            )
            self.plan_id = plan.id
        self.order_id = self.order()

    def order(self):
        with self.db.transaction() as session:
            return create_order(session, self.plan_id, now=self.now).id

    def handle(self, request):
        if request.method == "POST":
            self.post_count += 1
            self.request_body = json.loads(request.content)
            if self.timeout:
                raise httpx.ReadTimeout("synthetic", request=request)
            return httpx.Response(
                200,
                json=charge_response(
                    transactionId="tx-" + self.request_body["identifier"],
                ),
            )
        self.get_count += 1
        order_id = request.url.params.get("clientIdentifier") or request.url.params["id"][3:]
        return httpx.Response(
            200,
            json=transaction_response(
                id="tx-" + order_id,
                clientIdentifier=order_id,
                **self.changes,
            ),
        )

    def create(self, order_id=None):
        return self.service.create_charge(order_id or self.order_id, buyer())

    def reconcile(self, order_id=None):
        self.now += 61
        return self.service.reconcile(order_id or self.order_id)

    def complete(self):
        self.changes = {
            "status": "COMPLETED",
            "payedAt": datetime.fromtimestamp(self.now, UTC).isoformat(),
        }

    def webhook(self, event="TRANSACTION_PAID", *, token="synthetic-webhook-token", order_id=None):
        # Critical fields match the documented producer payment webhook shape.
        # Optional client/tracking/order information is intentionally ignored by the service.
        body = {
            "event": event,
            "token": token,
            "client": {},
            "transaction": {"id": "tx-" + (order_id or self.order_id)},
        }
        return self.service.process_webhook(json.dumps(body).encode())


@pytest.fixture
def h(db):
    return Harness(db)


def test_creation_persists_reference_and_hash_without_activating_a_payment(h):
    view = h.create()
    assert view["status"] == "pending"
    assert view["eligible_for_linking"] is False
    assert "webhook_token" not in view
    with h.db.transaction() as session:
        assert session.get(Order, h.order_id).provider_transaction_id == "tx-" + h.order_id
        charge = session.get(OmegaCharge, h.order_id)
        assert charge.state == "ready"
        assert charge.webhook_token_hash == hashlib.sha256(b"synthetic-webhook-token").hexdigest()
        assert session.scalar(select(func.count()).select_from(Payment)) == 0


def test_repeated_and_concurrent_creation_never_posts_twice(h):
    def attempt(_):
        try:
            return h.create()
        except DomainError as error:
            assert error.code == "charge_creation_in_progress"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(attempt, range(2)))
    assert h.create()["transaction_id"] == "tx-" + h.order_id
    assert h.post_count == 1


def test_timeout_preserves_reservation_and_reconciles_without_new_post(h):
    h.timeout = True
    with pytest.raises(GatewayError, match="omega_timeout"):
        h.create()
    with h.db.transaction() as session:
        assert session.get(OmegaCharge, h.order_id).state == "uncertain"
    result = h.create()
    assert result["transaction_id"] == "tx-" + h.order_id
    assert h.post_count == 1
    assert h.get_count == 1


@pytest.mark.parametrize(
    "state,want", [("PENDING", "pending"), ("FAILED", "cancelled"), ("EXPIRED", "expired")]
)
def test_nonpaid_queries_never_make_the_order_eligible(h, state, want):
    h.create()
    h.changes = {"status": state}
    result = h.reconcile()
    assert result["status"] == want
    assert result["eligible_for_linking"] is False


def test_completed_payment_becomes_linkable_only_after_authenticated_query(h):
    h.create()
    h.complete()
    assert h.reconcile()["eligible_for_linking"] is True
    with h.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
        assert session.scalar(select(func.count()).select_from(Subscription)) == 0
        raw = issue_link_token(session, h.order_id, ttl_seconds=900, now=h.now)
        register_user(session, 123)
        assert redeem_link_token(session, raw, 123, now=h.now).telegram_id == 123


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": 24.9},
        {"chargeAmount": 24.9},
        {"currency": "USD"},
        {"paymentMethod": "CREDIT_CARD"},
        {"purchaseType": "RECURRING"},
        {"exchangeRate": 2},
        {"producerExtraAmount": 1},
        {"clientIdentifier": "unknown"},
    ],
)
def test_mismatches_do_not_record_any_payment(h, changes):
    h.create()
    h.complete()
    h.changes |= changes
    # Override a provider result without accepting any transaction ID from a client.
    # The handler supports one order identifier; avoid duplicate keyword in its fixture builder.
    if "clientIdentifier" in changes:
        h.changes.pop("clientIdentifier")
        h.service.gateway = OmegaGateway(
            configured(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=transaction_response(
                        id="tx-" + h.order_id, clientIdentifier="unknown", **h.changes
                    ),
                )
            ),
        )
    with pytest.raises(DomainError):
        h.reconcile()
    with h.db.transaction() as session:
        assert session.get(Order, h.order_id).status == "pending"
        assert session.scalar(select(func.count()).select_from(Payment)) == 0


def test_invalid_token_or_unknown_transaction_never_triggers_a_query(h):
    h.create()
    h.complete()
    with pytest.raises(DomainError, match="invalid_webhook_token"):
        h.webhook(token="wrong")
    with pytest.raises(DomainError, match="unknown_webhook_transaction"):
        h.webhook(order_id="unknown")
    assert h.get_count == 0


def test_webhook_is_idempotent_and_disagrees_with_pending_query(h):
    h.create()
    with pytest.raises(DomainError, match="webhook_not_confirmed"):
        h.webhook()
    h.complete()
    assert h.webhook()["duplicate"] is False
    assert h.webhook()["duplicate"] is True
    assert h.get_count == 2
    with h.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1


def test_webhook_and_reconciliation_concurrently_create_only_one_payment(h):
    h.create()
    h.complete()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(h.webhook), pool.submit(h.reconcile)]
        for outcome in outcomes:
            outcome.result()
    with h.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
        assert session.get(Order, h.order_id).status == "paid"


@pytest.mark.parametrize(
    "state,event",
    [("REFUNDED", "TRANSACTION_REFUNDED"), ("CHARGED_BACK", "TRANSACTION_CHARGED_BACK")],
)
def test_refund_and_chargeback_revoke_internal_eligibility_without_telegram_operations(
    h, state, event
):
    h.create()
    h.complete()
    h.reconcile()
    with h.db.transaction() as session:
        raw = issue_link_token(session, h.order_id, ttl_seconds=900, now=h.now)
        register_user(session, 123)
        redeem_link_token(session, raw, 123, now=h.now)
    h.changes = {"status": state, "refundedAt": datetime.fromtimestamp(h.now, UTC).isoformat()}
    h.webhook(event)
    with h.db.transaction() as session:
        assert session.get(Order, h.order_id).status == "cancelled"
        assert memberships(session, 123, now=h.now) == []
        assert session.scalar(select(func.count()).select_from(AuditEvent)) > 0
    # A late paid event cannot undo the final financial state.
    h.complete()
    h.webhook()
    with h.db.transaction() as session:
        assert session.get(Order, h.order_id).status == "cancelled"
        assert session.get(OmegaCharge, h.order_id).financial_status == state


def test_created_event_cannot_approve_even_when_query_already_says_completed(h):
    h.create()
    h.complete()
    h.webhook("TRANSACTION_CREATED")
    with h.db.transaction() as session:
        assert session.get(Order, h.order_id).status == "pending"


def test_expired_local_order_cannot_be_activated_by_a_late_completed_query(h):
    h.create()
    h.now += 1800
    h.complete()
    assert h.reconcile()["eligible_for_linking"] is False


def test_failed_validation_rolls_back_financial_state_and_event_receipts(h):
    h.create()
    h.complete()
    h.changes["amount"] = 0.01
    with pytest.raises(DomainError):
        h.webhook()
    with h.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OmegaEvent)) == 0
        assert session.scalar(select(func.count()).select_from(Payment)) == 0


def test_query_crossing_order_expiry_cannot_use_time_from_before_the_http_request(h):
    h.create()
    h.now = NOW + 1799
    h.complete()
    original = h.handle

    def delayed(request):
        response = original(request)
        h.now = NOW + 1801
        return response

    h.service.gateway = OmegaGateway(configured(), transport=httpx.MockTransport(delayed))
    assert h.service.reconcile(h.order_id)["eligible_for_linking"] is False


@pytest.mark.parametrize("event", [{}, []])
def test_malformed_webhook_discriminator_has_a_controlled_rejection(h, event):
    raw = json.dumps({"event": event, "token": "synthetic", "transaction": {"id": "unknown"}})
    with pytest.raises(DomainError, match="invalid_webhook"):
        h.service.process_webhook(raw.encode())


def test_persistence_failure_after_successful_post_never_allows_a_second_post(h, monkeypatch):
    original = h.db.transaction
    calls = 0

    @contextmanager
    def broken():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OperationalError("synthetic persistence failure", None, None)
        with original() as session:
            yield session

    with monkeypatch.context() as context:
        context.setattr(h.db, "transaction", broken)
        with pytest.raises(OperationalError):
            h.create()
    with pytest.raises(DomainError, match="charge_creation_in_progress"):
        h.create()
    assert h.reconcile()["transaction_id"] == "tx-" + h.order_id
    assert h.post_count == 1


def test_refunding_an_earlier_purchase_removes_its_time_from_remaining_renewals(h):
    second = h.order()
    for order_id in [h.order_id, second]:
        h.create(order_id)
        h.complete()
        h.reconcile(order_id)
        with h.db.transaction() as session:
            raw = issue_link_token(session, order_id, ttl_seconds=900, now=h.now)
            register_user(session, 123)
            redeem_link_token(session, raw, 123, now=h.now)
    h.changes = {"status": "REFUNDED", "refundedAt": datetime.fromtimestamp(h.now, UTC).isoformat()}
    h.webhook("TRANSACTION_REFUNDED")
    with h.db.transaction() as session:
        remaining = memberships(session, 123, now=h.now)
        assert len(remaining) == 1
        assert remaining[0].order_id == second
        assert remaining[0].expires_at == remaining[0].starts_at + 30 * 86400


def test_query_failure_and_unknown_reference_after_timeout_never_repeat_post(h):
    h.timeout = True
    with pytest.raises(GatewayError):
        h.create()
    h.service.gateway = OmegaGateway(
        configured(), transport=httpx.MockTransport(lambda request: httpx.Response(404))
    )
    with pytest.raises(GatewayError, match="omega_resource_not_found"):
        h.create()
    assert h.post_count == 1
    with h.db.transaction() as session:
        assert session.get(Order, h.order_id).status == "pending"
        assert session.get(OmegaCharge, h.order_id).state == "uncertain"
