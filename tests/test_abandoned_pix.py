from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from tests.test_catalog import setup_catalog
from tests.test_pushinpay import Harness, notify
from vip.bot_purchases import BotPurchases
from vip.errors import DomainError
from vip.models import AccessGrant, AuditEvent, Fulfillment, Order, Payment, PushinCharge


@pytest.fixture
def h(db):
    setup_catalog(db)
    instance = Harness(db)
    instance.bot.clock = lambda: instance.now
    return instance


def abandon(h, db, product="sample_video"):
    first = h.bot.begin(123, product)
    with db.transaction() as s:
        s.get(Order, first["order_id"]).expires_at = h.now - 1
    return first


def test_explicit_replacement_preserves_old_charge_without_query_or_cancel(h, db):
    first = abandon(h, db)
    second = h.bot.replace(123, first["order_id"])
    assert second["order_id"] != first["order_id"] and second["copy_paste"]
    assert len(h.posts) == 2 and not h.gets
    with db.transaction() as s:
        assert s.get(Order, first["order_id"]).status == "pending"
        assert s.get(Order, first["order_id"]).expires_at == h.now - 1
        assert s.get(PushinCharge, first["order_id"]).copy_paste == first["copy_paste"]
    old = h.bot.status(123, first["order_id"])
    assert old["state"] == "replaced" and not old["copy_paste"]
    restarted = BotPurchases(db, h.bot.settings, payments=h.service, clock=lambda: h.now)
    assert restarted.replace(123, first["order_id"])["order_id"] == second["order_id"]
    assert len(h.posts) == 2


def test_simultaneous_replace_clicks_reserve_only_one_new_post(h, db):
    first = abandon(h, db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: h.bot.replace(123, first["order_id"]), range(2)))
    assert len({v["order_id"] for v in states}) == 1 and len(h.posts) == 2


@pytest.mark.parametrize("old_first", [True, False])
@pytest.mark.parametrize("product", ["sample_video", "vip_permanent"])
def test_both_attempts_paid_recorded_but_only_one_delivery(h, db, old_first, product):
    from tests.test_fulfillment import InviteSender
    from tests.test_groups import Scenario
    from tests.test_sample_delivery import Sender, registered
    from vip.fulfillment import FulfillmentService

    first = abandon(h, db, product)
    second = h.bot.replace(123, first["order_id"])
    txs = list(h.records)
    ids = [first["order_id"], second["order_id"]]
    if not old_first:
        txs.reverse()
        ids.reverse()
    registered(db)
    group = Scenario(db)
    videos, invites = Sender(), InviteSender()
    for tx in txs:
        notify(h, tx)
        h.changes = {"status": "paid"}
        h.service.process_pending()
        group.now = h.now
        FulfillmentService(
            db, 999, videos, access=group.service, invite_sender=invites, clock=lambda: h.now
        ).run_once()
        h.now += 60
    # Restarts and duplicate webhooks do not send again.
    notify(h, txs[-1])
    h.service.process_pending()
    FulfillmentService(
        db, 999, videos, access=group.service, invite_sender=invites, clock=lambda: h.now
    ).run_once()
    assert len(videos.calls) == (product == "sample_video")
    assert len(invites.calls) == (product == "vip_permanent")
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 2
        assert all(s.get(Order, oid).status == "paid" for oid in ids)
        assert s.get(Fulfillment, ids[0]).status == "complete"
        assert s.get(Fulfillment, ids[1]).last_error == "duplicate_payment_review_required"
        assert s.get(Fulfillment, ids[1]).status == "review"
        assert (
            s.scalar(
                select(func.count())
                .select_from(AccessGrant)
                .where(AccessGrant.granted_at.is_not(None))
            )
            == 1
        )
        assert (
            s.scalar(
                select(AuditEvent).where(AuditEvent.action == "duplicate_payment_review_required")
            )
            is not None
        )


def test_replacement_is_authenticated_and_does_not_change_other_product(h, db):
    video = abandon(h, db)
    vip = h.bot.begin(123, "vip_permanent")
    with pytest.raises(DomainError):
        h.bot.replace(456, video["order_id"])
    replaced = h.bot.replace(123, video["order_id"])
    assert replaced["amount_minor"] == 1000
    assert h.bot.begin(123, "vip_permanent")["order_id"] == vip["order_id"]
    assert len(h.posts) == 3 and not h.gets


async def test_unverified_screen_offers_explicit_new_pix_and_double_click_is_safe(h, db):
    from tests.test_bot_ui import UI
    from tests.test_pushinpay import configured

    first = abandon(h, db)
    ui = UI(db, settings=configured(), payments=h.service)
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    buttons = [b for row in ui.keyboard().inline_keyboard for b in row]
    assert any(b.text == "Gerar novo Pix" for b in buttons)
    assert len(h.posts) == 1
    await ui.click("pay:replace:" + first["order_id"])
    await ui.click("pay:replace:" + first["order_id"])
    assert len(h.posts) == 2
    assert [b.text for row in ui.keyboard().inline_keyboard for b in row] == [
        "Copiar código Pix",
        "Voltar",
    ]


def test_active_recoverable_pix_cannot_be_replaced_by_forged_callback(h):
    first = h.bot.begin(123, "sample_video")
    assert h.bot.replace(123, first["order_id"])["order_id"] == first["order_id"]
    assert len(h.posts) == 1 and not h.gets


def test_multiple_abandonments_keep_one_settlement_chain(h, db):
    from vip.models import PixReplacement

    a = abandon(h, db)
    b = h.bot.replace(123, a["order_id"])
    with db.transaction() as s:
        s.get(Order, b["order_id"]).expires_at = h.now - 1
    c = h.bot.replace(123, b["order_id"])
    assert h.bot.replace(123, a["order_id"])["order_id"] == c["order_id"]
    assert len(h.posts) == 3
    with db.transaction() as s:
        links = list(s.scalars(select(PixReplacement)))
        assert len(links) == 2 and {x.root_order_id for x in links} == {a["order_id"]}
    notify(h, next(iter(h.records)))
    h.changes = {"status": "paid"}
    h.service.process_pending()
    assert h.bot.status(123, c["order_id"])["state"] == "paid"
    assert h.bot.status(123, c["order_id"])["copy_paste"] is None


def test_original_paid_during_replacement_post_hides_new_code(h, db):
    import httpx

    first = abandon(h, db)
    original_handle = h.handle
    old_tx = next(iter(h.records))

    def handler(request):
        if request.method == "POST":
            h.changes = {"status": "paid"}
            h.service.reconcile(first["order_id"])
        return original_handle(request)

    h.gateway._transport = httpx.MockTransport(handler)
    result = h.bot.replace(123, first["order_id"])
    assert result["state"] == "paid" and not result["copy_paste"]
    assert len(h.posts) == 2 and h.gets == [old_tx]
    with db.transaction() as s:
        # The completed POST is preserved even when the old payment won the race.
        assert s.get(PushinCharge, result["order_id"]).transaction_id is not None
        assert s.scalar(select(func.count()).select_from(Payment)) == 1


def test_timeout_attempt_can_be_explicitly_replaced_without_retrying_its_post(h, db):
    from vip.errors import GatewayError

    h.fail_post = True
    with pytest.raises(GatewayError):
        h.bot.begin(123, "sample_video")
    first = h.bot.status(123)
    h.fail_post = False
    second = h.bot.replace(123, first["order_id"])
    assert second["order_id"] != first["order_id"] and second["copy_paste"]
    with db.transaction() as s:
        charge = s.get(PushinCharge, first["order_id"])
        assert charge.state == "uncertain" and charge.transaction_id is None
        assert s.get(Order, first["order_id"]).status == "pending"
    assert len(h.posts) == 2 and not h.gets


def test_inflight_creation_is_not_replaced(h, db):
    first = abandon(h, db)
    with db.transaction() as s:
        s.get(PushinCharge, first["order_id"]).state = "creating"
    with pytest.raises(DomainError, match="purchase_in_progress"):
        h.bot.replace(123, first["order_id"])
    assert len(h.posts) == 1


def test_authenticated_paid_after_cancellation_is_recorded_for_review(h, db):
    first = abandon(h, db)
    old_tx = next(iter(h.records))
    h.changes = {"status": "canceled"}
    h.service.reconcile(first["order_id"])
    second = h.bot.begin(123, "sample_video")
    new_tx = next(tx for tx in h.records if tx != old_tx)
    h.now += 60
    h.changes = {"status": "paid"}
    notify(h, new_tx)
    h.service.process_pending()
    h.now += 60
    notify(h, old_tx)
    h.service.process_pending()
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 2
        assert s.get(Fulfillment, first["order_id"]).status == "review"
        assert (
            s.get(Fulfillment, first["order_id"]).last_error == "duplicate_payment_review_required"
        )
        assert s.get(Order, second["order_id"]).status == "paid"


def test_abandoned_creating_reservation_does_not_block_forever(h, db):
    first = abandon(h, db)
    with db.transaction() as s:
        charge = s.get(PushinCharge, first["order_id"])
        charge.state, charge.created_at = "creating", h.now - 61
    old = h.bot.status(123)
    assert old["replacement_allowed"]
    second = h.bot.replace(123, first["order_id"])
    assert second["order_id"] != first["order_id"] and len(h.posts) == 2
    with db.transaction() as s:
        assert s.get(PushinCharge, first["order_id"]).state == "creating"
