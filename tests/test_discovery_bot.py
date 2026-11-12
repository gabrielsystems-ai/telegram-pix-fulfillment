from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.methods import GetMe
from aiogram.types import Chat, Message, Update, User

from tests.test_bot import TelegramTransport
from vip.bot import build_router
from vip.config import Settings


async def test_named_group_diagnostic_works_with_automation_disabled_and_is_admin_only(db):
    class DiscoveryTransport(TelegramTransport):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetMe):
                return User(
                    id=999, is_bot=True, first_name="Synthetic", username="example_fulfillment_bot"
                )
            return await super().make_request(bot, method, timeout)

    transport = DiscoveryTransport()
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(Settings(_env_file=None, admin_telegram_ids="123"), db))
    async with Bot(token="999:synthetic-test-token", session=transport) as bot:
        for user_id in [123, 456]:
            text = "/idgrupo@example_fulfillment_bot"
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=user_id,
                    message=Message(
                        message_id=user_id,
                        date=datetime.now(UTC),
                        chat=Chat(id=-100123, type="supergroup"),
                        from_user=User(id=user_id, is_bot=False, first_name="Synthetic"),
                        text=text,
                        entities=[{"type": "bot_command", "offset": 0, "length": len(text)}],
                    ),
                ),
            )
    assert transport.responses == ["ID deste grupo: -100123"]
