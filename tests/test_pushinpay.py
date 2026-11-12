import json
from concurrent.futures import ThreadPoolExecutor
from time import time
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from tests.test_catalog import setup_catalog
from vip.bot_purchases import BotPurchases
from vip.config import Settings
from vip.errors import DomainError, GatewayError
from vip.models import Order, Payment, PushinCharge
from vip.payments.pushinpay import PushinGateway, PushinPayments


def configured():
    return Settings(
        _env_file=None,
        payment_provider="pushinpay",
        pushinpay_enabled=True,
        pushinpay_token="synthetic-api-token",
        pushinpay_webhook_token="synthetic-webhook-secret-0123456789",
        pushinpay_callback_url="https://sales.example/webhooks/pushinpay",
    )


class Harness:
    def __init__(self, db):
        self.db, self.now = db, int(time())
        self.posts, self.gets, self.records = [], [], {}
        self.fail_post = False
        self.changes = {}
        self.gateway = PushinGateway(configured(), transport=httpx.MockTransport(self.handle))
        self.service = PushinPayments(db, self.gateway, clock=lambda: self.now)
        self.bot = BotPurchases(db, configured(), payments=self.service)

    def handle(self, request):
        assert request.headers["authorization"] == "Bearer synthetic-api-token"
        assert request.headers["accept"] == "application/json"
        if request.method == "POST":
            body = json.loads(request.content)
            self.posts.append(body)
            assert request.url.path == "/api/pix/cashIn"
            assert set(body) == {"value", "webhook_url"}
            if self.fail_post:
                raise httpx.ReadTimeout("private", request=request)
            tx = str(uuid4())
            record = {
                "id": tx,
                "status": "created",
                "value": body["value"],
                "qr_code": "000201synthetic-pushin-pix",
                "qr_code_base64": "",
                "webhook_url": body["webhook_url"],
            }
            self.records[tx] = record
            return httpx.Response(200, json=record)
        tx = request.url.path.rsplit("/", 1)[1]
        self.gets.append(tx)
        assert request.url.path == "/api/transactions/" + tx
        return httpx.Response(200, json=self.records[tx] | {"payment_type": "pix"} | self.changes)


@pytest.fixture
def h(db):
    setup_catalog(db)
    return Harness(db)


@pytest.mark.parametrize("code,amount", [("sample_video", 1000), ("vip_permanent", 2000)])
def test_buy_generates_pix_without_payer_details_and_reuses_order(h, code, amount):
    first = h.bot.begin(123, code)
    assert first["state"] == "awaiting_payment" and first["copy_paste"]
    assert first["amount_minor"] == amount and not h.bot.wants_buyer(123)
    assert h.bot.begin(123, code)["order_id"] == first["order_id"]
    assert len(h.posts) == 1 and h.posts[0]["value"] == amount


def test_timeout_preserves_reservation_without_duplicate_or_unknown_lookup(h, db):
    h.fail_post = True
    with pytest.raises(GatewayError):
        h.bot.begin(123, "sample_video")
    assert h.bot.status(123)["state"] == "pix_pending"
    h.bot.begin(123, "sample_video")
    with pytest.raises(DomainError, match="pushinpay_transaction_unknown"):
        h.bot.refresh(123)
    assert len(h.posts) == 1 and not h.gets
    with db.transaction() as s:
        assert s.scalar(select(PushinCharge)).state == "uncertain"


def test_local_expiration_does_not_create_a_second_still_payable_pix(h, db):
    first = h.bot.begin(123, "sample_video")
    with db.transaction() as s:
        s.get(Order, first["order_id"]).expires_at = 1
    h.changes = {"pix_details": {"expiration_date": "2081-06-23T02:31:34Z"}}
    assert h.bot.begin(123, "sample_video")["order_id"] == first["order_id"]
    assert len(h.posts) == 1


def test_concurrent_buy_reserves_one_post(h):
    def buy(_):
        try:
            return h.bot.begin(123, "sample_video")
        except DomainError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(buy, range(2)))
    assert len(h.posts) == 1


def notify(h, tx, token="synthetic-webhook-secret-0123456789"):
    return h.service.process_webhook(
        json.dumps({"id": tx, "status": "paid", "value": 1}).encode(), token
    )


def test_webhook_is_durable_and_never_trusts_body_before_query(h, db):
    state = h.bot.begin(123, "sample_video")
    tx = next(iter(h.records))
    with pytest.raises(DomainError, match="invalid_webhook_token"):
        notify(h, tx, "forged")
    notify(h, tx)
    assert not h.gets
    with db.transaction() as s:
        assert s.get(Order, state["order_id"]).status == "pending"
    restarted = PushinPayments(db, h.gateway, clock=lambda: h.now)
    h.changes = {"status": "paid", "value": "1000"}
    restarted.process_pending()
    assert h.bot.status(123)["state"] == "paid"
    notify(h, tx)
    restarted.process_pending()
    assert len(h.gets) == 1
    h.now += 60
    restarted.process_pending()
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 1


def test_value_mismatch_never_confirms_and_pending_event_can_recover(h, db):
    state = h.bot.begin(123, "sample_video")
    notify(h, next(iter(h.records)))
    h.changes = {"status": "paid", "value": 1}
    h.service.process_pending()
    with db.transaction() as s:
        assert s.get(Order, state["order_id"]).status == "pending"
    h.now += 60
    h.changes["value"] = 1000
    h.service.process_pending()
    assert h.bot.status(123)["state"] == "paid"


def test_query_limit_is_shared_across_transactions_and_service_instances(h, db):
    a = h.bot.begin(123, "sample_video")
    b = h.bot.begin(456, "vip_permanent")
    h.service.reconcile(a["order_id"])
    restarted = PushinPayments(db, h.gateway, clock=lambda: h.now)
    with pytest.raises(DomainError, match="reconciliation_rate_limited"):
        restarted.reconcile(b["order_id"])
    assert len(h.gets) == 1


def test_interactive_refresh_cannot_take_slot_from_pending_payment_notification(h):
    first = h.bot.begin(123, "sample_video")
    paid = h.bot.begin(456, "vip_permanent")
    transaction = list(h.records)[1]
    notify(h, transaction)
    with pytest.raises(DomainError, match="reconciliation_rate_limited"):
        h.bot.refresh(123, first["order_id"])
    assert not h.gets
    h.changes = {"status": "paid", "value": 2000}
    h.service.process_pending()
    assert h.gets == [transaction]
    assert h.bot.status(456)["order_id"] == paid["order_id"]
    assert h.bot.status(456)["state"] == "paid"


def test_known_bot_order_accepts_authenticated_payment_after_local_window(h, db):
    state = h.bot.begin(123, "sample_video")
    with db.transaction() as s:
        s.get(Order, state["order_id"]).expires_at = 1
    h.changes = {"status": "paid"}
    h.service.reconcile(state["order_id"])
    with db.transaction() as s:
        assert s.get(Order, state["order_id"]).status == "paid"
        assert s.scalar(select(func.count()).select_from(Payment)) == 1


def test_late_policy_does_not_bypass_unbound_order_identity(h, db):
    from vip.models import BotPurchase

    state = h.bot.begin(123, "sample_video")
    with db.transaction() as s:
        s.delete(s.get(BotPurchase, (123, "sample_video")))
        s.get(Order, state["order_id"]).expires_at = 1
    h.changes = {"status": "paid"}
    h.service.reconcile(state["order_id"])
    with db.transaction() as s:
        assert s.get(Order, state["order_id"]).status == "pending"
        assert s.scalar(select(func.count()).select_from(Payment)) == 0


def test_paid_signal_waits_for_authenticated_status_and_retries_after_restart(h, db):
    h.bot.begin(123, "sample_video")
    notify(h, next(iter(h.records)))
    h.service.process_pending()
    assert h.bot.status(123)["state"] == "awaiting_payment"
    h.now += 60
    h.changes = {"status": "paid"}
    PushinPayments(db, h.gateway, clock=lambda: h.now).process_pending()
    assert h.bot.status(123)["state"] == "paid"


def test_one_failed_transaction_does_not_starve_another_notification(h):
    a = h.bot.begin(123, "sample_video")
    b = h.bot.begin(456, "vip_permanent")
    for tx in h.records:
        notify(h, tx)
    h.changes = {"status": "paid", "value": 1}
    h.service.process_pending()
    h.now += 60
    h.changes = {"status": "paid"}
    h.service.process_pending()
    states = {h.bot.status(123)["state"], h.bot.status(456)["state"]}
    assert states == {"paid", "awaiting_payment"}
    assert a["order_id"] != b["order_id"] and len(set(h.gets)) == 2


def test_provider_mismatch_id_and_currency_cannot_confirm(h, db):
    from tests.test_omega_operations import Harness as OmegaHarness

    first = h.bot.begin(123, "sample_video")
    omega = OmegaHarness(db)
    with pytest.raises(DomainError, match="payment_provider_mismatch"):
        omega.create(first["order_id"])
    assert omega.post_count == 0
    h.changes = {"id": str(uuid4()), "status": "paid"}
    with pytest.raises(DomainError, match="transaction_mismatch"):
        h.service.reconcile(first["order_id"])
    h.now += 60
    h.changes = {"status": "paid", "currency": "USD"}
    with pytest.raises(GatewayError):
        h.service.reconcile(first["order_id"])


def test_old_omega_uncertainty_is_not_converted_or_replaced(h, db):
    from tests.test_omega_operations import Harness as OmegaHarness

    legacy = OmegaHarness(db)
    legacy.service.clock = time
    old = BotPurchases(db, legacy.service.gateway._settings, payments=legacy.service)
    first = old.begin(123, "sample_video")
    legacy.timeout = True
    with pytest.raises(GatewayError):
        old.submit(123, "buyer@example.test", "Synthetic")
    assert h.bot.begin(123, "sample_video")["order_id"] == first["order_id"]
    assert not h.posts
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).payment_provider == "omega"
        assert s.get(PushinCharge, first["order_id"]) is None


@pytest.mark.parametrize("state", ["FAILED", "EXPIRED"])
def test_legitimate_legacy_reconciliation_allows_new_pushin_purchase(h, db, state):
    from tests.test_omega_operations import Harness as OmegaHarness
    from vip.models import OmegaCharge

    legacy = OmegaHarness(db)
    legacy.service.clock = time
    old = BotPurchases(db, legacy.service.gateway._settings, payments=legacy.service)
    first = old.begin(123, "sample_video")
    legacy.timeout = True
    with pytest.raises(GatewayError):
        old.submit(123, "buyer@example.test", "Synthetic")
    legacy.changes = {"status": state, "amount": 10.0, "chargeAmount": 10.0}
    legacy.service.reconcile(first["order_id"])
    second = h.bot.begin(123, "sample_video")
    assert second["order_id"] != first["order_id"]
    assert second["copy_paste"] and second["payment_provider"] == "pushinpay"
    assert len(h.posts) == legacy.post_count == 1
    with db.transaction() as s:
        assert s.get(OmegaCharge, first["order_id"]).financial_status == state
        assert s.get(Order, first["order_id"]).payment_provider == "omega"
        assert s.get(Order, second["order_id"]).payment_provider == "pushinpay"


def test_disabled_legacy_recovery_has_specific_error_and_never_calls_new_provider(h, db):
    from tests.test_omega_operations import Harness as OmegaHarness

    legacy = OmegaHarness(db)
    legacy.service.clock = time
    old = BotPurchases(db, legacy.service.gateway._settings, payments=legacy.service)
    old.begin(123, "sample_video")
    legacy.timeout = True
    with pytest.raises(GatewayError):
        old.submit(123, "buyer@example.test", "Synthetic")
    with pytest.raises(DomainError, match="legacy_gateway_unavailable"):
        h.bot.refresh(123)
    assert not h.posts and not h.gets


def test_independent_controlled_buyer_can_test_pushin_while_legacy_remains_uncertain(h, db):
    from tests.test_omega_operations import Harness as OmegaHarness
    from vip.models import OmegaCharge

    legacy = OmegaHarness(db)
    legacy.service.clock = time
    old = BotPurchases(db, legacy.service.gateway._settings, payments=legacy.service)
    first = old.begin(123, "sample_video")
    legacy.timeout = True
    with pytest.raises(GatewayError):
        old.submit(123, "buyer@example.test", "Synthetic")
    assert h.bot.can_buy("sample_video")
    other = h.bot.begin(456, "sample_video")
    assert other["copy_paste"] and other["state"] == "awaiting_payment"
    assert len(h.posts) == 1 and not legacy.get_count
    with db.transaction() as s:
        prior = s.get(Order, first["order_id"])
        assert prior.status == "pending" and prior.payment_provider == "omega"
        assert prior.linked_telegram_id is None
        assert s.get(OmegaCharge, prior.id).state == "uncertain"
        assert s.get(Order, other["order_id"]).buyer_telegram_id == 456


async def test_legacy_recovery_message_explains_support_without_creating_a_pushin_pix(h, db):
    from tests.test_bot_ui import UI
    from tests.test_omega_operations import Harness as OmegaHarness

    legacy = OmegaHarness(db)
    legacy.service.clock = time
    old = BotPurchases(db, legacy.service.gateway._settings, payments=legacy.service)
    old.begin(123, "sample_video")
    legacy.timeout = True
    with pytest.raises(GatewayError):
        old.submit(123, "buyer@example.test", "Synthetic")
    ui = UI(db, settings=configured(), payments=h.service)
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    recover = next(
        b.callback_data
        for row in ui.keyboard().inline_keyboard
        for b in row
        if b.text == "Verificar tentativa"
    )
    await ui.click(recover)
    assert "Sua tentativa anterior precisa ser conferida" in ui.screen()
    assert "e-mail" not in ui.screen() and "Telegram ID" not in ui.screen()
    assert not h.posts and not h.gets


async def test_real_handler_distinguishes_second_account_and_logs_no_raw_identity(h, db, caplog):
    import re

    from tests.test_bot_ui import UI
    from tests.test_omega_operations import Harness as OmegaHarness

    legacy = OmegaHarness(db)
    legacy.service.clock = time
    old = BotPurchases(db, legacy.service.gateway._settings, payments=legacy.service)
    first = old.begin(123, "sample_video")
    legacy.timeout = True
    with pytest.raises(GatewayError):
        old.submit(123, "buyer@example.test", "Synthetic")
    caplog.clear()
    ui = UI(db, settings=configured(), payments=h.service)
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("/sample", user_id=456)
    await ui.click("pay:buy:sample_video", user_id=456, chat_id=456)
    assert "Copie o código Pix" in ui.screen() and len(h.posts) == 1
    assert len(set(re.findall(r"account_ref=([0-9a-f]{16})", caplog.text))) == 2
    assert "provider=omega" in caplog.text and "provider=pushinpay" in caplog.text
    assert "chat_pix_presented" in caplog.text
    for private in (first["order_id"], "buyer@example.test", ui.bot.token):
        assert private not in caplog.text
    assert "user_id=123" not in caplog.text and "user_id=456" not in caplog.text
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).status == "pending"


def test_legacy_draft_can_be_cancelled_without_collecting_email_after_switch(h, db):
    from tests.test_omega_operations import Harness as OmegaHarness

    legacy = OmegaHarness(db)
    old = BotPurchases(db, legacy.service.gateway._settings, payments=legacy.service)
    draft = old.begin(123, "sample_video")
    assert h.bot.begin(123, "sample_video")["state"] == "legacy_draft"
    assert not h.bot.wants_buyer(123)
    h.bot.cancel(123, draft["order_id"])
    assert h.bot.begin(123, "sample_video")["state"] == "awaiting_payment"
    assert len(h.posts) == 1 and legacy.post_count == 0


@pytest.mark.parametrize("code", ["sample_video", "vip_permanent", "recovery_bundle"])
def test_authenticated_payment_reuses_delivery_for_all_products(db, code):
    from tests.test_fulfillment import InviteSender, prepared
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService
    from vip.models import Fulfillment

    oid = prepared(db, code)
    h = Harness(db)
    with db.transaction() as s:
        order = s.get(Order, oid)
        order.payment_provider = "pushinpay"
        order.expires_at = h.now + 600
    h.service.create_charge(oid)
    tx = next(iter(h.records))
    notify(h, tx)
    h.changes = {"status": "paid"}
    h.service.process_pending()
    registered(db)
    group = Scenario(db)
    group.now = h.now
    video, invites = Sender(), InviteSender()
    delivery = FulfillmentService(
        db, 999, video, access=group.service, invite_sender=invites, clock=lambda: h.now
    )
    delivery.run_once()
    delivery.run_once()
    assert len(video.calls) == (code != "vip_permanent")
    assert len(invites.calls) == (code != "sample_video")
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == "complete"


async def test_buy_handler_sends_qr_and_copy_paste_without_asking_email(h, db):
    from tests.test_bot_ui import UI

    ui = UI(db, settings=configured(), payments=h.service)
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    assert "Copie o código Pix" in ui.screen()
    assert "e-mail" not in ui.screen() and "Telegram ID" not in ui.screen()
    assert any(b.text == "Copiar código Pix" for row in ui.keyboard().inline_keyboard for b in row)
    assert len(h.posts) == 1


async def test_api_webhook_authenticates_and_persists_before_acknowledging(h, db):
    from vip.api import create_app

    h.bot.begin(123, "sample_video")
    tx = next(iter(h.records))
    app = create_app(settings=configured(), database=db, pushin_gateway=h.gateway)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        forged = await client.post("/webhooks/pushinpay", json={"id": tx})
        assert forged.status_code == 401
        response = await client.post(
            "/webhooks/pushinpay",
            json={"id": tx, "status": "paid"},
            headers={"X-PushinPay-Token": "synthetic-webhook-secret-0123456789"},
        )
        assert response.status_code == 200 and not h.gets
        h.changes = {"status": "paid"}
        h.service.process_pending()
        assert h.bot.status(123)["state"] == "paid"


def test_canceled_payment_and_out_of_order_created_never_activate(h, db):
    from vip.models import AccessGrant, Fulfillment

    state = h.bot.begin(123, "sample_video")
    h.changes = {"status": "canceled"}
    h.service.reconcile(state["order_id"])
    h.now += 60
    h.changes = {"status": "paid"}
    h.service.reconcile(state["order_id"])
    with db.transaction() as s:
        # Record the authenticated receipt without restoring revoked access.
        assert s.get(Order, state["order_id"]).status == "paid"
        assert s.scalar(select(func.count()).select_from(Payment)) == 1
        assert s.get(Fulfillment, state["order_id"]).status == "review"
        assert (
            s.scalar(
                select(func.count())
                .select_from(AccessGrant)
                .where(AccessGrant.granted_at.is_not(None))
            )
            == 0
        )


def test_query_throttle_never_rounds_down_fractional_seconds(h):
    state = h.bot.begin(123, "sample_video")
    h.now += 0.8
    h.service.reconcile(state["order_id"])
    h.now += 59.3
    with pytest.raises(DomainError, match="reconciliation_rate_limited"):
        h.service.reconcile(state["order_id"])
    assert len(h.gets) == 1


def test_late_created_expired_or_canceled_do_not_remove_a_paid_buyers_access(h, db):
    state = h.bot.begin(123, "vip_permanent")
    h.changes = {"status": "paid"}
    h.service.reconcile(state["order_id"])
    for status in ("created", "expired", "canceled"):
        h.now += 60
        h.changes = {"status": status}
        h.service.reconcile(state["order_id"])
        with db.transaction() as s:
            assert s.get(Order, state["order_id"]).status == "paid"


@pytest.mark.parametrize("bad_value", [1000.0, True, "10.00", "-1000"])
def test_non_integer_minor_units_fail_closed(h, bad_value):
    state = h.bot.begin(123, "sample_video")
    h.changes = {"status": "paid", "value": bad_value}
    with pytest.raises(GatewayError):
        h.service.reconcile(state["order_id"])


async def test_product_api_uses_selected_provider_without_rewriting_existing_orders(h, db):
    from vip.api import create_app

    settings = configured().model_copy(update={"sales_api_key": SecretStr("s" * 40)})
    app = create_app(settings=settings, database=db, pushin_gateway=h.gateway)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Sales-API-Key": "s" * 40},
    ) as client:
        r = await client.post(
            "/product-orders",
            json={
                "product_code": "sample_video",
                "buyer_telegram_id": 123,
                "sales_request_id": "pushin-api-request-001",
            },
        )
        assert r.status_code == 201
        oid = r.json()["id"]
        assert (await client.post(f"/orders/{oid}/pix")).status_code == 200
        assert len(h.posts) == 1
