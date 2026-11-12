from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from aiogram.types import Video
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from tests.test_catalog import product_order, setup_catalog
from tests.test_domain import NOW
from tests.test_product_access import bind, settle
from vip.models import BotMediaCache, Order, Product, RecoveryTask
from vip.video_delivery import (
    VideoSendError,
    deliver_video,
    profile,
    register_video,
    update_video_offer,
)


class Sender:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def send(self, user_id, file_id):
        self.calls.append((user_id, file_id))
        if self.failure:
            raise VideoSendError(self.failure)
        return SimpleNamespace(message_id=123)


def registered(db):
    p = profile()
    video = Video(
        file_id="synthetic-video",
        file_unique_id="synthetic-unique",
        file_size=p["size"],
        width=p["width"],
        height=p["height"],
        duration=170,
    )
    register_video(db, 999, video)
    return video


def purchased(db, code="sample_video", *, old=False):
    setup_catalog(db)
    if code == "recovery_bundle":
        with db.transaction() as session:
            session.add(
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
    if old:
        with db.transaction() as session:
            order = session.get(Order, oid)
            order.amount_minor, order.product_name = 900, "Vídeo Anterior"
    bind(db, settle(db, oid))
    return oid


@pytest.mark.parametrize(
    "code,expected",
    [
        ("sample_video", "sent"),
        ("vip_permanent", "denied"),
        ("recovery_bundle", "sent"),
    ],
)
def test_video_delivery_respects_paid_linked_permissions(db, code, expected):
    purchased(db, code)
    registered(db)
    sender = Sender()
    assert deliver_video(db, 999, 123, sender) == expected
    assert len(sender.calls) == (expected == "sent")
    assert deliver_video(db, 999, 456, sender) == "denied"


def test_old_purchase_keeps_original_price_and_video_permission(db):
    oid = purchased(db, old=True)
    registered(db)
    with db.transaction() as session:
        update_video_offer(session)
        assert session.get(Order, oid).amount_minor == 900
        assert session.get(Order, oid).product_name == "Vídeo Anterior"
        assert session.get(Product, "sample_video").amount_minor == 1000
    assert deliver_video(db, 999, 123, Sender()) == "sent"


def test_pending_and_unlinked_orders_never_deliver(db):
    setup_catalog(db)
    oid = product_order(db)
    registered(db)
    sender = Sender()
    assert deliver_video(db, 999, 123, sender) == "denied"
    settle(db, oid)
    assert deliver_video(db, 999, 123, sender) == "denied"
    assert sender.calls == []


def test_delivery_is_persistently_idempotent_and_bot_specific(db):
    purchased(db)
    registered(db)
    sender = Sender()
    assert deliver_video(db, 777, 123, sender) == "unavailable"
    assert deliver_video(db, 999, 123, sender) == "sent"
    assert deliver_video(db, 999, 123, sender) == "already_sent"
    assert len(sender.calls) == 1


def test_ambiguous_network_failure_never_resends_automatically(db):
    purchased(db)
    registered(db)
    sender = Sender("uncertain")
    assert deliver_video(db, 999, 123, sender) == "uncertain"
    assert deliver_video(db, 999, 123, sender) == "uncertain"
    assert len(sender.calls) == 1


def test_definite_rejection_can_be_retried_explicitly(db):
    purchased(db)
    registered(db)
    assert deliver_video(db, 999, 123, Sender("rejected")) == "unavailable"
    assert deliver_video(db, 999, 123, Sender()) == "sent"


def test_failure_committing_after_success_preserves_durable_claim(db, monkeypatch):
    purchased(db)
    registered(db)
    sender = Sender()
    transaction = db.transaction

    @contextmanager
    def fail_after_send():
        with transaction() as session:
            yield session
            if sender.calls:
                raise OperationalError("synthetic failure", None, None)

    monkeypatch.setattr(db, "transaction", fail_after_send)
    assert deliver_video(db, 999, 123, sender) == "uncertain"
    monkeypatch.setattr(db, "transaction", transaction)
    assert deliver_video(db, 999, 123, sender) == "uncertain"
    assert len(sender.calls) == 1


def test_registration_validates_metadata_and_keeps_original_media(db):
    video = registered(db)
    with pytest.raises(Exception, match="video_metadata_mismatch"):
        register_video(db, 999, video.model_copy(update={"file_size": 1}))
    with db.transaction() as session:
        assert session.get(BotMediaCache, (999, profile()["sha256"])).file_id == "synthetic-video"


def test_offer_update_preserves_group_other_products_and_reviews(db):
    setup_catalog(db)
    with db.transaction() as session:
        before = [
            (p.code, p.amount_minor, p.telegram_group_id, p.rights_reference)
            for p in session.scalars(select(Product))
            if p.code != "sample_video"
        ]
        update_video_offer(session)
        after = [
            (p.code, p.amount_minor, p.telegram_group_id, p.rights_reference)
            for p in session.scalars(select(Product))
            if p.code != "sample_video"
        ]
        assert before == after


def test_concurrent_delivery_requests_never_send_twice(db):
    from concurrent.futures import ThreadPoolExecutor

    purchased(db)
    registered(db)
    sender = Sender()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver_video(db, 999, 123, sender), range(2)))
    assert "sent" in results
    assert len(sender.calls) == 1


def test_revocation_after_claim_is_rechecked_before_external_send(db, monkeypatch):
    oid = purchased(db)
    registered(db)
    transaction = db.transaction
    claimed = False

    @contextmanager
    def revoke_after_claim():
        nonlocal claimed
        with transaction() as session:
            yield session
        if not claimed:
            claimed = True
            with transaction() as session:
                session.get(Order, oid).status = "cancelled"

    monkeypatch.setattr(db, "transaction", revoke_after_claim)
    sender = Sender()
    assert deliver_video(db, 999, 123, sender) == "denied"
    assert not sender.calls


def test_concrete_sender_uses_only_file_id_and_protected_video(monkeypatch):
    from pydantic import SecretStr

    import vip.video_delivery as module

    calls = []

    class FakeBot:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_video(self, user_id, file_id, **kwargs):
            calls.append((user_id, file_id, kwargs))
            return SimpleNamespace(message_id=1)

    monkeypatch.setattr(module, "Bot", lambda *args, **kwargs: FakeBot())
    module.TelegramVideoSender(SecretStr("999:synthetic-token")).send(123, "synthetic-file-id")
    assert calls[0][0:2] == (123, "synthetic-file-id")
    assert calls[0][2]["protect_content"] is True
