import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import FSInputFile
from sqlalchemy import event

from tests.media_fixtures import MEDIA_ROOT
from tests.test_bot_ui import UI
from vip.bot_media import BannerPhoto
from vip.config import Settings

PHOTO = MEDIA_ROOT / "example-store-start.jpg"


def photo_ui(db):
    return UI(db, settings=Settings(_env_file=None, start_banner_path=PHOTO))


async def test_banner_reuses_file_id_for_other_users_and_after_router_restart(db):
    ui = photo_ui(db)
    await ui.text("/start")
    await ui.text("/start", user_id=456)
    photos = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert isinstance(photos[0].photo, FSInputFile)
    assert photos[1].photo == "synthetic"
    restarted = photo_ui(db)
    await restarted.text("/start", user_id=789)
    assert (
        next(m for m in restarted.transport.requests if isinstance(m, SendPhoto)).photo
        == "synthetic"
    )


async def test_start_does_not_query_financial_or_recovery_tables_before_response(db):
    ui = photo_ui(db)
    queries = []
    before = []
    original = ui.transport.make_request

    def capture(_connection, _cursor, statement, *_args):
        queries.append(statement.lower())

    async def deliver(bot, method, timeout=None):
        if isinstance(method, SendPhoto):
            before.extend(queries)
        return await original(bot, method, timeout)

    event.listen(db.engine, "before_cursor_execute", capture)
    ui.transport.make_request = deliver
    try:
        await ui.text("/start")
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert before
    for table in ("payments", "orders", "omega_charges", "access_grants", "recovery_tasks"):
        assert not any(table in statement for statement in before), table
    # Existing recovery persistence is preserved, just outside the first-response path.
    assert any("recovery_tasks" in statement for statement in queries)


async def test_invalid_cached_photo_recovers_by_uploading_local_photo(db):
    ui = photo_ui(db)
    await ui.text("/start")
    original = ui.transport.make_request
    attempts = []

    async def reject_stale(bot, method, timeout=None):
        if isinstance(method, SendPhoto):
            attempts.append(method.photo)
            if isinstance(method.photo, str):
                raise TelegramBadRequest(
                    method, "Bad Request: wrong file identifier/HTTP URL specified"
                )
        return await original(bot, method, timeout)

    ui.transport.make_request = reject_stale
    await ui.text("/start", user_id=456)
    assert attempts[0] == "synthetic"
    assert isinstance(attempts[1], FSInputFile)
    assert len(attempts) == 2


async def test_uncertain_network_failure_does_not_retry_or_send_duplicate_text(db):
    ui = photo_ui(db)
    await ui.text("/start")
    original = ui.transport.make_request
    attempts = []

    async def unavailable(bot, method, timeout=None):
        if isinstance(method, (SendPhoto, SendMessage)):
            attempts.append(method)
            raise TelegramNetworkError(method, "synthetic timeout")
        return await original(bot, method, timeout)

    ui.transport.make_request = unavailable
    await ui.text("/start", user_id=456)
    assert len(attempts) == 1
    assert attempts[0].photo == "synthetic"


@pytest.mark.parametrize("page", ["previous", "vip", "access", "support"])
async def test_callback_acknowledgement_precedes_db_queries_and_reuses_cached_photo(db, page):
    ui = photo_ui(db)
    await ui.text("/start")
    sequence = []
    original = ui.transport.make_request

    def capture(*_args):
        sequence.append("database")

    async def measured(bot, method, timeout=None):
        sequence.append("ack" if isinstance(method, AnswerCallbackQuery) else type(method).__name__)
        if isinstance(method, SendPhoto):
            assert isinstance(method.photo, str)  # Media switches reuse Telegram file_id.
        return await original(bot, method, timeout)

    event.listen(db.engine, "before_cursor_execute", capture)
    ui.transport.make_request = measured
    try:
        await ui.click("menu:" + page)
        await ui.click("menu:home")
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert sequence[0] == "ack"


async def test_cache_storage_failure_still_reuses_process_file_id(db, monkeypatch, caplog):
    def fail(*_args):
        raise RuntimeError("fake-private-connection-string")

    monkeypatch.setattr(BannerPhoto, "_read", fail)
    monkeypatch.setattr(BannerPhoto, "_write", fail)
    ui = photo_ui(db)
    await ui.text("/start")
    await ui.text("/start", user_id=456)
    photos = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert isinstance(photos[0].photo, FSInputFile)
    assert photos[1].photo == "synthetic"
    assert "fake-private" not in caplog.text


async def test_other_bot_and_changed_asset_never_reuse_wrong_file_id(db, tmp_path):
    from aiogram import Bot

    ui = photo_ui(db)
    await ui.text("/start")
    another = photo_ui(db)
    another.bot = Bot(token="777:synthetic-other-bot", session=another.transport)
    await another.text("/start", user_id=456)
    assert isinstance(another.transport.requests[0].photo, FSInputFile)
    different_photo = tmp_path / "changed.jpg"
    different_photo.write_bytes(b"synthetic-test-image")
    changed = UI(db, settings=Settings(_env_file=None, start_banner_path=different_photo))
    await changed.text("/start", user_id=789)
    assert isinstance(changed.transport.requests[0].photo, FSInputFile)


async def test_concurrent_cold_starts_upload_once_and_warm_sends_are_not_serialized(db):
    import asyncio

    ui = photo_ui(db)
    await asyncio.gather(*(ui.text("/start", user_id=n) for n in range(100, 105)))
    photos = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert sum(isinstance(m.photo, FSInputFile) for m in photos) == 1
    original = ui.transport.make_request
    both_entered = asyncio.Event()
    entered = 0

    async def hold_warm(bot, method, timeout=None):
        nonlocal entered
        if isinstance(method, SendPhoto):
            entered += 1
            if entered == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=2)
        return await original(bot, method, timeout)

    ui.transport.make_request = hold_warm
    await asyncio.gather(ui.text("/start", user_id=600), ui.text("/start", user_id=700))
    assert entered == 2


async def test_after_reply_persistence_remains_awaited_and_failures_are_sanitized(
    db, monkeypatch, caplog
):
    import vip.bot as bot_module

    def fail_after_reply(*_args, **_kwargs):
        from sqlalchemy.exc import OperationalError

        assert ui.transport.messages  # UI arrived before secondary work.
        raise OperationalError("fake-sensitive-sql", {}, Exception("fake-secret"))

    monkeypatch.setattr(bot_module, "record_start", fail_after_reply)
    ui = photo_ui(db)
    await ui.text("/start")
    assert "Start contact persistence unavailable" in caplog.text
    assert "fake-sensitive" not in caplog.text and "fake-secret" not in caplog.text


async def test_callback_ack_network_failure_does_not_send_another_photo(db):
    ui = photo_ui(db)
    await ui.text("/start")
    requests_before = len(ui.transport.requests)
    original = ui.transport.make_request

    async def fail_ack(bot, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            raise TelegramNetworkError(method, "synthetic timeout")
        return await original(bot, method, timeout)

    ui.transport.make_request = fail_ack
    with pytest.raises(TelegramNetworkError):
        await ui.click("menu:support")
    assert len(ui.transport.requests) == requests_before
