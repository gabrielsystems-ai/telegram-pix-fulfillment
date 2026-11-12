from aiogram.methods import EditMessageCaption, SendPhoto

from tests.media_fixtures import MEDIA_ROOT
from tests.test_bot_ui import UI
from tests.test_catalog import product_order, setup_catalog
from tests.test_product_access import bind, settle
from vip.catalog import OFFERS
from vip.config import Settings


def test_offer_rebrands_without_changing_persistent_codes_or_other_prices():
    assert OFFERS["sample_video"] == ("Vídeo Exemplo", 1000, ("sample_video_access",))
    assert OFFERS["vip_permanent"][1] == 2000
    assert OFFERS["recovery_bundle"][1] == 2500


async def test_start_sends_fresh_cached_photo_instead_of_editing_old_caption(db):
    ui = UI(db, settings=Settings(_env_file=None, start_banner_path=str(MEDIA_ROOT / "sample.jpg")))
    await ui.text("/start")
    ui.transport.rejected_edit = "Bad Request: message is not modified"
    await ui.text("/start")
    photos = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert len(photos) == 2
    assert photos[1].photo == "synthetic"
    assert photos[1].reply_markup == photos[0].reply_markup
    assert not any(isinstance(m, EditMessageCaption) for m in ui.transport.requests)


async def test_old_photo_cache_does_not_hide_new_sample_photo(db):
    from aiogram.types import FSInputFile

    old = UI(
        db,
        settings=Settings(
            _env_file=None, start_banner_path=str(MEDIA_ROOT / "example-store-start.jpg")
        ),
    )
    await old.text("/start")
    new = UI(
        db, settings=Settings(_env_file=None, start_banner_path=str(MEDIA_ROOT / "sample.jpg"))
    )
    await new.text("/start")
    assert isinstance(new.transport.requests[0].photo, FSInputFile)
    assert new.transport.requests[0].photo.path.name == "sample.jpg"


async def test_sample_command_and_legacy_alias_show_current_product(db):
    ui = UI(db)
    await ui.text("/start")
    await ui.click("menu:sample")
    assert "Vídeo Exemplo" in ui.screen() and "R$ 10,00" in ui.screen()
    await ui.text("/sample")
    assert "Vídeo Exemplo" in ui.screen() and "R$ 10,00" in ui.screen()
    await ui.text("/previous")
    assert "Vídeo Exemplo" in ui.screen() and "Anterior" not in ui.screen()


async def test_admin_offer_update_is_restricted_and_does_not_change_group(db):
    from vip.models import Product

    setup_catalog(db)
    with db.transaction() as session:
        session.get(Product, "sample_video").amount_minor = 900
    ui = UI(db, settings=Settings(_env_file=None, admin_telegram_ids="123"))
    await ui.text("/atualizarvideo", user_id=456)
    assert "restrito" in ui.screen()
    with db.transaction() as session:
        assert session.get(Product, "sample_video").amount_minor == 900
    await ui.text("/atualizarvideo", user_id=123)
    with db.transaction() as session:
        assert session.get(Product, "sample_video").amount_minor == 1000
        assert session.get(Product, "vip_permanent").telegram_group_id == -100123


async def test_authorized_video_callback_uses_entitlement_service(db, monkeypatch):
    from tests.test_sample_delivery import Sender, registered
    from vip.video_delivery import TelegramVideoSender

    setup_catalog(db)
    bind(db, settle(db, product_order(db)))
    registered(db)
    sender = Sender()
    monkeypatch.setattr(TelegramVideoSender, "send", sender.send)
    ui = UI(db)
    await ui.text("/sample")
    assert any(
        b.callback_data == "menu:video" for row in ui.keyboard().inline_keyboard for b in row
    )
    await ui.click("menu:video")
    assert "enviado" in ui.screen()
    await ui.click("menu:video")
    assert "já foi entregue" in ui.screen()
    assert len(sender.calls) == 1


async def test_caption_rejection_recreates_atomic_cached_photo_menu(db):
    ui = UI(db, settings=Settings(_env_file=None, start_banner_path=str(MEDIA_ROOT / "sample.jpg")))
    await ui.text("/start")
    ui.transport.rejected_edit = "Bad Request: message can't be edited"
    await ui.click("menu:support")
    sends = [m for m in ui.transport.requests if isinstance(m, SendPhoto)]
    assert len(sends) == 2
    assert sends[-1].photo == "synthetic"
    assert sends[-1].caption and sends[-1].reply_markup


async def test_caption_error_logs_safe_reason_not_private_details(caplog):
    import pytest
    from aiogram.exceptions import TelegramBadRequest

    from vip.bot_diagnostics import PollingDiagnostics

    method = EditMessageCaption(chat_id=123, message_id=1, caption="synthetic")

    async def rejected(_bot, _method):
        raise TelegramBadRequest(
            method, "message can't be edited https://private.invalid/fake-token"
        )

    with pytest.raises(TelegramBadRequest):
        await PollingDiagnostics()(rejected, None, method)
    assert "description=message can't be edited" in caplog.text
    assert "private.invalid" not in caplog.text and "fake-token" not in caplog.text


async def test_video_registration_is_admin_only_and_persists_file_id(db):
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, Update, User, Video

    from vip.video_delivery import configured_video, profile

    expected = profile()
    ui = UI(db, settings=Settings(_env_file=None, admin_telegram_ids="123"))
    for user_id in [456, 123]:
        message = Message(
            message_id=2,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Synthetic"),
            text="/cadastrarvideo",
            entities=[{"type": "bot_command", "offset": 0, "length": 15}],
            reply_to_message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                video=Video(
                    file_id="synthetic-registered",
                    file_unique_id="synthetic",
                    file_size=expected["size"],
                    width=270,
                    height=480,
                    duration=170,
                ),
            ),
        )
        await ui.dispatcher.feed_update(ui.bot, Update(update_id=user_id, message=message))
        assert configured_video(db, 999) == (user_id == 123)


async def test_manual_upload_targets_only_configured_admin_without_polling(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    import pytest

    from scripts import upload_sample as module

    video = tmp_path / "synthetic.mp4"
    video.write_bytes(b"synthetic")
    monkeypatch.setattr(
        module,
        "profile",
        lambda: {
            "size": video.stat().st_size,
            "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        },
    )
    calls = []

    class FakeBot:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_me(self):
            return SimpleNamespace(username="example_fulfillment_bot")

        async def send_video(self, chat_id, photo, **kwargs):
            calls.append(chat_id)

    monkeypatch.setattr(module, "Bot", lambda *args, **kwargs: FakeBot())
    settings = Settings(
        _env_file=None, telegram_bot_token="999:synthetic", admin_telegram_ids="123"
    )
    with pytest.raises(ValueError, match="administrator_required"):
        await module.upload(settings, video, 456)
    assert not calls
    await module.upload(settings, video, 123)
    assert calls == [123]
