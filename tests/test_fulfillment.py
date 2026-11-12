from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_catalog import product_order, setup_catalog
from tests.test_domain import NOW
from vip.catalog import OFFERS
from vip.models import Fulfillment, Order, RecoveryTask
from vip.payments.contracts import VerifiedPayment
from vip.payments.service import record_verified_payment


def prepared(db, code="sample_video"):
    setup_catalog(db)
    if code == "recovery_bundle":
        with db.transaction() as s:
            s.add(
                RecoveryTask(
                    telegram_id=123,
                    first_started_at=NOW,
                    due_at=NOW + 1200,
                    next_check_at=NOW + 1200,
                    eligible_at=NOW + 1200,
                    state="eligible",
                )
            )
    oid = product_order(db, code)
    permissions = OFFERS[code][2]
    with db.transaction() as s:
        s.add(
            Fulfillment(
                order_id=oid,
                authenticated_user_id=123,
                verified_at=NOW,
                video_state="pending" if "sample_video_access" in permissions else "not_required",
                vip_state="pending" if "vip_permanent_access" in permissions else "not_required",
            )
        )
    return oid


def confirm(db, oid):
    with db.transaction() as s:
        order = s.get(Order, oid)
        order.provider_transaction_id = "tx-" + oid
        record_verified_payment(
            s,
            VerifiedPayment("event-" + oid, "tx-" + oid, oid, order.amount_minor, "BRL", "paid"),
            now=NOW + 1202,
        )


def test_only_verified_new_purchase_links_automatically_after_payment(db):
    oid = prepared(db)
    with db.transaction() as s:
        assert s.get(Order, oid).linked_telegram_id is None
    confirm(db, oid)
    with db.transaction() as s:
        assert s.get(Order, oid).linked_telegram_id == 123
        assert s.get(Fulfillment, oid).status == "queued"
    legacy = product_order(db, request="legacy-request-001")
    confirm(db, legacy)
    with db.transaction() as s:
        assert s.get(Order, legacy).linked_telegram_id is None
        assert s.get(Fulfillment, legacy) is None


def test_worker_delivers_video_after_payment_once_across_restart(db):
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    oid = prepared(db)
    registered(db)
    sender = Sender()
    worker = FulfillmentService(db, 999, sender, clock=lambda: NOW + 1203)
    worker.run_once()
    assert sender.calls == []
    confirm(db, oid)
    worker.run_once()
    from vip.db import Database

    restarted = Database(str(db.engine.url))
    try:
        FulfillmentService(restarted, 999, sender, clock=lambda: NOW + 1500).run_once()
    finally:
        restarted.engine.dispose()
    assert len(sender.calls) == 1
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == "complete"


class InviteSender:
    def __init__(self, failure=None):
        self.calls, self.failure = [], failure

    def send_invite(self, user_id, url):
        self.calls.append((user_id, url))
        if self.failure:
            raise self.failure


@pytest.mark.parametrize(
    "code,video_count,invite_count",
    [
        ("sample_video", 1, 0),
        ("vip_permanent", 0, 1),
        ("recovery_bundle", 1, 1),
    ],
)
def test_automatic_products_reuse_real_video_and_group_services(
    db, code, video_count, invite_count
):
    from aiogram.methods import ApproveChatJoinRequest

    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    oid = prepared(db, code)
    registered(db)
    confirm(db, oid)
    group = Scenario(db)
    group.now = NOW + 1203
    sender, invites = Sender(), InviteSender()

    def worker():
        return FulfillmentService(
            db, 999, sender, access=group.service, invite_sender=invites, clock=lambda: group.now
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: worker().run_once(), range(2)))
    assert len(sender.calls) == video_count
    assert len(invites.calls) == invite_count
    if invite_count:
        group.service.join_request(group.request(123, invites.calls[0][1]))
        assert group.telegram.count(ApproveChatJoinRequest) == 1
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == "complete"


def test_combo_retries_only_missing_component_after_restart(db):
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    oid = prepared(db, "recovery_bundle")
    registered(db)
    confirm(db, oid)
    video, invites = Sender(), InviteSender()
    FulfillmentService(db, 999, video, clock=lambda: NOW + 1203).run_once()
    with db.transaction() as s:
        job = s.get(Fulfillment, oid)
        assert job.video_state == "sent" and job.vip_state == "retry"
    group = Scenario(db)
    group.now = NOW + 1300
    FulfillmentService(
        db, 999, video, access=group.service, invite_sender=invites, clock=lambda: group.now
    ).run_once()
    assert len(video.calls) == 1 and len(invites.calls) == 1
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == "complete"


@pytest.mark.parametrize("failure,expected", [("rejected", "queued"), ("uncertain", "review")])
def test_video_failure_retry_or_review_survives_restart(db, failure, expected):
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    oid = prepared(db)
    registered(db)
    confirm(db, oid)
    sender = Sender(failure)
    FulfillmentService(db, 999, sender, clock=lambda: NOW + 1203).run_once()
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == expected
    sender.failure = None
    FulfillmentService(db, 999, sender, clock=lambda: NOW + 1300).run_once()
    assert len(sender.calls) == (2 if failure == "rejected" else 1)


@pytest.mark.parametrize("phase", ["before_send", "video_sent", "vip_sending"])
def test_worker_recovers_expired_lease_without_duplicate_external_sends(db, phase):
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService
    from vip.video_delivery import deliver_video

    oid = prepared(db, "recovery_bundle")
    registered(db)
    confirm(db, oid)
    sender, invites = Sender(), InviteSender()
    if phase != "before_send":
        assert deliver_video(db, 999, 123, sender) == "sent"
    with db.transaction() as s:
        job = s.get(Fulfillment, oid)
        job.status, job.lease_owner, job.lease_until = "processing", "dead-process", NOW + 1204
        if phase == "vip_sending":
            job.video_state, job.vip_state = "sent", "sending"
    group = Scenario(db)
    group.now = NOW + 1400
    FulfillmentService(
        db, 999, sender, access=group.service, invite_sender=invites, clock=lambda: group.now
    ).run_once()
    assert len(sender.calls) == 1
    assert len(invites.calls) == (0 if phase == "vip_sending" else 1)
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == (
            "review" if phase == "vip_sending" else "complete"
        )


@pytest.mark.parametrize("code,state", [("rejected", "queued"), ("uncertain", "review")])
def test_invite_notification_retries_only_definite_rejection(db, code, state):
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender
    from vip.fulfillment import DeliverySendError, FulfillmentService

    oid = prepared(db, "vip_permanent")
    confirm(db, oid)
    group = Scenario(db)
    group.now = NOW + 1203
    invites = InviteSender(DeliverySendError(code, retry_after=300))

    def service():
        return FulfillmentService(
            db, 999, Sender(), access=group.service, invite_sender=invites, clock=lambda: group.now
        )

    service().run_once()
    with db.transaction() as s:
        job = s.get(Fulfillment, oid)
        assert job.status == state
        assert job.next_check_at >= group.now + 300
    group.now += 100
    assert service().run_once() == 0
    group.now += 250
    invites.failure = None
    service().run_once()
    assert len(invites.calls) == (2 if code == "rejected" else 1)


def test_revoked_order_never_delivers_and_wrong_group_does_not_change_configuration(db):
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender
    from vip.fulfillment import FulfillmentService

    oid = prepared(db, "vip_permanent")
    confirm(db, oid)
    group = Scenario(db)
    group.now = NOW + 1203
    invites, video = InviteSender(), Sender()
    group.settings.vip_chat_id = -100999
    FulfillmentService(
        db, 999, video, access=group.service, invite_sender=invites, clock=lambda: group.now
    ).run_once()
    assert invites.calls == [] and group.service.chat_id == -100999
    with db.transaction() as s:
        s.get(Order, oid).status = "cancelled"
    group.now += 100
    FulfillmentService(
        db, 999, video, access=group.service, invite_sender=invites, clock=lambda: group.now
    ).run_once()
    assert invites.calls == []
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == "cancelled"


@pytest.mark.parametrize("part", ["video_state", "vip_state"])
def test_database_failure_after_send_retains_evidence_without_resending(db, part, monkeypatch):
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    code = "sample_video" if part == "video_state" else "vip_permanent"
    oid = prepared(db, code)
    registered(db)
    confirm(db, oid)
    group = Scenario(db)
    group.now = NOW + 1203
    video, invites = Sender(), InviteSender()
    service = FulfillmentService(
        db, 999, video, access=group.service, invite_sender=invites, clock=lambda: group.now
    )
    save = service._save

    def fail_once(oid, owner, **values):
        if values.get(part) == "sent":
            monkeypatch.setattr(service, "_save", save)
            raise RuntimeError("synthetic commit failure")
        return save(oid, owner, **values)

    monkeypatch.setattr(service, "_save", fail_once)
    service.run_once()
    group.now += 100
    FulfillmentService(
        db, 999, video, access=group.service, invite_sender=invites, clock=lambda: group.now
    ).run_once()
    assert len(video.calls) + len(invites.calls) == 1
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == ("complete" if part == "video_state" else "review")


def test_existing_registered_media_outage_recovers_without_new_payment(db):
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    oid = prepared(db)
    confirm(db, oid)
    sender = Sender()
    FulfillmentService(db, 999, sender, clock=lambda: NOW + 1203).run_once()
    assert sender.calls == []
    registered(db)
    FulfillmentService(db, 999, sender, clock=lambda: NOW + 1300).run_once()
    assert len(sender.calls) == 1


def test_automatic_expired_order_does_not_bind_or_release_job(db):
    from vip.errors import DomainError

    oid = prepared(db)
    with db.transaction() as s:
        s.get(Order, oid).expires_at = NOW + 1202
    with pytest.raises(DomainError, match="order_not_payable"):
        confirm(db, oid)
    with db.transaction() as s:
        assert s.get(Order, oid).linked_telegram_id is None
        assert s.get(Fulfillment, oid).status == "waiting_payment"
