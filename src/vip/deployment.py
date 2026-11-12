"""Operational entry point. No payments, seed data or Telegram calls during checks."""

import argparse
import asyncio
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.utils.token import TokenValidationError, validate_token
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import ArgumentError, DisconnectionError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from alembic import command
from vip.config import Settings
from vip.db import Database

LOG = logging.getLogger(__name__)
# Session lock, separate from membership/recovery worker leases.
POLLING_LOCK = 727024116072
ROOT = Path(__file__).resolve().parents[2]
CONFIG_FIELDS = frozenset(Settings.model_fields)


class StartupError(RuntimeError):
    """An internal, allowlisted failure code. Never contains supplied values."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


STARTUP_CODES = {
    "telegram_token_required": ("telegram_token_missing", "TELEGRAM_BOT_TOKEN"),
    "vip_chat_id_required": ("required_configuration_missing", "VIP_CHAT_ID"),
    "hosted_database_requires_postgresql": ("postgresql_required", "DATABASE_URL"),
    "database_migrations_required": ("database_migrations_required", "DATABASE_URL"),
    "polling_already_running": ("polling_ownership_unavailable", "-"),
    "polling_lock_connection_lost": ("polling_ownership_lost", "DATABASE_URL"),
    "invalid_port": ("invalid_configuration", "PORT"),
}


def report_startup_failure(error, role, stage):
    category, fields, error_types = "unexpected_startup_error", "-", "-"
    if isinstance(error, ValidationError):
        category = "invalid_configuration"
        # Do not include input, ctx, messages, repr(error), traceback or exception chain.
        details = error.errors(include_input=False, include_context=False, include_url=False)
        fields = (
            ",".join(
                sorted(
                    {
                        str(item["loc"][0]).upper()
                        for item in details
                        if item["loc"] and item["loc"][0] in CONFIG_FIELDS
                    }
                )
            )
            or "-"
        )
        error_types = ",".join(sorted({item["type"] for item in details}))
    elif isinstance(error, StartupError):
        category, fields = STARTUP_CODES.get(error.code, (category, fields))
    elif isinstance(error, TokenValidationError):
        category, fields = "telegram_token_invalid", "TELEGRAM_BOT_TOKEN"
    elif isinstance(error, TelegramUnauthorizedError):
        category, fields = "telegram_authentication_failed", "TELEGRAM_BOT_TOKEN"
    elif isinstance(error, TelegramConflictError):
        category = "telegram_polling_conflict"
    elif isinstance(error, TelegramNetworkError):
        category = "telegram_connection_failed"
    elif isinstance(error, (OperationalError, DisconnectionError, PoolTimeout)):
        category, fields = "database_connection_failed", "DATABASE_URL"
    elif isinstance(error, ArgumentError):
        category, fields = "database_configuration_invalid", "DATABASE_URL"
    elif isinstance(error, CommandError):
        category = "migration_configuration_invalid"
    elif isinstance(error, ImportError):
        category = "dependency_unavailable"
    LOG.error(
        "startup role=%s failed stage=%s category=%s exception=%s fields=%s error_types=%s",
        role,
        stage,
        category,
        type(error).__name__,
        fields,
        error_types,
    )


def validate_bot_configuration(settings):
    token = settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        raise StartupError("telegram_token_required")
    validate_token(token.get_secret_value())
    if settings.vip_group_enabled and settings.vip_chat_id is None:
        raise StartupError("vip_chat_id_required")


def schema_ready(engine) -> bool:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        current = set(MigrationContext.configure(connection).get_current_heads())
    return bool(expected) and current == expected


def check_database(settings: Settings) -> Database:
    database = Database(settings.database_url)
    try:
        if database.engine.dialect.name != "postgresql":
            raise StartupError("hosted_database_requires_postgresql")
        if not schema_ready(database.engine):
            raise StartupError("database_migrations_required")
        return database
    except BaseException:
        database.engine.dispose()
        raise


@contextmanager
def polling_lock(engine):
    # AUTOCOMMIT keeps the session lock without a long-running idle transaction.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": POLLING_LOCK}
        )
        if not acquired:
            raise StartupError("polling_already_running")
        try:
            yield connection
        finally:
            # Closing the session also releases the lock after a lost connection.
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": POLLING_LOCK})
            except Exception:
                connection.invalidate()


async def guarded_bot(engine, *, state=None):
    from vip.bot import run_bot

    state = {} if state is None else state
    state["stage"] = "polling_ownership"
    with polling_lock(engine) as connection:
        LOG.warning("startup role=bot polling_lock=acquired")
        state["stage"] = "bot_runtime"
        task = asyncio.create_task(run_bot())
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=5)
                if done:
                    break
                # Do not reconnect: a new session would not own the advisory lock.
                if connection.invalidated or connection.closed:
                    raise StartupError("polling_lock_connection_lost")
                state["stage"] = "polling_heartbeat"
                await asyncio.to_thread(connection.execute, text("SELECT 1"))
                state["stage"] = "bot_runtime"
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hosted process startup and schema diagnosis")
    parser.add_argument("role", choices=("api", "bot", "check", "migrate"))
    role = parser.parse_args().role
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    database = None
    state = {"stage": "settings"}
    try:
        settings = Settings()
        if role == "bot":
            state["stage"] = "bot_configuration"
            validate_bot_configuration(settings)
        state["stage"] = "database"
        LOG.warning("startup role=%s verifying_database", role)
        if role == "migrate":
            database = Database(settings.database_url)
            if database.engine.dialect.name != "postgresql":
                raise StartupError("hosted_database_requires_postgresql")
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "alembic"))
            LOG.warning("startup role=migrate applying_schema")
            state["stage"] = "migrations"
            command.upgrade(config, "head")
            if not schema_ready(database.engine):
                raise StartupError("database_migrations_required")
            LOG.warning("startup role=migrate database=postgresql schema=ready")
            return 0
        database = check_database(settings)
        LOG.warning("startup role=%s database=postgresql schema=ready", role)
        if role == "check":
            return 0
        if role == "bot":
            asyncio.run(guarded_bot(database.engine, state=state))
        else:
            state["stage"] = "api_launcher"
            port = int(os.environ.get("PORT", "8080"))
            if not 1 <= port <= 65535:
                raise StartupError("invalid_port")
            database.engine.dispose()
            os.execv(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "vip.api:create_app",
                    "--factory",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(port),
                    "--workers",
                    "1",
                    "--no-access-log",
                ],
            )
        return 0
    except Exception as error:
        report_startup_failure(error, role, state["stage"])
        return 1
    finally:
        if database is not None:
            database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
