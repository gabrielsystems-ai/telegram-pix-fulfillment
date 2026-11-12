from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageReplyMarkup, SendMessage
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from vip.bot import build_router
from vip.config import Settings
from vip.linking import issue_link_token
from vip.models import User
from vip.orders import create_order
from vip.payments.contracts import VerifiedPayment
from vip.payments.service import record_verified_payment
from vip.plans import create_plan


class TelegramTransport(BaseSession):
    """Replace only Telegram HTTP transport; real routing, settings and DB remain in use."""

    def __init__(self):
        super().__init__()
        self.responses = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, EditMessageReplyMarkup):
            return True
        if isinstance(method, SendMessage):
            self.responses.append(method.text)
            return Message(
                message_id=99,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        raise AssertionError(f"Unexpected Telegram operation: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("No files should be transferred")
        yield b""  # pragma: no cover


async def dispatch(db, text, *, user_id=123, chat_type="private", admins=""):
    transport = TelegramTransport()
    bot = Bot(token="999:synthetic-test-token", session=transport)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(Settings(_env_file=None, admin_telegram_ids=admins), db))
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id if chat_type == "private" else -100123, type=chat_type),
        from_user=TelegramUser(id=user_id, is_bot=False, first_name="Test"),
        text=text,
        entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if text.startswith("/")
        else [],
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    await bot.session.close()
    return transport.responses


async def test_start_registers_once_and_vip_is_scoped_to_sender(db):
    assert await dispatch(db, "/start")
    assert await dispatch(db, "/start")
    replies = await dispatch(db, "/meuvip")
    assert "Nenhuma assinatura" in replies[0]
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1


async def test_admin_denied_by_default_and_allowed_only_for_configured_id(db):
    assert "restrito" in (await dispatch(db, "/admin"))[0]
    assert "Usuários:" in (await dispatch(db, "/admin", admins="123"))[0]
    assert "restrito" in (await dispatch(db, "/admin", user_id=456, admins="123"))[0]


async def test_group_chat_is_ignored_and_link_failure_does_not_echo_token(db):
    assert await dispatch(db, "/start", chat_type="supergroup") == []
    raw = "A" * 43
    replies = await dispatch(db, f"/vincular {raw}")
    assert replies and raw not in replies[0]
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0


async def test_binding_uses_telegram_sender_and_vip_is_not_visible_to_another_user(db):
    with db.transaction() as session:
        plan = create_plan(
            session,
            name="Synthetic",
            amount_minor=2500,
            duration_days=30,
            telegram_group_id=-100123,
        )
        order = create_order(session, plan.id)
        order.provider_transaction_id = "synthetic-tx"
        record_verified_payment(
            session,
            VerifiedPayment(
                event_id="synthetic-event",
                transaction_id="synthetic-tx",
                order_id=order.id,
                amount_minor=2500,
                currency="BRL",
                status="paid",
            ),
        )
        raw = issue_link_token(session, order.id, ttl_seconds=900)
    replies = await dispatch(db, f"/start link_{raw}", user_id=123)
    assert "Compra vinculada" in replies[0]
    assert "vencimento" in (await dispatch(db, "/meuvip", user_id=123))[0]
    assert "Nenhuma assinatura" in (await dispatch(db, "/meuvip", user_id=456))[0]
    assert "já utilizado" in (await dispatch(db, f"/vincular {raw}", user_id=456))[0]
    # The fake transport rejects every group/invitation/payment API operation.
