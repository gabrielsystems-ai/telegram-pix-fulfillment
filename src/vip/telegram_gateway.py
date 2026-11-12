import asyncio
from typing import Protocol

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage, TelegramMethod
from pydantic import SecretStr


class GroupError(Exception):
    def __init__(self, code: str, *, retry_after: int = 30):
        self.code = code
        # A server-provided RetryAfter must never be shortened by a local upper bound.
        self.retry_after = max(5, retry_after)
        super().__init__(code)


class GroupGateway(Protocol):
    def call(self, method: TelegramMethod): ...


class AiogramGroupGateway:
    """Synchronous worker adapter for the existing bot identity, never another bot.

    Each call owns its HTTP session/event loop on the worker thread. The polling
    Bot session is never shared across event loops. No method/body/token is logged.
    """

    def __init__(self, token: SecretStr):
        self._token = token

    def call(self, method: TelegramMethod):
        async def invoke():
            async with Bot(
                self._token.get_secret_value(), session=AiohttpSession(timeout=10)
            ) as bot:
                return await bot(method)

        try:
            return asyncio.run(invoke())
        except TelegramRetryAfter as error:
            raise GroupError("telegram_rate_limited", retry_after=int(error.retry_after)) from None
        except TelegramForbiddenError:
            raise GroupError("telegram_forbidden") from None
        except (TelegramAPIError, TimeoutError, OSError):
            raise GroupError("telegram_unavailable") from None


class AiogramRecoverySender:
    def __init__(self, gateway):
        self.gateway = gateway

    def send(self, telegram_id, text):
        from vip.recovery import DeliveryError

        try:
            message = self.gateway.call(
                SendMessage(chat_id=telegram_id, text=text, parse_mode=None)
            )
            return message.message_id
        except GroupError as error:
            kind = {"telegram_forbidden": "blocked", "telegram_rate_limited": "retry"}.get(
                error.code, "ambiguous"
            )
            raise DeliveryError(kind, retry_after=error.retry_after) from None
