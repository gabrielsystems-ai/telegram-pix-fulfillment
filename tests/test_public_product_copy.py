import pytest
from aiogram.methods import SendMessage, SendPhoto

from tests.media_fixtures import MEDIA_ROOT
from tests.test_bot_ui import UI
from tests.test_catalog import product_order, setup_catalog
from tests.test_product_access import bind, settle
from vip.config import Settings


def buttons(ui):
    return [b for row in ui.keyboard().inline_keyboard for b in row]


@pytest.mark.parametrize("command", ["/sample", "/vip", "/acessar"])
async def test_public_product_screens_do_not_require_manual_linking(db, command):
    ui = UI(db)
    await ui.text(command)
    text = ui.screen().casefold()
    assert "vincul" not in text and "telegram" not in text
    assert all(b.callback_data != "menu:link" for b in buttons(ui))
    assert not any(b.text in {"Comprar agora", "Garantir acesso VIP"} or b.url for b in buttons(ui))
    if command != "/acessar":
        assert "temporariamente indisponíveis" in text


async def test_inactive_vip_offers_a_working_product_navigation(db):
    ui = UI(db)
    await ui.text("/start")
    await ui.click("menu:access")
    assert "Seu acesso VIP ainda não está ativo." in ui.screen()
    assert any(
        b.text == "💎 Conhecer VIP Permanente" and b.callback_data == "menu:vip"
        for b in buttons(ui)
    )
    await ui.click("menu:vip")
    assert "R$ 20,00" in ui.screen() and "Pagamento único" in ui.screen()
    assert "vincul" not in ui.screen().lower()


async def test_video_owner_receives_the_registered_video_and_no_purchase_link(db, monkeypatch):
    from tests.test_sample_delivery import Sender, registered

    setup_catalog(db)
    bind(db, settle(db, product_order(db)))
    registered(db)
    sender = Sender()
    monkeypatch.setattr("vip.bot.TelegramVideoSender", lambda token: sender)
    ui = UI(db)
    await ui.text("/sample")
    assert any(b.text == "▶️ Receber vídeo" and b.callback_data == "menu:video" for b in buttons(ui))
    assert not any(b.url or b.callback_data == "menu:link" for b in buttons(ui))
    await ui.click("menu:video")
    assert "enviado" in ui.screen() and len(sender.calls) == 1
    await ui.click("menu:video")
    assert "já foi entregue" in ui.screen() and len(sender.calls) == 1


async def test_vip_owner_keeps_access_action_and_single_payment_description(db):
    setup_catalog(db)
    bind(db, settle(db, product_order(db, "vip_permanent")))
    ui = UI(db, settings=Settings(_env_file=None, vip_chat_id=-100123))
    await ui.text("/vip")
    assert "Pagamento único" in ui.screen()
    assert "Acesso permanente ao grupo VIP" in ui.screen()
    assert any(b.text == "🎟️ Acessar VIP" and b.callback_data == "menu:access" for b in buttons(ui))
    assert not any(b.url or b.callback_data == "menu:link" for b in buttons(ui))


async def test_configured_chat_purchase_uses_callbacks_without_external_checkout(db):
    setup_catalog(db)
    settings = Settings(
        _env_file=None,
        omega_pix_enabled=True,
        omega_client_id="synthetic-public",
        omega_client_secret="synthetic-private",
        omega_callback_url="https://example.test/webhooks/omega",
    )
    ui = UI(db, settings=settings)
    for command in ["/sample", "/vip"]:
        await ui.text(command)
        assert any(
            b.text == "Comprar agora" and b.callback_data.startswith("pay:buy:")
            for b in buttons(ui)
        )
        assert not any(b.url for b in buttons(ui))
        assert "OIDC" not in ui.screen() and "/checkout" not in ui.screen()


@pytest.mark.parametrize("banner", [None, MEDIA_ROOT / "sample.jpg"])
async def test_edit_failure_recreates_a_complete_nontechnical_access_screen(db, banner):
    ui = UI(db, settings=Settings(_env_file=None, start_banner_path=banner))
    await ui.text("/start")
    ui.transport.rejected_edit = "Bad Request: message to edit not found"
    await ui.click("menu:access")
    sent = [m for m in ui.transport.requests if isinstance(m, (SendPhoto, SendMessage))]
    assert len(sent) == 2
    last = sent[-1]
    assert "Seu acesso VIP ainda não está ativo." in last.text
    assert any(
        b.callback_data == "menu:vip" for row in last.reply_markup.inline_keyboard for b in row
    )
    assert all(
        b.callback_data != "menu:link" for row in last.reply_markup.inline_keyboard for b in row
    )
