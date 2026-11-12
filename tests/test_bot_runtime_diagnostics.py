import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe, GetUpdates, GetWebhookInfo
from aiogram.types import User, WebhookInfo

from tests.test_bot_ui import UITransport
from vip.bot_diagnostics import PollingDiagnostics, SafeFetchLog, inspect_telegram, processing_error
from vip.config import Settings


class RuntimeTransport(UITransport):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            self.requests.append(method)
            return User(id=999, is_bot=True, first_name="Test", username="example_fulfillment_bot")
        if isinstance(method, GetWebhookInfo):
            self.requests.append(method)
            return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
        return await super().make_request(bot, method, timeout)


async def test_runtime_announces_identity_webhook_and_polling_entry_without_starting_polling(
    db, monkeypatch, caplog
):
    import vip.bot as module

    transport = RuntimeTransport()
    settings = Settings(
        _env_file=None, telegram_bot_token="999:synthetic-token", database_url=str(db.engine.url)
    )
    monkeypatch.setattr(module, "Settings", lambda: settings)
    monkeypatch.setattr(module, "Bot", lambda token: Bot(token, session=transport))
    reached = []
    delivery_lifecycle = []

    async def delivery_worker(database, config, bot_id, access, stop, wake):
        import asyncio

        delivery_lifecycle.append((bot_id, access))
        while not stop.is_set():
            await asyncio.sleep(0)
        delivery_lifecycle.append("stopped")

    monkeypatch.setattr(module, "run_fulfillment_worker", delivery_worker)

    async def simulated_polling(dispatcher, bot, *, allowed_updates):
        from datetime import UTC, datetime

        from aiogram.types import Chat, Message, Update

        assert "message" in allowed_updates
        reached.append(True)
        assert "bot_identity username=example_fulfillment_bot bot_id=999" in caplog.text
        assert "telegram_webhook active=false" in caplog.text
        assert "polling_start_requested" in caplog.text
        await dispatcher.feed_update(
            bot,
            Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=123, type="private"),
                    from_user=User(id=123, is_bot=False, first_name="Synthetic"),
                    text="/start",
                    entities=[{"type": "bot_command", "offset": 0, "length": 6}],
                ),
            ),
        )
        assert "start_handler_entered" in caplog.text
        assert "start_menu_attempt_finished" in caplog.text

    monkeypatch.setattr(Dispatcher, "start_polling", simulated_polling)
    await module.run_bot()
    assert reached
    assert delivery_lifecycle == [(999, None), "stopped"]
    assert "polling_finished" in caplog.text
    assert "synthetic-token" not in caplog.text
    assert not any(isinstance(x, GetUpdates) for x in transport.requests)


async def test_start_handler_logs_entry_without_personal_data(db, caplog):
    from tests.test_bot_ui import UI

    ui = UI(db)
    await ui.text("/start", user_id=987654321)
    assert "start_handler_entered" in caplog.text
    assert "987654321" not in caplog.text


async def test_webhook_probe_never_logs_private_url_or_deletes_webhook(caplog):
    transport = RuntimeTransport()
    original = transport.make_request

    async def active_webhook(bot, method, timeout=None):
        if isinstance(method, GetWebhookInfo):
            return WebhookInfo(
                url="https://private.invalid/fake-token",
                has_custom_certificate=False,
                pending_update_count=3,
            )
        return await original(bot, method, timeout)

    transport.make_request = active_webhook
    async with Bot("999:synthetic-token", session=transport) as bot:
        await inspect_telegram(bot)
    assert "telegram_webhook active=true" in caplog.text
    assert "private.invalid" not in caplog.text and "fake-token" not in caplog.text
    assert all(isinstance(m, (GetMe, GetWebhookInfo)) for m in transport.requests)


async def test_polling_receiver_distinguishes_request_and_success_without_exposing_updates(caplog):
    diagnostic = PollingDiagnostics()

    async def updates(_bot, _method):
        return ["fake-private-update"]

    await diagnostic(updates, None, GetUpdates())
    await diagnostic(updates, None, GetUpdates())
    assert caplog.text.count("polling_receive_started") == 1
    assert caplog.text.count("polling_receive_confirmed") == 1
    assert "telegram_updates_received count=1" in caplog.text
    assert "fake-private-update" not in caplog.text


async def test_receive_failure_logs_only_exception_class(caplog):
    import pytest

    method = GetUpdates()

    async def unavailable(_bot, _method):
        raise TelegramNetworkError(method, "https://private.invalid/fake-token")

    with pytest.raises(TelegramNetworkError):
        await PollingDiagnostics()(unavailable, None, method)
    assert "exception=TelegramNetworkError" in caplog.text
    assert "fake-token" not in caplog.text


def test_aiogram_receive_loop_error_message_is_sanitized():
    error = TelegramNetworkError(GetUpdates(), "https://private.invalid/fake-token")
    record = logging.LogRecord(
        "aiogram.dispatcher",
        logging.ERROR,
        "synthetic",
        1,
        "Failed to fetch updates - %s: %s",
        (type(error).__name__, error),
        None,
    )
    assert SafeFetchLog().filter(record)
    assert record.getMessage() == "Failed to fetch updates exception=TelegramNetworkError"


async def test_processing_failure_never_logs_exception_payload(caplog):
    from types import SimpleNamespace

    assert await processing_error(SimpleNamespace(exception=RuntimeError("fake-private-error")))
    assert "telegram_update_processing_failed exception=RuntimeError" in caplog.text
    assert "fake-private-error" not in caplog.text
