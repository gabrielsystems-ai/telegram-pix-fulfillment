import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageCaption, SendMessage, SendPhoto, SendVideo
from aiogram.types import FSInputFile
from sqlalchemy import event

from scripts.prepare_bundle import prepare
from tests.media_fixtures import MEDIA_ROOT
from tests.test_bot_ui import EXPECTED_START_CAPTION, MAIN_LABELS, UI
from vip.config import Settings

VIDEO = MEDIA_ROOT / "example-store-start.mp4"
PHOTO = MEDIA_ROOT / "sample.jpg"


def video_ui(db, *, path=VIDEO):
    return UI(db, settings=Settings(_env_file=None, start_video_path=path, start_banner_path=PHOTO))


async def test_start_sends_video_caption_and_exact_keyboard_atomically(db):
    ui = video_ui(db)
    await ui.text("/start")
    sends = [r for r in ui.transport.requests if isinstance(r, (SendVideo, SendPhoto, SendMessage))]
    assert len(sends) == 1 and isinstance(sends[0], SendVideo)
    sent = sends[0]
    assert isinstance(sent.video, FSInputFile) and sent.caption == EXPECTED_START_CAPTION
    assert sent.parse_mode == "HTML" and sent.supports_streaming is True
    assert [[b.text for b in row] for row in sent.reply_markup.inline_keyboard] == MAIN_LABELS
    assert [[b.callback_data for b in row] for row in sent.reply_markup.inline_keyboard] == [
        ["menu:sample", "menu:vip"],
        ["menu:access", "menu:support"],
    ]


async def test_video_cache_survives_restart_and_does_not_reupload(db):
    ui = video_ui(db)
    await ui.text("/start")
    await ui.text("/start", user_id=456)
    sends = [r for r in ui.transport.requests if isinstance(r, SendVideo)]
    assert isinstance(sends[0].video, FSInputFile) and sends[1].video == "synthetic-video"
    restarted = video_ui(db)
    await restarted.text("/start", user_id=789)
    assert (
        next(r for r in restarted.transport.requests if isinstance(r, SendVideo)).video
        == "synthetic-video"
    )


@pytest.mark.parametrize("failure", ["missing", "rejected"])
async def test_video_failure_preserves_current_photo_and_buttons(db, tmp_path, failure):
    ui = video_ui(db, path=tmp_path / "absent.mp4" if failure == "missing" else VIDEO)
    original = ui.transport.make_request

    async def reject_video(bot, method, timeout=None):
        if isinstance(method, SendVideo):
            raise TelegramBadRequest(method, "Bad Request: VIDEO_CONTENT_TYPE_INVALID")
        return await original(bot, method, timeout)

    ui.transport.make_request = reject_video
    await ui.text("/start")
    photos = [r for r in ui.transport.requests if isinstance(r, SendPhoto)]
    assert len(photos) == 1 and str(photos[0].photo.path) == str(PHOTO)
    assert photos[0].caption == EXPECTED_START_CAPTION
    assert [[b.text for b in row] for row in photos[0].reply_markup.inline_keyboard] == MAIN_LABELS


async def test_invalid_video_file_id_recovers_using_local_mp4(db):
    ui = video_ui(db)
    await ui.text("/start")
    original = ui.transport.make_request

    async def reject_cached(bot, method, timeout=None):
        if isinstance(method, SendVideo) and isinstance(method.video, str):
            raise TelegramBadRequest(method, "wrong file identifier")
        return await original(bot, method, timeout)

    ui.transport.make_request = reject_cached
    await ui.text("/start", user_id=456)
    sends = [r for r in ui.transport.requests if isinstance(r, SendVideo)]
    assert len(sends) == 2 and isinstance(sends[-1].video, FSInputFile)
    assert sends[-1].reply_markup


async def test_replacing_mp4_at_same_path_invalidates_cache_on_restart(db, tmp_path):
    path = tmp_path / "start.mp4"
    path.write_bytes(b"synthetic-v1")
    ui = video_ui(db, path=path)
    await ui.text("/start")
    path.write_bytes(b"synthetic-v2")
    restarted = video_ui(db, path=path)
    await restarted.text("/start")
    assert isinstance(
        next(r for r in restarted.transport.requests if isinstance(r, SendVideo)).video, FSInputFile
    )


async def test_video_cache_never_uses_a_photo_file_id(db):
    photo = UI(db, settings=Settings(_env_file=None, start_banner_path=VIDEO))
    await photo.text("/start")
    ui = video_ui(db)
    await ui.text("/start")
    assert isinstance(
        next(r for r in ui.transport.requests if isinstance(r, SendVideo)).video, FSInputFile
    )


async def test_video_navigation_edits_caption_without_reuploading(db):
    ui = video_ui(db)
    await ui.text("/start")
    for page in ("sample", "vip", "access", "support", "home"):
        await ui.click("menu:" + page)
    videos = [r for r in ui.transport.requests if isinstance(r, SendVideo)]
    assert len(videos) == 2 and videos[-1].video == "synthetic-video"
    assert len([r for r in ui.transport.requests if isinstance(r, EditMessageCaption)]) == 2
    assert ui.screen() == EXPECTED_START_CAPTION


async def test_video_edit_failure_recreates_complete_video_menu(db):
    ui = video_ui(db)
    await ui.text("/start")
    ui.transport.rejected_edit = "Bad Request: message to edit not found"
    await ui.click("menu:support")
    sends = [r for r in ui.transport.requests if isinstance(r, SendVideo)]
    assert len(sends) == 2 and sends[-1].video == "synthetic-video"
    assert sends[-1].reply_markup


async def test_uncertain_video_network_error_does_not_duplicate_with_photo(db):
    ui = video_ui(db)
    original = ui.transport.make_request

    async def timeout_video(bot, method, timeout=None):
        if isinstance(method, SendVideo):
            raise TelegramNetworkError(method, "synthetic timeout")
        return await original(bot, method, timeout)

    ui.transport.make_request = timeout_video
    await ui.text("/start")
    assert not any(isinstance(r, (SendPhoto, SendMessage)) for r in ui.transport.requests)


async def test_video_cold_start_uploads_once_and_has_no_financial_work(db):
    ui = video_ui(db)
    queries, before = [], []
    original = ui.transport.make_request

    def capture(_connection, _cursor, statement, *_args):
        queries.append(statement.lower())

    async def send(bot, method, timeout=None):
        if isinstance(method, SendVideo) and not before:
            # Later users may overlap with the first user's post-response work.
            before.extend(queries)
        return await original(bot, method, timeout)

    ui.transport.make_request = send
    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        await asyncio.gather(*(ui.text("/start", user_id=n) for n in range(100, 105)))
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    sends = [r for r in ui.transport.requests if isinstance(r, SendVideo)]
    assert sum(isinstance(r.video, FSInputFile) for r in sends) == 1
    assert not any(table in q for q in before for table in ("payments", "orders", "omega_charges"))


def test_bundle_excludes_all_private_media(tmp_path):
    files = prepare(tmp_path / "upload")
    assert not any(name.endswith((".mp4", ".jpg")) for name in files)


def test_default_video_path_and_explicit_disable(monkeypatch):

    monkeypatch.delenv("START_VIDEO_PATH")
    assert Settings(_env_file=None).start_video_path is None
    assert Settings(_env_file=None, start_video_path="").start_video_path is None


async def test_warm_video_sends_remain_concurrent(db):
    ui = video_ui(db)
    await ui.text("/start")
    original = ui.transport.make_request
    entered, ready = 0, asyncio.Event()

    async def hold(bot, method, timeout=None):
        nonlocal entered
        if isinstance(method, SendVideo):
            assert method.video == "synthetic-video"
            entered += 1
            if entered == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=2)
        return await original(bot, method, timeout)

    ui.transport.make_request = hold
    await asyncio.gather(ui.text("/start", user_id=456), ui.text("/start", user_id=789))
    assert entered == 2
