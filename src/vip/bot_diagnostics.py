"""Safe lifecycle visibility at WARNING, without enabling payload/debug logging."""

import logging

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetUpdates

LOG = logging.getLogger(__name__)


def safe_rejection(error):
    # Canonical fragments only: never echo arbitrary Telegram text or interpolated data.
    for description in (
        "message is not modified",
        "message to edit not found",
        "message can't be edited",
        "message caption is too long",
        "can't parse entities",
        "button_data_invalid",
    ):
        if description in error.message.lower():
            return description
    return "unrecognized rejection; details suppressed"


async def inspect_telegram(bot):
    LOG.warning("telegram_identity_check_started")
    identity = await bot.me()
    LOG.warning(
        "bot_identity username=%s bot_id=%d", identity.username or "unavailable", identity.id
    )
    LOG.warning("telegram_webhook_check_started")
    webhook = await bot.get_webhook_info()
    LOG.warning(
        "telegram_webhook active=%s pending_updates=%d",
        str(bool(webhook.url)).lower(),
        webhook.pending_update_count,
    )
    if webhook.url:
        LOG.warning("telegram_webhook_conflicts_with_polling; no webhook was deleted")


class PollingDiagnostics:
    def __init__(self):
        self.requested = self.confirmed = False

    async def __call__(self, make_request, bot, method):
        polling = isinstance(method, GetUpdates)
        if polling and not self.requested:
            LOG.warning("polling_receive_started")
            self.requested = True
        try:
            result = await make_request(bot, method)
        except Exception as error:
            if (
                isinstance(error, TelegramBadRequest)
                and method.__api_method__ == "editMessageCaption"
            ):
                description = safe_rejection(error)
                LOG.warning(
                    "telegram_caption_edit_rejected description=%s noop=%s",
                    description,
                    str(description == "message is not modified").lower(),
                )
                raise
            LOG.error(
                "telegram_request_failed method=%s exception=%s",
                method.__api_method__,
                type(error).__name__,
            )
            raise
        if polling:
            if not self.confirmed:
                LOG.warning("polling_receive_confirmed")
                self.confirmed = True
            if result:
                LOG.warning("telegram_updates_received count=%d", len(result))
        return result


class UpdateDiagnostics(BaseMiddleware):
    async def __call__(self, handler, event, data):
        kind = "message" if event.message else "callback" if event.callback_query else "other"
        LOG.warning("telegram_update_dispatch_started kind=%s", kind)
        result = await handler(event, data)
        LOG.warning(
            "telegram_update_dispatch_finished handled=%s", str(result is not UNHANDLED).lower()
        )
        return result


async def processing_error(event):
    # Equivalent to aiogram's default consumed-error behavior, with no traceback/input echo.
    LOG.error("telegram_update_processing_failed exception=%s", type(event.exception).__name__)
    return True


async def dispatcher_started(**_kwargs):
    LOG.warning("dispatcher_startup_reached")


class SafeFetchLog(logging.Filter):
    def filter(self, record):
        if record.msg == "Failed to fetch updates - %s: %s" and isinstance(record.args, tuple):
            # aiogram's receive loop would otherwise stringify HTTP/network exceptions.
            record.msg = "Failed to fetch updates exception=%s"
            record.args = (type(record.args[1]).__name__,)
            record.exc_info = None
        return True


def install_receive_log_filter():
    logger = logging.getLogger("aiogram.dispatcher")
    if not any(isinstance(item, SafeFetchLog) for item in logger.filters):
        logger.addFilter(SafeFetchLog())
