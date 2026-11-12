from aiogram.methods import GetUpdates, SendPhoto
from aiogram.types import Update

from vip.bot_timing import Timings, UpdateTimings, telegram_timings


async def test_timing_logs_duration_and_error_category_without_sensitive_payload(caplog):
    from aiogram.exceptions import TelegramNetworkError

    method = SendPhoto(chat_id=123, photo="fake-private-file-id", caption="fake-sensitive-caption")

    async def unavailable(bot, method):
        raise TelegramNetworkError(method, "fake-token-in-error")

    try:
        await telegram_timings(unavailable, None, method)
    except TelegramNetworkError:
        pass
    assert "elapsed_ms=" in caplog.text and "network_error" in caplog.text
    assert "fake-" not in caplog.text


async def test_update_timing_separates_external_and_application_duration(caplog):
    async def immediate(_bot, _method):
        return []

    async def handler(_event, _data):
        await telegram_timings(immediate, None, GetUpdates())

    await UpdateTimings()(handler, Update(update_id=987654321), {})
    assert "telegram_ms=" in caplog.text and "application_ms=" in caplog.text
    assert "987654321" not in caplog.text


def test_duration_measurement_is_disabled_by_default(caplog):
    with Timings().measure("synthetic"):
        pass
    assert not caplog.text


async def test_real_routing_timing_preserves_caption_and_hides_link_and_user_data(db, caplog):
    from tests.test_bot_ui import UI
    from vip.config import Settings

    ui = UI(db, settings=Settings(_env_file=None, bot_timing_enabled=True))
    ui.bot.session.middleware(telegram_timings)
    ui.dispatcher.update.outer_middleware(UpdateTimings())
    await ui.text("/start", user_id=987654321)
    await ui.click("menu:support", user_id=987654321, chat_id=987654321)
    assert "stage=start.registration" in caplog.text
    assert "stage=start.after_reply" in caplog.text
    assert "method=answerCallbackQuery" in caplog.text
    assert "telegram_ms=" in caplog.text
    assert "987654321" not in caplog.text
    assert "synthetic-test-token" not in caplog.text
    assert "Você não chegou" not in caplog.text
