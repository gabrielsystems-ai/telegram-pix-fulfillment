import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import time

import pytest
from sqlalchemy import func, select

from tests.test_catalog import setup_catalog
from tests.test_omega_operations import Harness
from vip.bot_purchases import BotPurchases
from vip.db import Database
from vip.errors import DomainError
from vip.models import Fulfillment, Order, Product


@pytest.fixture
def purchases(db):
    setup_catalog(db)
    h = Harness(db)
    h.service.clock = time
    return BotPurchases(db, h.service.gateway._settings, payments=h.service), h


def buyer():
    return "Synthetic Buyer | buyer@example.test"


def test_direct_bot_identity_needs_no_oidc_and_reuses_pending_order(purchases, db):
    service, h = purchases
    first = service.begin(123, "sample_video")
    second = service.begin(123, "sample_video")
    assert first["order_id"] == second["order_id"] and first["state"] == "buyer_required"
    assert h.post_count == 0
    with db.transaction() as s:
        order = s.get(Order, first["order_id"])
        assert order.buyer_telegram_id == 123 and order.linked_telegram_id is None
        assert s.get(Fulfillment, order.id).status == "waiting_payment"
        from vip.models import WebPurchase

        assert s.scalar(select(func.count()).select_from(WebPurchase)) == 0


@pytest.mark.parametrize("code,amount", [("sample_video", 1000), ("vip_permanent", 2000)])
def test_buyer_submission_creates_one_correctly_priced_pix(purchases, code, amount):
    service, h = purchases
    order = service.begin(123, code)
    result = service.submit(123, buyer())
    assert result["amount_minor"] == amount and result["copy_paste"]
    assert result["state"] == "awaiting_payment"
    service.submit(123, buyer())
    assert h.post_count == 1
    assert h.request_body["amount"] == amount / 100
    assert h.request_body["identifier"] == order["order_id"]


@pytest.mark.parametrize(
    "text", ["hi", "Name | not-an-email", " | a@example.test", "A | a@example.test | extra"]
)
def test_invalid_buyer_data_never_calls_provider(purchases, text):
    service, h = purchases
    service.begin(123, "sample_video")
    with pytest.raises(DomainError, match="invalid_buyer_details"):
        service.submit(123, text)
    assert h.post_count == 0


def test_other_user_forged_product_and_price_mismatch_are_rejected(purchases, db):
    service, h = purchases
    state = service.begin(123, "sample_video")
    with pytest.raises(DomainError):
        service.status(456, state["order_id"])
    with pytest.raises(DomainError):
        service.begin(456, "recovery_bundle")
    with db.transaction() as s:
        s.get(Product, "vip_permanent").amount_minor = 1
    with pytest.raises(DomainError, match="product_price_mismatch"):
        service.begin(456, "vip_permanent")
    assert h.post_count == 0


def test_pending_purchase_survives_restart_and_blocks_parallel_orders(purchases, db):
    service, h = purchases
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: service.begin(123, "sample_video"), range(2)))
    assert states[0]["order_id"] == states[1]["order_id"]
    other = service.begin(123, "vip_permanent")
    assert other["order_id"] != states[0]["order_id"]
    service.begin(123, "sample_video")  # Restore only the selected presentation context.
    restarted_db = Database(str(db.engine.url))
    try:
        restarted = BotPurchases(restarted_db, service.settings, payments=h.service)
        assert restarted.status(123)["order_id"] == states[0]["order_id"]
        restarted.submit(123, buyer())
        assert h.post_count == 1
    finally:
        restarted_db.engine.dispose()


def test_only_unsubmitted_local_draft_can_be_cancelled(purchases, db):
    service, _ = purchases
    state = service.begin(123, "sample_video")
    service.cancel(123, state["order_id"])
    with db.transaction() as s:
        assert s.get(Order, state["order_id"]).status == "cancelled"
    state = service.begin(123, "vip_permanent")
    service.submit(123, buyer())
    with pytest.raises(DomainError, match="pix_already_created"):
        service.cancel(123, state["order_id"])


def test_timeout_cannot_cause_another_financial_post(purchases):
    from vip.errors import GatewayError

    service, h = purchases
    service.begin(123, "sample_video")
    h.timeout = True
    with pytest.raises(GatewayError):
        service.submit(123, buyer())
    assert service.status(123)["charge_state"] == "uncertain"
    h.timeout = False
    h.changes = {"amount": 10.0, "chargeAmount": 10.0}
    service.refresh(123)
    assert h.post_count == 1 and h.get_count == 1


@pytest.mark.parametrize("code,amount", [("sample_video", 1000), ("vip_permanent", 2000)])
def test_validated_webhook_releases_existing_automatic_delivery(purchases, db, code, amount):
    from tests.test_fulfillment import InviteSender
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService
    from vip.payments.operations import OmegaPayments

    service, h = purchases
    state = service.begin(123, code)
    service.submit(123, buyer())
    oid = state["order_id"]
    event = {
        "event": "TRANSACTION_PAID",
        "token": "synthetic-webhook-token",
        "transaction": {"id": "tx-" + oid},
    }
    payments = OmegaPayments(db, h.service.gateway)
    with pytest.raises(DomainError):
        payments.process_webhook(json.dumps(event | {"token": "forged"}).encode())
    h.changes = {
        "status": "COMPLETED",
        "payedAt": datetime.now(UTC).isoformat(),
        "amount": 0.01,
        "chargeAmount": amount / 100,
    }
    with pytest.raises(DomainError, match="payment_mismatch"):
        payments.process_webhook(json.dumps(event).encode())
    with db.transaction() as s:
        assert s.get(Fulfillment, oid).status == "waiting_payment"
        assert s.get(Order, oid).linked_telegram_id is None
    h.changes["amount"] = amount / 100
    assert payments.process_webhook(json.dumps(event).encode())["duplicate"] is False
    assert payments.process_webhook(json.dumps(event).encode())["duplicate"] is True
    registered(db)
    group = Scenario(db)
    group.now = int(time())
    video, invites = Sender(), InviteSender()
    FulfillmentService(
        db, 999, video, access=group.service, invite_sender=invites, clock=lambda: group.now
    ).run_once()
    assert service.status(123)["state"] == "available"
    assert len(video.calls) == (1 if code == "sample_video" else 0)
    assert len(invites.calls) == (1 if code == "vip_permanent" else 0)
    with db.transaction() as s:
        assert s.get(Order, oid).linked_telegram_id == 123
        assert s.scalar(select(func.count()).select_from(Fulfillment)) == 1
    with pytest.raises(DomainError, match="product_already_owned"):
        service.begin(123, code)


def test_expired_order_never_generates_a_pix(purchases, db):
    service, h = purchases
    state = service.begin(123, "sample_video")
    with db.transaction() as s:
        s.get(Order, state["order_id"]).expires_at = 1
    assert service.status(123)["state"] == "unavailable"
    assert service.submit(123, buyer())["state"] == "unavailable"
    assert h.post_count == 0


def test_disabled_flag_prevents_forged_buy_actions(purchases, db):
    from vip.errors import IntegrationUnavailable

    service, h = purchases
    service.settings = service.settings.model_copy(update={"omega_pix_enabled": False})
    with pytest.raises(IntegrationUnavailable):
        service.begin(123, "sample_video")
    assert h.post_count == 0
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Fulfillment)) == 0


def test_profile_name_and_real_email_are_forwarded_without_name_collection(purchases):
    service, h = purchases
    service.begin(123, "sample_video")
    result = service.submit(123, "buyer@example.test", profile_name="Synthetic Profile Name")
    assert result["state"] == "awaiting_payment"
    assert h.request_body["client"] == {
        "name": "Synthetic Profile Name",
        "email": "buyer@example.test",
    }


@pytest.mark.parametrize("email", ["", "not-an-email", "123456", "buyer"])
def test_missing_email_is_not_replaced_with_fabricated_data(purchases, email):
    service, h = purchases
    service.begin(123, "sample_video")
    with pytest.raises(DomainError, match="invalid_buyer_details"):
        service.submit(123, email, profile_name="Synthetic Name")
    assert h.post_count == 0


def test_expired_uncertain_attempt_blocks_new_order_and_survives_restart(purchases, db):
    from vip.errors import GatewayError

    service, h = purchases
    first = service.begin(123, "sample_video")
    h.timeout = True
    with pytest.raises(GatewayError):
        service.submit(123, buyer())
    with db.transaction() as s:
        s.get(Order, first["order_id"]).expires_at = 1
    restarted = BotPurchases(db, service.settings, payments=h.service)
    assert restarted.status(123)["state"] == "pix_pending"
    assert restarted.begin(123, "sample_video")["order_id"] == first["order_id"]
    assert restarted.begin(123, "vip_permanent")["order_id"] != first["order_id"]
    assert restarted.status(123, first["order_id"])["state"] == "pix_pending"
    assert h.post_count == 1


def test_refresh_before_email_does_not_query_provider(purchases):
    service, h = purchases
    first = service.begin(123, "sample_video")
    assert service.refresh(123, first["order_id"])["state"] == "buyer_required"
    assert h.get_count == h.post_count == 0
