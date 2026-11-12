"""Delivery work inside the existing single-poller process; all jobs live in SQL."""

import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from vip.fulfillment import DeliverySendError, FulfillmentService
from vip.payments.pushinpay import PushinGateway, PushinPayments
from vip.video_delivery import TelegramVideoSender


class TelegramInviteSender:
    def __init__(self, token):
        self.token = token

    def send_invite(self, user_id, url):
        async def invoke():
            async with Bot(
                self.token.get_secret_value(), session=AiohttpSession(timeout=10)
            ) as bot:
                return await bot.send_message(
                    user_id,
                    "Pagamento confirmado. Seu acesso VIP está disponível."
                    + (
                        " Solicite entrada pelo convite abaixo."
                        if url
                        else " Você já está no grupo."
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="Acessar VIP", url=url)]]
                    )
                    if url
                    else None,
                )

        try:
            return asyncio.run(invoke())
        except TelegramRetryAfter as error:
            raise DeliverySendError("rejected", retry_after=int(error.retry_after)) from None
        except (TelegramBadRequest, TelegramForbiddenError):
            raise DeliverySendError("rejected") from None
        except Exception:
            raise DeliverySendError("uncertain") from None


async def run_fulfillment_worker(db, settings, bot_id, access, stop, wake):
    payments = PushinPayments(db, PushinGateway(settings)) if settings.pushinpay_enabled else None
    service = FulfillmentService(
        db,
        bot_id,
        TelegramVideoSender(
            settings.telegram_bot_token,
            caption="Pagamento confirmado. Vídeo Exemplo disponível.",
        ),
        access=access,
        invite_sender=TelegramInviteSender(settings.telegram_bot_token),
    )
    while not stop.is_set():
        if payments:
            try:
                await asyncio.to_thread(payments.process_pending)
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "pushinpay_worker_unavailable exception=%s", type(error).__name__
                )
        try:
            await asyncio.to_thread(service.run_once, stop)
        except Exception as error:
            logging.getLogger(__name__).warning(
                "fulfillment_worker_unavailable exception=%s", type(error).__name__
            )
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(wake.wait(), timeout=3)
            wake.clear()
        except TimeoutError:
            pass
