from concurrent.futures import ThreadPoolExecutor
from time import time

import pytest
from sqlalchemy import select

from tests.test_pushinpay import Harness, configured, notify
from vip.bot_purchases import BotPurchases
from vip.errors import DomainError, GatewayError
from vip.models import BotPurchase, Fulfillment, OmegaCharge, Order


@pytest.fixture
def h(db):
    from tests.test_catalog import setup_catalog

    setup_catalog(db)
    return Harness(db)


def test_two_products_reuse_only_their_own_pending_charge(h, db):
    video = h.bot.begin(123, "sample_video")
    vip = h.bot.begin(123, "vip_permanent")
    assert video["order_id"] != vip["order_id"] and len(h.posts) == 2
    assert h.bot.begin(123, "sample_video")["order_id"] == video["order_id"]
    assert h.bot.begin(123, "vip_permanent")["order_id"] == vip["order_id"]
    assert len(h.posts) == 2
    for order in (video, vip):
        assert h.bot.status(123, order["order_id"])["state"] == "awaiting_payment"
        with db.transaction() as s:
            assert s.get(Order, order["order_id"]).status == "pending"
    with db.transaction() as s:
        assert len(list(s.scalars(select(BotPurchase)))) == 2


def test_old_uncertainty_stays_protected_for_its_product_only(h, db):
    from tests.test_omega_operations import Harness as OmegaHarness

    legacy = OmegaHarness(db)
    legacy.service.clock = time
    old = BotPurchases(db, legacy.service.gateway._settings, payments=legacy.service)
    video = old.begin(123, "sample_video")
    legacy.timeout = True
    with pytest.raises(GatewayError):
        old.submit(123, "buyer@example.test", "Synthetic")
    vip = h.bot.begin(123, "vip_permanent")
    assert vip["copy_paste"] and len(h.posts) == 1
    assert h.bot.begin(123, "sample_video")["order_id"] == video["order_id"]
    assert len(h.posts) == 1
    with db.transaction() as s:
        assert s.get(Order, video["order_id"]).status == "pending"
        assert s.get(OmegaCharge, video["order_id"]).state == "uncertain"


def test_concurrent_different_products_create_one_charge_each(h):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda code: h.bot.begin(123, code), ["sample_video", "vip_permanent"])
        )
    assert len(h.posts) == 2 and len({r["order_id"] for r in results}) == 2
    assert h.bot.begin(123, "sample_video")["order_id"] in {r["order_id"] for r in results}
    assert len(h.posts) == 2


def test_each_order_keeps_its_own_qr_message_and_ownership(h):
    a = h.bot.begin(123, "sample_video")
    b = h.bot.begin(123, "vip_permanent")
    h.bot.remember_message(123, a["order_id"], 101, "video-pix", a["copy_paste"])
    h.bot.remember_message(123, b["order_id"], 202, "vip-pix", b["copy_paste"])
    assert h.bot.status(123, a["order_id"])["pix_message_id"] == 101
    assert h.bot.status(123, b["order_id"])["pix_message_id"] == 202
    with pytest.raises(DomainError):
        h.bot.status(456, a["order_id"])


def test_earlier_payment_delivers_only_its_original_product(h, db):
    from tests.test_fulfillment import InviteSender
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    video = h.bot.begin(123, "sample_video")
    video_tx = next(iter(h.records))
    vip = h.bot.begin(123, "vip_permanent")
    vip_tx = next(tx for tx in h.records if tx != video_tx)
    notify(h, video_tx)
    h.changes = {"status": "paid"}
    h.service.process_pending()
    registered(db)
    group = Scenario(db)
    group.now = h.now
    videos, invites = Sender(), InviteSender()
    delivery = FulfillmentService(
        db, 999, videos, access=group.service, invite_sender=invites, clock=lambda: h.now
    )
    delivery.run_once()
    assert len(videos.calls) == 1 and not invites.calls
    with db.transaction() as s:
        assert s.get(Fulfillment, video["order_id"]).status == "complete"
        assert s.get(Fulfillment, vip["order_id"]).status == "waiting_payment"
    h.now += 60
    group.now = h.now
    notify(h, vip_tx)
    h.service.process_pending()
    delivery.run_once()
    delivery.run_once()
    assert len(videos.calls) == len(invites.calls) == 1


async def test_pix_has_two_buttons_and_product_switch_keeps_both_payments(h, db):
    from tests.test_bot_ui import UI

    ui = UI(db, settings=configured(), payments=h.service)
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    assert [b.text for row in ui.keyboard().inline_keyboard for b in row] == [
        "Copiar código Pix",
        "Voltar",
    ]
    assert "Copie o código Pix ou escaneie o QR Code." in ui.screen()
    await ui.click("pay:product:sample_video")
    await ui.click("menu:home")
    await ui.click("menu:vip")
    await ui.click("pay:buy:vip_permanent")
    assert "20,00" in ui.screen() and len(h.posts) == 2
    assert [b.text for row in ui.keyboard().inline_keyboard for b in row] == [
        "Copiar código Pix",
        "Voltar",
    ]
    await ui.click("pay:product:vip_permanent")
    await ui.click("menu:home")
    await ui.click("menu:sample")
    await ui.click("pay:buy:sample_video")
    assert "10,00" in ui.screen() and len(h.posts) == 2
