from time import time

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, SendPhoto
from aiogram.types import BufferedInputFile
from sqlalchemy import func, select

from tests.media_fixtures import MEDIA_ROOT
from tests.test_bot_ui import MAIN_LABELS, UI
from tests.test_catalog import setup_catalog
from tests.test_omega_operations import Harness
from vip.bot_purchases import BotPurchases
from vip.models import Fulfillment, Order


@pytest.fixture
def chat(db):
    setup_catalog(db)
    h = Harness(db)
    h.service.clock = time
    settings = h.service.gateway._settings.model_copy(update={"start_banner_path": None})
    h.chat_purchases = BotPurchases(db, settings, payments=h.service)
    return UI(db, settings=settings, payments=h.service), h


def buttons(ui):
    return [b for row in ui.keyboard().inline_keyboard for b in row]


@pytest.mark.parametrize(
    "command,code,price",
    [("/sample", "sample_video", "10,00"), ("/vip", "vip_permanent", "20,00")],
)
async def test_chat_buy_button_collects_only_email_without_oidc(chat, db, command, code, price):
    ui, h = chat
    await ui.text(command)
    assert price in ui.screen()
    assert any(
        b.text == "Comprar agora" and b.callback_data == "pay:buy:" + code for b in buttons(ui)
    )
    assert not any(b.url for b in buttons(ui))
    before = len(ui.transport.requests)
    await ui.click("pay:buy:" + code)
    assert isinstance(ui.transport.requests[before], AnswerCallbackQuery)
    assert "nome completo" not in ui.screen() and "e-mail" in ui.screen()
    assert "OIDC" not in ui.screen() and "/checkout" not in ui.screen()
    assert h.post_count == 0
    with db.transaction() as s:
        order = s.scalar(select(Order).where(Order.buyer_telegram_id == 123))
        assert order.linked_telegram_id is None
        assert s.get(Fulfillment, order.id).status == "waiting_payment"


async def test_pix_photo_caption_and_buttons_sent_together_and_reused(chat):
    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("buyer@example.test")
    photos = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert len(photos) == 1
    photo = photos[0]
    assert isinstance(photo.photo, BufferedInputFile)
    assert photo.photo.data.startswith(b"\x89PNG")
    assert "Aguardando pagamento" in photo.caption and "<code>" not in photo.caption
    assert any(b.copy_text and b.copy_text.text == "000201synthetic-pix-code" for b in buttons(ui))
    assert photo.reply_markup and h.post_count == 1
    data = "pay:status:" + h.chat_purchases.status(123)["order_id"]
    h.changes = {"amount": 10.0, "chargeAmount": 10.0}
    await ui.click(data)
    assert h.post_count == 1
    assert len([m for m in ui.transport.requests if isinstance(m, SendPhoto)]) == 1
    assert any(isinstance(m, EditMessageCaption) for m in ui.transport.requests)


async def test_invalid_details_and_other_user_do_not_create_pix(chat, db):
    ui, h = chat
    await ui.text("/vip")
    await ui.click("pay:buy:vip_permanent", user_id=456, chat_id=123)
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Fulfillment)) == 0
    await ui.click("pay:buy:vip_permanent")
    await ui.text("A user declaration is not payment")
    assert "e-mail" in ui.screen() and h.post_count == 0


async def test_payment_back_navigation_restores_approved_banner_not_qr(db):
    setup_catalog(db)
    h = Harness(db)
    h.service.clock = time
    settings = h.service.gateway._settings.model_copy(
        update={"start_banner_path": MEDIA_ROOT / "sample.jpg"}
    )
    ui = UI(db, settings=settings, payments=h.service)
    await ui.text("/start")
    menu_mid = next(iter(ui.transport.messages))
    await ui.click("menu:sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("buyer@example.test")
    await ui.click("pay:home")
    keyboard = [[b.text for b in row] for row in ui.keyboard().inline_keyboard]
    assert keyboard == MAIN_LABELS
    edits = [m for m in ui.transport.requests if isinstance(m, EditMessageCaption)]
    assert edits[-1].message_id == menu_mid


async def test_failed_pix_caption_edit_recreates_with_cached_file_id(chat):
    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("buyer@example.test")
    status = "pay:status:" + h.chat_purchases.status(123)["order_id"]
    ui.transport.rejected_edit = "Bad Request: message to edit not found"
    h.changes = {"amount": 10.0, "chargeAmount": 10.0}
    await ui.click(status)
    photos = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert len(photos) == 2 and photos[-1].photo == "synthetic"
    assert photos[-1].reply_markup and "Aguardando pagamento" in photos[-1].caption
    assert h.post_count == 1


async def test_invalid_cached_photo_recovers_by_upload_without_another_charge(chat, monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("buyer@example.test")
    status = "pay:status:" + h.chat_purchases.status(123)["order_id"]
    ui.transport.rejected_edit = "Bad Request: message to edit not found"
    original = ui.transport.make_request

    async def reject_cached(bot, method, timeout=None):
        if isinstance(method, SendPhoto) and isinstance(method.photo, str):
            raise TelegramBadRequest(method, "wrong file identifier")
        return await original(bot, method, timeout)

    monkeypatch.setattr(ui.transport, "make_request", reject_cached)
    h.changes = {"amount": 10.0, "chargeAmount": 10.0}
    await ui.click(status)
    photos = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert len(photos) == 2 and isinstance(photos[-1].photo, BufferedInputFile)
    assert photos[-1].reply_markup and h.post_count == 1


async def test_oversized_qr_falls_back_to_complete_copy_paste(chat, db):
    from aiogram.methods import SendMessage

    from vip.models import BotPurchase, OmegaCharge

    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("buyer@example.test")
    with db.transaction() as s:
        oid = s.get(BotPurchase, (123, "sample_video")).order_id
        s.get(OmegaCharge, oid).copy_paste = "a" * 4000
        s.get(BotPurchase, (123, "sample_video")).pix_message_id = None
        s.get(BotPurchase, (123, "sample_video")).pix_file_id = None
    # Reopening reuses the charge; long Pix is sent only when copying is requested.
    await ui.click("pay:buy:sample_video")
    await ui.click("pay:copy:" + oid)
    sent = [m for m in ui.transport.requests if isinstance(m, SendMessage)]
    assert "a" * 4000 in sent[-1].text
    assert h.post_count == 1


async def test_payer_details_never_appear_in_application_logs(chat, caplog):
    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("private-buyer@example.test")
    assert h.request_body["client"]["name"] == "Buyer"
    assert h.request_body["client"]["email"] == "private-buyer@example.test"
    assert "Buyer" not in caplog.text
    assert "private-buyer@example.test" not in caplog.text


async def test_updated_copy_paste_never_reuses_a_stale_qr_photo(chat):
    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("buyer@example.test")
    status = "pay:status:" + h.chat_purchases.status(123)["order_id"]
    h.changes = {
        "amount": 10.0,
        "chargeAmount": 10.0,
        "pixInformation": {"qrCode": "000201updated-synthetic-pix-code"},
    }
    await ui.click(status)
    photos = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert len(photos) == 2 and isinstance(photos[-1].photo, BufferedInputFile)
    assert "000201updated-synthetic-pix-code" not in photos[-1].caption
    assert any(
        b.copy_text and b.copy_text.text == "000201updated-synthetic-pix-code" for b in buttons(ui)
    )
    assert h.post_count == 1


async def test_unreviewed_product_does_not_advertise_a_nonfunctional_purchase(chat, db):
    from vip.models import Product

    ui, h = chat
    with db.transaction() as s:
        s.get(Product, "sample_video").rights_reference = None
    await ui.text("/sample")
    assert not any(b.text == "Comprar agora" for b in buttons(ui))
    assert h.post_count == 0


async def test_email_prompt_has_no_refresh_or_internal_identity(chat):
    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    assert "📩 Informe seu e-mail para gerar o pagamento Pix." in ui.screen()
    assert "Telegram" not in ui.screen()
    assert not any((b.callback_data or "").startswith("pay:status:") for b in buttons(ui))
    assert h.post_count == h.get_count == 0


async def test_uncertain_creation_recovers_same_attempt_without_reasking_email(chat, caplog):
    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    h.timeout = True
    await ui.text("private-buyer@example.test")
    assert "Informe seu e-mail" not in ui.screen()
    assert "Aguardando pagamento" not in ui.screen()
    assert "Telegram ID" not in ui.screen()
    assert "code=omega_timeout" in caplog.text
    assert "phase=submit" in caplog.text
    assert "private-buyer@example.test" not in caplog.text
    assert not any((b.callback_data or "").startswith("pay:status:") for b in buttons(ui))
    recover = next(b.callback_data for b in buttons(ui) if b.text == "Verificar tentativa")
    h.timeout = False
    h.changes = {"amount": 10.0, "chargeAmount": 10.0}
    await ui.click(recover)
    assert h.post_count == h.get_count == 1
    assert any(b.text == "Copiar código Pix" for b in buttons(ui))
    assert any(b.copy_text for b in buttons(ui))


async def test_provider_rejection_is_not_an_invalid_email_or_a_new_charge(chat, caplog):
    import httpx

    ui, h = chat
    original = h.handle

    def reject(request):
        if request.method == "POST":
            h.post_count += 1
            return httpx.Response(400, json={"message": "private-buyer@example.test"})
        return original(request)

    h.service.gateway._transport = httpx.MockTransport(reject)
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    await ui.text("private-buyer@example.test")
    assert h.post_count == 1
    assert "code=omega_request_rejected" in caplog.text
    assert "private-buyer@example.test" not in caplog.text
    assert "e-mail válido" not in ui.screen()
    await ui.text("private-buyer@example.test")
    await ui.click("pay:buy:sample_video")
    assert h.post_count == 1


async def test_not_found_reconciliation_preserves_uncertainty_without_new_post(chat, caplog):
    import httpx

    ui, h = chat
    await ui.text("/sample")
    await ui.click("pay:buy:sample_video")
    h.timeout = True
    await ui.text("buyer@example.test")
    recover = next(b.callback_data for b in buttons(ui) if b.text == "Verificar tentativa")
    h.service.gateway._transport = httpx.MockTransport(lambda _: httpx.Response(404))
    await ui.click(recover)
    assert h.post_count == 1
    assert "code=omega_resource_not_found" in caplog.text
    assert any(b.text == "Verificar tentativa" for b in buttons(ui))
    assert not any(b.text == "Atualizar pagamento" for b in buttons(ui))
