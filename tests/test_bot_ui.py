from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    GetWebhookInfo,
    SendMessage,
    SendPhoto,
    SendVideo,
    SetMyCommands,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    User,
    Video,
    WebhookInfo,
)

from tests.media_fixtures import MEDIA_ROOT
from tests.test_catalog import product_order, setup_catalog
from tests.test_groups import Scenario
from tests.test_product_access import bind, settle
from vip.bot import build_router
from vip.config import Settings
from vip.models import RecoveryTask

ROOT = Path(__file__).resolve().parents[1]
MAIN_LABELS = [["▶️ Vídeo Exemplo", "💎 VIP Permanente"], ["🎟️ Acessar VIP", "💬 Suporte"]]
EXPECTED_START_CAPTION = (
    "<b>Bem-vindo ao catálogo de exemplo.</b>\nEscolha um produto ou consulte seu acesso."
)


class UITransport(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests, self.messages = [], {}
        self.rejected_edit = None

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="Bot", username="example_fulfillment_bot")
        if isinstance(method, GetWebhookInfo):
            return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        if isinstance(method, (AnswerCallbackQuery, SetMyCommands)):
            return True
        if isinstance(method, (SendPhoto, SendVideo, SendMessage)):
            mid = 100 + len(self.messages)
            message = Message(
                message_id=mid,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=User(id=999, is_bot=True, first_name="Bot"),
                text=method.text if isinstance(method, SendMessage) else None,
                caption=method.caption if isinstance(method, (SendPhoto, SendVideo)) else None,
                video=Video(
                    file_id="synthetic-video",
                    file_unique_id="video-unique",
                    width=720,
                    height=1280,
                    duration=4,
                )
                if isinstance(method, SendVideo)
                else None,
                photo=[
                    PhotoSize(
                        file_id="synthetic", file_unique_id="synthetic", width=992, height=1178
                    )
                ]
                if isinstance(method, SendPhoto)
                else None,
            )
            self.messages[mid] = message
            return message
        if isinstance(method, (EditMessageCaption, EditMessageText, EditMessageReplyMarkup)):
            if self.rejected_edit:
                raise TelegramBadRequest(method, self.rejected_edit)
            return True
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("No external content")
        yield b""


class UI:
    def __init__(self, db, *, settings=None, access=None, payments=None):
        self.transport = UITransport()
        self.bot = Bot(token="999:synthetic-test-token", session=self.transport)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(
            build_router(settings or Settings(_env_file=None), db, access=access, payments=payments)
        )

    async def text(self, text, user_id=123):
        from aiogram.types import Update

        await self.dispatcher.feed_update(
            self.bot,
            Update(
                update_id=len(self.transport.requests) + 1,
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=user_id, type="private"),
                    from_user=User(id=user_id, is_bot=False, first_name="Buyer"),
                    text=text,
                    entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                    if text.startswith("/")
                    else [],
                ),
            ),
        )

    async def click(self, data, *, user_id=123, chat_id=123):
        from aiogram.types import Update

        source = next(reversed(self.transport.messages.values())).model_copy(
            update={"chat": Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup")}
        )
        await self.dispatcher.feed_update(
            self.bot,
            Update(
                update_id=len(self.transport.requests) + 1,
                callback_query=CallbackQuery(
                    id="synthetic-query",
                    chat_instance="synthetic",
                    from_user=User(id=user_id, is_bot=False, first_name="Buyer"),
                    message=source,
                    data=data,
                ),
            ),
        )

    def screen(self):
        for method in reversed(self.transport.requests):
            if isinstance(method, (SendMessage, EditMessageText)):
                return method.text
            if isinstance(method, (SendPhoto, SendVideo, EditMessageCaption)):
                return method.caption
        raise AssertionError("No screen")

    def keyboard(self):
        return next(
            method.reply_markup
            for method in reversed(self.transport.requests)
            if getattr(method, "reply_markup", None)
            and hasattr(method.reply_markup, "inline_keyboard")
        )


@pytest.mark.parametrize("banner", ["official", "missing", "disabled"])
async def test_start_is_one_message_with_exact_inline_menu_and_optional_local_photo(
    db, tmp_path, banner
):
    path = (
        MEDIA_ROOT / "example-store-start.jpg" if banner == "official" else tmp_path / "absent.jpg"
    )
    ui = UI(
        db,
        settings=Settings(_env_file=None, start_banner_path=path if banner != "disabled" else None),
    )
    await ui.text("/start")
    assert ui.screen() == EXPECTED_START_CAPTION
    assert [[button.text for button in row] for row in ui.keyboard().inline_keyboard] == MAIN_LABELS
    assert all(
        button.callback_data and not button.url
        for row in ui.keyboard().inline_keyboard
        for button in row
    )
    sends = [m for m in ui.transport.requests if isinstance(m, (SendMessage, SendPhoto))]
    assert len(sends) == 1
    assert sends[0].parse_mode == "HTML"
    assert isinstance(sends[0].reply_markup, InlineKeyboardMarkup)
    assert [[b.text for b in row] for row in sends[0].reply_markup.inline_keyboard] == MAIN_LABELS
    assert [[b.callback_data for b in row] for row in sends[0].reply_markup.inline_keyboard] == [
        ["menu:sample", "menu:vip"],
        ["menu:access", "menu:support"],
    ]
    assert not any(isinstance(m, EditMessageReplyMarkup) for m in ui.transport.requests)
    if banner == "official":
        assert isinstance(sends[0], SendPhoto)
        assert Path(sends[0].photo.path) == path
    else:
        assert isinstance(sends[0], SendMessage)


@pytest.mark.parametrize("banner", [None, MEDIA_ROOT / "example-store-start.jpg"])
async def test_start_does_not_depend_on_a_second_keyboard_attachment(db, banner):
    ui = UI(db, settings=Settings(_env_file=None, start_banner_path=banner))
    # Reproduce the old partial-delivery failure: sending succeeds, but attaching
    # the keyboard in a second API call would be rejected by Telegram.
    ui.transport.rejected_edit = "Bad Request: message can't be edited"
    await ui.text("/start")
    sends = [m for m in ui.transport.requests if isinstance(m, (SendPhoto, SendMessage))]
    assert len(sends) == 1
    assert isinstance(sends[0].reply_markup, InlineKeyboardMarkup)
    assert [[b.text for b in row] for row in sends[0].reply_markup.inline_keyboard] == MAIN_LABELS
    assert not any(isinstance(m, EditMessageReplyMarkup) for m in ui.transport.requests)


@pytest.mark.parametrize("failure", ["telegram", "file"])
async def test_rejected_photo_falls_back_to_one_text_message_with_inline_buttons(db, failure):
    ui = UI(
        db,
        settings=Settings(_env_file=None, start_banner_path=MEDIA_ROOT / "example-store-start.jpg"),
    )
    original = ui.transport.make_request

    async def reject_photo(bot, method, timeout=None):
        if isinstance(method, SendPhoto):
            ui.transport.requests.append(method)
            if failure == "file":
                raise OSError("Synthetic unreadable image")
            raise TelegramBadRequest(method, "Bad Request: synthetic photo rejected")
        return await original(bot, method, timeout)

    ui.transport.make_request = reject_photo
    await ui.text("/start")
    assert len(ui.transport.messages) == 1
    delivered = next(m for m in ui.transport.requests if isinstance(m, SendMessage))
    assert delivered.text == EXPECTED_START_CAPTION
    assert isinstance(delivered.reply_markup, InlineKeyboardMarkup)
    assert [[b.text for b in row] for row in delivered.reply_markup.inline_keyboard] == MAIN_LABELS
    assert not any(isinstance(m, EditMessageReplyMarkup) for m in ui.transport.requests)


@pytest.mark.parametrize("banner", [None, MEDIA_ROOT / "example-store-start.jpg"])
@pytest.mark.parametrize(
    ("callback", "expected"),
    [
        ("menu:previous", "Vídeo Exemplo"),
        ("menu:vip", "VIP Permanente"),
        ("menu:access", "Seu acesso VIP ainda não está ativo"),
        ("menu:support", "Suporte"),
    ],
)
async def test_all_main_callbacks_and_return_keep_inline_markup_on_the_same_message(
    db, banner, callback, expected
):
    ui = UI(db, settings=Settings(_env_file=None, start_banner_path=banner))
    await ui.text("/start")
    message_id = next(iter(ui.transport.messages))
    await ui.click(callback)
    text_only = bool(banner) and callback in {"menu:vip", "menu:access"}
    edit_type = EditMessageCaption if banner else EditMessageText
    screen = next(
        m
        for m in reversed(ui.transport.requests)
        if isinstance(m, SendMessage if text_only else edit_type)
    )
    assert expected in (screen.caption if banner and not text_only else screen.text)
    if not text_only:
        assert screen.message_id == message_id
    assert isinstance(screen.reply_markup, InlineKeyboardMarkup)
    assert any(
        b.callback_data == "menu:home" for row in screen.reply_markup.inline_keyboard for b in row
    )
    await ui.click("menu:home")
    home = next(
        m
        for m in reversed(ui.transport.requests)
        if isinstance(m, SendPhoto if text_only else edit_type)
    )
    if not text_only:
        assert home.message_id == message_id
    assert (home.caption if banner else home.text) == EXPECTED_START_CAPTION
    assert isinstance(home.reply_markup, InlineKeyboardMarkup)
    assert [[b.text for b in row] for row in home.reply_markup.inline_keyboard] == MAIN_LABELS
    assert len([m for m in ui.transport.requests if isinstance(m, (SendPhoto, SendMessage))]) == (
        3 if text_only else 1
    )


@pytest.mark.parametrize("banner", [None, MEDIA_ROOT / "example-store-start.jpg"])
async def test_navigation_reuses_message_but_new_start_creates_one_fresh_menu(db, banner):
    ui = UI(db, settings=Settings(_env_file=None, start_banner_path=banner))
    await ui.text("/start")
    await ui.click("menu:previous")
    assert "R$ 10,00" in ui.screen()
    await ui.click("menu:home")
    await ui.text("/start")
    assert len([m for m in ui.transport.requests if isinstance(m, (SendPhoto, SendMessage))]) == 2
    assert any(
        isinstance(m, EditMessageCaption if banner else EditMessageText)
        for m in ui.transport.requests
    )
    assert len([m for m in ui.transport.requests if isinstance(m, AnswerCallbackQuery)]) == 2


async def test_existing_photo_menu_remains_editable_after_router_restart(db):
    settings = Settings(_env_file=None, start_banner_path=MEDIA_ROOT / "example-store-start.jpg")
    ui = UI(db, settings=settings)
    await ui.text("/start")
    ui.dispatcher = Dispatcher()
    ui.dispatcher.include_router(build_router(settings, db))
    await ui.click("menu:vip")
    assert "R$ 20,00" in ui.screen()
    await ui.click("menu:home")
    assert ui.screen() == EXPECTED_START_CAPTION
    assert len([m for m in ui.transport.requests if isinstance(m, SendPhoto)]) == 2


async def test_previous_requires_payment_and_binding_but_does_not_invent_video_delivery(db):
    setup_catalog(db)
    ui = UI(db)
    await ui.text("/previous")
    assert "Vídeo Exemplo" in ui.screen() and "R$ 10,00" in ui.screen()
    assert not any(
        b.callback_data == "menu:link" for row in ui.keyboard().inline_keyboard for b in row
    )
    raw = settle(db, product_order(db))
    await ui.text("/previous")
    assert "Compra confirmada" not in ui.screen()
    bind(db, raw)
    await ui.text("/previous")
    assert "Compra confirmada" in ui.screen() and "temporariamente indisponível" in ui.screen()
    assert not any(b.url for row in ui.keyboard().inline_keyboard for b in row)


async def test_vip_product_reuses_permanent_permission_without_an_expiry(db):
    setup_catalog(db)
    ui = UI(db, settings=Settings(_env_file=None, vip_chat_id=-100123))
    await ui.text("/vip")
    assert "R$ 20,00" in ui.screen() and "Pagamento único" in ui.screen()
    bind(db, settle(db, product_order(db, "vip_permanent")))
    await ui.text("/vip")
    assert "ativo" in ui.screen()
    assert any(
        b.callback_data == "menu:access" for row in ui.keyboard().inline_keyboard for b in row
    )


async def test_access_callback_uses_clicking_user_not_the_bot_or_another_buyer(db):
    s = Scenario(db)
    s.paid(123)
    ui = UI(db, settings=s.settings, access=s.service)
    await ui.text("/start")
    await ui.click("menu:access")
    assert "https://t.me/" in ui.screen()
    # The same callback data cannot convey the first user's identity to a third party.
    await ui.text("/start", user_id=456)
    await ui.click("menu:access", user_id=456, chat_id=456)
    assert "Seu acesso VIP ainda não está ativo" in ui.screen()
    assert len(s.telegram.invites) == 1
    await ui.click("menu:link", user_id=456, chat_id=456)
    assert "/vincular" in ui.screen() and "Telegram ID" not in ui.screen()


async def test_support_uses_only_configured_contact_and_no_development_copy(db):
    ui = UI(
        db,
        settings=Settings(
            _env_file=None, support_contact="Contato de teste informado pelo operador"
        ),
    )
    await ui.text("/start")
    await ui.click("menu:support")
    assert "Contato de teste informado pelo operador" in ui.screen()
    assert "configur" not in ui.screen().lower()


async def test_callbacks_from_groups_or_a_different_private_chat_cannot_issue_invites(db):
    s = Scenario(db)
    s.paid()
    ui = UI(db, settings=s.settings, access=s.service)
    await ui.text("/start")
    await ui.click("menu:access", chat_id=-100123)
    await ui.click("menu:access", user_id=456, chat_id=123)
    assert s.telegram.invites == {}


async def test_public_command_registration_exposes_only_three_commands():
    from vip.bot_ui import configure_public_commands

    transport = UITransport()
    async with Bot(token="999:synthetic-test-token", session=transport) as bot:
        await configure_public_commands(bot)
    registrations = [m for m in transport.requests if isinstance(m, SetMyCommands)]
    assert registrations
    assert all(
        [(c.command, c.description) for c in m.commands]
        == [("start", "Início"), ("sample", "Vídeo Exemplo"), ("vip", "VIP Permanente")]
        for m in registrations
    )


async def test_menu_does_not_enable_recovery_or_change_the_original_start_schedule(db):
    ui = UI(db)
    await ui.text("/start")
    with db.transaction() as session:
        task = session.get(RecoveryTask, 123)
        assert task.due_at - task.first_started_at == 1200
    assert not Settings(_env_file=None).recovery_enabled


async def test_contextual_binding_reuses_the_existing_single_use_token_flow(db):
    setup_catalog(db)
    raw = settle(db, product_order(db))
    ui = UI(db)
    await ui.text("/start")
    await ui.click("menu:previous")
    assert not any(
        b.callback_data == "menu:link" for row in ui.keyboard().inline_keyboard for b in row
    )
    # Legacy/support recovery callback stays functional without appearing on the product screen.
    await ui.click("menu:link")
    assert "/vincular" in ui.screen()
    await ui.text("/vincular " + raw)
    assert "Compra vinculada" in ui.screen()
    assert "automação" not in ui.screen().lower() and "vencimento" not in ui.screen().lower()
    await ui.text("/previous")
    assert "Compra confirmada" in ui.screen()
    await ui.text("/vincular " + raw, user_id=456)
    assert "já utilizado" in ui.screen() and raw not in ui.screen()


async def test_reopening_unchanged_menu_does_not_duplicate_a_message(db):
    ui = UI(db)
    await ui.text("/start")
    ui.transport.rejected_edit = "Bad Request: message is not modified"
    await ui.click("menu:home")
    assert len(ui.transport.messages) == 1


async def test_deleted_menu_can_be_replaced_once(db):
    ui = UI(db)
    await ui.text("/start")
    ui.transport.rejected_edit = "Bad Request: message to edit not found"
    # Reject only the stale edit; attaching buttons to the newly sent message succeeds.
    original = ui.transport.make_request

    async def reject_old(bot, method, timeout=None):
        ui.transport.rejected_edit = (
            "Bad Request: message to edit not found"
            if isinstance(method, EditMessageText)
            else None
        )
        return await original(bot, method, timeout)

    ui.transport.make_request = reject_old
    await ui.text("/start")
    assert len(ui.transport.messages) == 2
    assert [[b.text for b in row] for row in ui.keyboard().inline_keyboard] == MAIN_LABELS


async def test_support_without_contact_does_not_invent_one_or_expose_configuration(db):
    ui = UI(db)
    await ui.text("/suporte")
    assert "temporariamente indisponível" in ui.screen()
    assert "configur" not in ui.screen().lower() and "@" not in ui.screen()


async def test_polling_subscribes_to_callbacks_and_registers_only_public_commands(db, monkeypatch):
    import vip.bot as bot_module

    transport = UITransport()
    settings = Settings(
        _env_file=None,
        telegram_bot_token="999:synthetic-test-token",
        database_url=str(db.engine.url),
    )
    monkeypatch.setattr(bot_module, "Settings", lambda: settings)
    monkeypatch.setattr(bot_module, "Bot", lambda token: Bot(token, session=transport))
    observed = []

    async def polling(_dispatcher, _bot, *, allowed_updates):
        observed.extend(allowed_updates)

    monkeypatch.setattr(Dispatcher, "start_polling", polling)
    await bot_module.run_bot()
    assert "callback_query" in observed and "message" in observed
    registrations = [m for m in transport.requests if isinstance(m, SetMyCommands)]
    assert all([c.command for c in m.commands] == ["start", "sample", "vip"] for m in registrations)
