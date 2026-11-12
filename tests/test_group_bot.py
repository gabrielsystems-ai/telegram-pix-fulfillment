from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.types import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)

from tests.test_bot import TelegramTransport
from tests.test_groups import CHAT, Scenario
from vip.bot import build_router


async def deliver(s, *, text=None, user_id=123, join=None, member=None, chat_id=None):
    transport = TelegramTransport()
    bot = Bot(token="999:synthetic-test-token", session=transport)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(s.settings, s.db, access=s.service))
    if text:
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id or user_id, type="supergroup" if chat_id else "private"),
            from_user=User(id=user_id, first_name="Synthetic", is_bot=False),
            text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
            if text.startswith("/")
            else [],
        )
        update = Update(update_id=1, message=message)
    elif join:
        update = Update(update_id=2, chat_join_request=join)
    else:
        update = Update(update_id=3, chat_member=member)
    await dispatcher.feed_update(bot, update)
    await bot.session.close()
    return transport.responses


async def test_access_button_and_join_update_use_real_sender_and_existing_router(db):
    s = Scenario(db)
    s.paid()
    replies = await deliver(s, text="Acessar VIP")
    assert "https://t.me/" in replies[0]
    url = next(iter(s.telegram.invites))
    await deliver(s, join=s.request(123, url))
    assert s.telegram.members[123] == "member"


async def test_vip_menu_shows_plan_expiry_and_days_without_leaking_group_id(db):
    s = Scenario(db)
    s.paid()
    reply = (await deliver(s, text="/meuvip"))[0]
    assert "Status:" in reply and "Plano:" in reply
    assert "vencimento" in reply and "Dias restantes:" in reply
    assert str(CHAT) not in reply


async def test_group_diagnostics_and_id_command_are_admin_only(db):
    s = Scenario(db)
    assert "restrito" in (await deliver(s, text="/grupostatus"))[0]
    assert await deliver(s, text="/idgrupo", chat_id=CHAT) == []
    s.settings.admin_telegram_ids = "123"
    assert "Permissões:" in (await deliver(s, text="/grupostatus"))[0]
    assert str(CHAT) in (await deliver(s, text="/idgrupo", chat_id=CHAT))[0]
    assert "123" in (await deliver(s, text="/meuid"))[0]


async def test_membership_update_is_persisted_for_enforcement(db):
    s = Scenario(db)
    user = User(id=123, first_name="Synthetic", is_bot=False)
    s.telegram.members[123] = "member"
    event = ChatMemberUpdated(
        chat=Chat(id=CHAT, type="supergroup"),
        from_user=user,
        date=datetime.now(UTC),
        old_chat_member=ChatMemberLeft(user=user),
        new_chat_member=ChatMemberMember(user=user),
    )
    await deliver(s, member=event)
    s.restarted().sync_once()
    assert s.telegram.members[123] == "left"
