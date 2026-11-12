"""Opt-in duration diagnostics. Never log updates, IDs, tokens or error messages."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramRetryAfter

LOG = logging.getLogger(__name__)
EXTERNAL = ContextVar("bot_external_duration", default=None)


class Timings:
    def __init__(self, enabled=False):
        self.enabled = enabled

    @contextmanager
    def measure(self, stage):
        if not self.enabled:
            yield
            return
        started = perf_counter()
        try:
            yield
        finally:
            LOG.warning(
                "bot_timing stage=%s elapsed_ms=%.3f", stage, (perf_counter() - started) * 1000
            )


class UpdateTimings(BaseMiddleware):
    async def __call__(self, handler, event, data):
        started = perf_counter()
        external = [0.0]
        token = EXTERNAL.set(external)
        kind = "callback" if event.callback_query else "message" if event.message else "other"
        if event.message:
            # Telegram dates have one-second resolution and include network/server delay.
            age = max(0, (datetime.now(UTC) - event.message.date).total_seconds() * 1000)
            LOG.warning("bot_timing stage=update_received delivery_age_ms=%.0f", age)
        try:
            return await handler(event, data)
        finally:
            elapsed = (perf_counter() - started) * 1000
            LOG.warning(
                "bot_timing stage=update kind=%s total_ms=%.3f "
                "telegram_ms=%.3f application_ms=%.3f",
                kind,
                elapsed,
                external[0],
                max(0, elapsed - external[0]),
            )
            EXTERNAL.reset(token)


async def telegram_timings(make_request, bot, method):
    started = perf_counter()
    outcome = "ok"
    try:
        return await make_request(bot, method)
    except TelegramConflictError:
        outcome = "polling_conflict"
        raise
    except TelegramNetworkError:
        outcome = "network_error"
        raise
    except TelegramRetryAfter:
        outcome = "rate_limited"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        elapsed = (perf_counter() - started) * 1000
        tracked = EXTERNAL.get()
        if tracked is not None:
            tracked[0] += elapsed
        # API method names come from aiogram classes, never from user input.
        LOG.warning(
            "bot_timing stage=telegram method=%s elapsed_ms=%.3f result=%s",
            method.__api_method__,
            elapsed,
            outcome,
        )
