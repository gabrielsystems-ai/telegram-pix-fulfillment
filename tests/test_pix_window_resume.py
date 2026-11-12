from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tests.test_catalog import setup_catalog
from tests.test_pushinpay import Harness, notify
from vip.errors import DomainError, GatewayError
from vip.models import Order, Payment


@pytest.fixture
def h(db):
    setup_catalog(db)
    return Harness(db)


def expired_purchase(h, db):
    h.bot.clock = lambda: h.now
    first = h.bot.begin(123, "sample_video")
    with db.transaction() as s:
        s.get(Order, first["order_id"]).expires_at = h.now - 1
    return first


def test_resume_checks_provider_and_audits_same_pix_before_delivery(h, db):
    from vip.models import PaymentWindowRenewal

    first = expired_purchase(h, db)
    expiry = datetime.fromtimestamp(h.now + 3600, UTC).isoformat()
    h.changes = {"pix_details": {"expiration_date": expiry}}
    resumed = h.bot.begin(123, "sample_video")
    assert resumed["order_id"] == first["order_id"]
    assert resumed["copy_paste"] == first["copy_paste"]
    assert resumed["state"] == "awaiting_payment"
    assert len(h.posts) == len(h.gets) == 1
    with db.transaction() as s:
        audit = s.scalar(select(PaymentWindowRenewal))
        assert audit.previous_expires_at == h.now - 1
        assert audit.new_expires_at == resumed["expires_at"] == h.now + 1800
        assert audit.provider_expires_at == h.now + 3600
        assert audit.provider_expiration == expiry
        assert s.get(Order, first["order_id"]).status == "pending"
    assert h.bot.begin(123, "sample_video")["order_id"] == first["order_id"]
    assert len(h.gets) == 1
    # A restarted service consumes the real webhook through existing fulfillment.
    notify(h, next(iter(h.records)))
    h.now += 60
    h.changes["status"] = "paid"
    h.service.process_pending()
    assert h.bot.status(123)["state"] == "paid"
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 1
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    registered(db)
    sender = Sender()
    delivery = FulfillmentService(db, 999, sender, clock=lambda: h.now)
    delivery.run_once()
    # Reconstruct the worker: persisted delivery prevents a second send.
    FulfillmentService(db, 999, sender, clock=lambda: h.now).run_once()
    assert len(sender.calls) == 1 and h.bot.status(123)["state"] == "available"


@pytest.mark.parametrize("expiry", [None, "invalid", "2020-01-01T00:00:00Z"])
def test_missing_invalid_or_expired_provider_expiration_never_renews(h, db, expiry):
    first = expired_purchase(h, db)
    h.changes = {"pix_details": {"expiration_date": expiry}}
    result = h.bot.begin(123, "sample_video")
    assert result["state"] in {"validity_unverified", "unavailable"}
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).expires_at == h.now - 1
    assert len(h.posts) == 1


def test_naive_expiration_uses_conservative_bound_not_guessed_timezone(h, db):
    first = expired_purchase(h, db)
    naive = datetime.fromtimestamp(h.now, UTC).replace(tzinfo=None) + timedelta(
        hours=14, minutes=10
    )
    h.changes = {"pix_details": {"expiration_date": naive.isoformat(" ")}}
    resumed = h.bot.begin(123, "sample_video")
    assert resumed["expires_at"] == h.now + 600
    assert resumed["order_id"] == first["order_id"] and len(h.posts) == 1


@pytest.mark.parametrize("state", ["paid", "expired", "canceled"])
def test_non_created_provider_state_never_renews(h, db, state):
    first = expired_purchase(h, db)
    h.changes = {"status": state}
    result = h.bot.begin(123, "sample_video")
    assert result["state"] == ("paid" if state == "paid" else "unavailable")
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).expires_at == h.now - 1
        assert s.scalar(select(func.count()).select_from(Payment)) == (state == "paid")
    assert len(h.posts) == 1


def test_created_with_ambiguous_near_expiration_is_not_enough(h, db):
    first = expired_purchase(h, db)
    naive = datetime.fromtimestamp(h.now + 3600, UTC).replace(tzinfo=None)
    h.changes = {"pix_details": {"expiration_date": naive.isoformat(" ")}}
    result = h.bot.begin(123, "sample_video")
    assert result["state"] == "validity_unverified" and not result["copy_paste"]
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).expires_at == h.now - 1


def test_timeout_preserves_window_and_query_rate_gate(h, db, monkeypatch):
    first = expired_purchase(h, db)

    def failed_query(identifier):
        raise GatewayError("pushinpay_timeout")

    monkeypatch.setattr(h.gateway, "query_payment", failed_query)
    with pytest.raises(GatewayError, match="pushinpay_timeout"):
        h.bot.begin(123, "sample_video")
    retry = h.bot.begin(123, "sample_video")
    assert retry["state"] == "validity_unverified"
    assert "novo Pix" in retry["message"]
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).expires_at == h.now - 1
    assert len(h.posts) == 1


@pytest.mark.parametrize("state", ["canceled", "expired"])
def test_authenticated_terminal_state_allows_next_explicit_purchase(h, db, state):
    first = expired_purchase(h, db)
    h.changes = {"status": state}
    closed = h.bot.begin(123, "sample_video")
    assert closed["order_id"] == first["order_id"] and closed["state"] == "unavailable"
    assert len(h.posts) == 1
    second = h.bot.begin(123, "sample_video")
    assert second["order_id"] != first["order_id"] and second["copy_paste"]
    assert len(h.posts) == 2


def test_definitely_elapsed_provider_date_allows_next_purchase_without_assuming_timezone(h, db):
    first = expired_purchase(h, db)
    raw = datetime.fromtimestamp(h.now - 13 * 3600, UTC).replace(tzinfo=None).isoformat(" ")
    h.changes = {"pix_details": {"expiration_date": raw}}
    closed = h.bot.begin(123, "sample_video")
    assert closed["state"] == "unavailable" and len(h.posts) == 1
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).status == "expired"
    second = h.bot.begin(123, "sample_video")
    assert second["order_id"] != first["order_id"] and len(h.posts) == 2


def test_incomplete_response_and_early_retry_preserve_same_attempt(h, db):
    first = expired_purchase(h, db)
    h.changes = {"pix_details": None}
    result = h.bot.begin(123, "sample_video")
    assert result["state"] == "validity_unverified" and not result["copy_paste"]
    # No further external request within one minute.
    again = h.bot.begin(123, "sample_video")
    assert again["order_id"] == first["order_id"]
    assert again["state"] == "validity_unverified"
    assert len(h.posts) == len(h.gets) == 1
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).status == "pending"


async def test_unchanged_qr_caption_is_success_and_does_not_duplicate_media(h, db):
    from aiogram.methods import EditMessageCaption, SendPhoto

    from tests.test_bot_ui import UI
    from tests.test_pushinpay import configured

    ui = UI(db, settings=configured(), payments=h.service)
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    ui.transport.rejected_edit = "Bad Request: message is not modified"
    before = len(ui.transport.requests)
    await ui.click("pay:buy:sample_video")
    sent = ui.transport.requests[before:]
    assert any(isinstance(r, EditMessageCaption) for r in sent)
    assert not any(isinstance(r, SendPhoto) for r in sent)
    assert len(h.posts) == 1 and not h.gets


def test_paid_late_after_closed_order_cannot_create_replacement(h, db):
    first = expired_purchase(h, db)
    with db.transaction() as s:
        s.get(Order, first["order_id"]).status = "expired"
    h.changes = {"status": "paid"}
    h.service.reconcile(first["order_id"])
    with pytest.raises(DomainError, match="product_already_owned"):
        h.bot.begin(123, "sample_video")
    assert h.bot.status(123)["state"] == "paid"
    assert len(h.posts) == 1


def test_fractional_provider_deadline_is_not_closed_early(h, db):
    first = expired_purchase(h, db)
    raw = datetime.fromtimestamp(h.now + 0.557, UTC).isoformat()
    h.changes = {"pix_details": {"expiration_date": raw}}
    view = h.bot.begin(123, "sample_video")
    assert view["state"] == "validity_unverified"
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).status == "pending"
    assert len(h.posts) == 1


async def test_real_buy_callback_resumes_same_qr_without_extra_post(h, db):
    from aiogram.methods import SendPhoto

    from tests.test_bot_ui import UI
    from tests.test_pushinpay import configured

    first = expired_purchase(h, db)
    h.changes = {
        "pix_details": {"expiration_date": datetime.fromtimestamp(h.now + 3600, UTC).isoformat()}
    }
    ui = UI(db, settings=configured(), payments=h.service)
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    sent = next(r for r in reversed(ui.transport.requests) if isinstance(r, SendPhoto))
    buttons = [b for row in sent.reply_markup.inline_keyboard for b in row]
    assert len(buttons) == 2
    assert buttons[0].copy_text.text == first["copy_paste"]
    assert "Aguardando pagamento." in sent.caption
    assert "10,00" in sent.caption and first["copy_paste"] not in sent.caption
    assert len(h.posts) == len(h.gets) == 1


@pytest.mark.parametrize(
    "change", [{"value": 2000}, {"id": "00000000-0000-0000-0000-000000000001"}]
)
def test_mismatch_never_renews(h, db, change):
    first = expired_purchase(h, db)
    h.changes = change
    with pytest.raises(DomainError):
        h.bot.begin(123, "sample_video")
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).expires_at == h.now - 1
    assert len(h.posts) == 1
