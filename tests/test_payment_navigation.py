import pytest
from aiogram.methods import EditMessageCaption, SendMessage, SendPhoto, SendVideo
from sqlalchemy import select

from tests.media_fixtures import MEDIA_ROOT
from tests.test_bot_ui import MAIN_LABELS, UI
from tests.test_pushinpay import Harness, configured
from vip.bot_ui import START_TEXT
from vip.models import BotPurchase


def make_ui(db):
    from tests.test_catalog import setup_catalog

    setup_catalog(db)
    h = Harness(db)
    settings = configured().model_copy(
        update={
            "start_banner_path": MEDIA_ROOT / "sample.jpg",
            "start_video_path": MEDIA_ROOT / "example-store-start.mp4",
        }
    )
    return UI(db, settings=settings, payments=h.service), h


def buttons(ui):
    return [b for row in ui.keyboard().inline_keyboard for b in row]


async def buy(ui):
    await ui.text("/start")
    await ui.click("menu:sample")
    await ui.click("pay:buy:sample_video")


async def test_pix_is_short_and_native_copy_is_atomic(db):
    ui, h = make_ui(db)
    await buy(ui)
    assert h.posts and len(h.posts) == 1
    photo = next(r for r in reversed(ui.transport.requests) if isinstance(r, SendPhoto))
    assert "Aguardando pagamento." in photo.caption
    assert "Seu vídeo será enviado" not in photo.caption
    assert "Copie o código Pix ou escaneie o QR Code." in photo.caption
    assert "000201synthetic" not in photo.caption
    assert [b.text for b in buttons(ui)] == [
        "Copiar código Pix",
        "Voltar",
    ]
    assert buttons(ui)[0].copy_text.text == "000201synthetic-pushin-pix"
    assert "Pushin Pay" in photo.caption


async def test_pix_home_restores_start_video_in_current_conversation(db):
    ui, h = make_ui(db)
    await buy(ui)
    previous = len(ui.transport.requests)
    await ui.click("pay:home")
    new = ui.transport.requests[previous:]
    assert isinstance(new[-1], SendVideo)
    assert new[-1].caption == START_TEXT
    assert [[b.text for b in row] for row in ui.keyboard().inline_keyboard] == MAIN_LABELS
    assert len(h.posts) == 1


async def test_vip_never_reuses_sample_or_start_media(db):
    ui, h = make_ui(db)
    await ui.text("/start")
    before = len(ui.transport.requests)
    await ui.click("menu:vip")
    assert isinstance(ui.transport.requests[-1], SendMessage)
    assert "Acesso permanente ao grupo VIP por R$ 20,00. Pagamento único." in ui.screen()
    assert not any(
        isinstance(r, (SendPhoto, SendVideo, EditMessageCaption))
        for r in ui.transport.requests[before:]
    )
    assert any(b.text == "Comprar agora" for b in buttons(ui))
    assert not h.posts


async def test_pending_purchase_does_not_block_navigation_and_reuses_pix(db):
    ui, h = make_ui(db)
    await buy(ui)
    await ui.click("pay:product:sample_video")
    assert "Vídeo Exemplo" in ui.screen()
    await ui.click("menu:home")
    await ui.click("menu:vip")
    assert "VIP Permanente" in ui.screen()
    await ui.click("pay:buy:vip_permanent")
    assert "20,00" in ui.screen()
    await ui.click("pay:product:vip_permanent")
    await ui.click("menu:home")
    await ui.click("menu:sample")
    await ui.click("pay:buy:sample_video")
    assert "Aguardando pagamento." in ui.screen()
    assert any(b.copy_text for b in buttons(ui))
    assert len(h.posts) == 2 and not h.gets
    expected_order = h.bot.status(123, product_code="sample_video")["order_id"]
    with db.transaction() as s:
        assert s.get(BotPurchase, (123, "sample_video")).order_id == expected_order


async def test_long_pix_uses_safe_copy_callback_without_native_overflow(db):
    from vip.models import PushinCharge

    ui, h = make_ui(db)
    await buy(ui)
    with db.transaction() as s:
        oid = s.scalar(select(BotPurchase)).order_id
        s.get(PushinCharge, oid).copy_paste = "a" * 300
    await ui.click("pay:buy:sample_video")
    copy = next(b for b in buttons(ui) if b.text == "Copiar código Pix")
    assert copy.copy_text is None and copy.callback_data == "pay:copy:" + oid
    assert "a" * 300 not in ui.screen()
    await ui.click(copy.callback_data)
    assert "a" * 300 in ui.screen() and len(h.posts) == 1 and not h.gets


@pytest.mark.parametrize("length,native", [(256, True), (257, False)])
def test_native_copy_obeys_telegram_limit(length, native):
    from vip.bot_payment_ui import payment_keyboard

    data = dict(
        order_id="synthetic-order",
        state="awaiting_payment",
        product_code="sample_video",
        charge_state="ready",
        copy_paste="a" * length,
    )
    keyboard = payment_keyboard(data)
    copy = keyboard.inline_keyboard[0][0]
    assert bool(copy.copy_text) == native
    if native:
        assert copy.copy_text.text == data["copy_paste"]
    else:
        assert copy.callback_data == "pay:copy:synthetic-order"


async def test_native_copy_rejection_falls_back_without_another_charge(db, monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    ui, h = make_ui(db)
    original = ui.transport.make_request

    async def reject_native(bot, method, timeout=None):
        if isinstance(method, SendPhoto) and any(
            b.copy_text for row in method.reply_markup.inline_keyboard for b in row
        ):
            raise TelegramBadRequest(method, "BUTTON_TYPE_INVALID copy_text")
        return await original(bot, method, timeout)

    monkeypatch.setattr(ui.transport, "make_request", reject_native)
    await buy(ui)
    assert len(h.posts) == 1
    copy = next(b for b in buttons(ui) if b.text == "Copiar código Pix")
    assert copy.copy_text is None and copy.callback_data.startswith("pay:copy:")
    await ui.click(copy.callback_data)
    assert "000201synthetic-pushin-pix" in ui.screen() and not h.gets


async def test_start_and_vip_back_work_without_reconciling_pending_payment(db):
    ui, h = make_ui(db)
    await buy(ui)
    await ui.text("/start")
    assert ui.screen() == START_TEXT and isinstance(ui.transport.requests[-1], SendVideo)
    await ui.click("menu:vip")
    assert isinstance(ui.transport.requests[-1], SendMessage)
    await ui.click("menu:home")
    assert isinstance(ui.transport.requests[-1], SendVideo)
    assert len(h.posts) == 1 and not h.gets
