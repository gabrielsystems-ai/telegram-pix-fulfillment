import sys
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from vip import deployment
from vip.config import Settings


def test_nonnegative_vip_chat_id_is_diagnosed_before_database_access(monkeypatch, caplog):
    monkeypatch.setenv("VIP_GROUP_ENABLED", "false")
    monkeypatch.setenv("VIP_CHAT_ID", "123456789")
    monkeypatch.setattr(sys, "argv", ["deployment", "bot"])
    database = MagicMock()
    monkeypatch.setattr(deployment, "check_database", database)
    assert deployment.main() == 1
    database.assert_not_called()
    assert "category=invalid_configuration" in caplog.text
    assert "exception=ValidationError" in caplog.text
    assert "fields=VIP_CHAT_ID" in caplog.text
    assert "error_types=less_than" in caplog.text
    assert "123456789" not in caplog.text


def test_empty_chat_id_is_valid_only_with_disabled_group(monkeypatch):
    monkeypatch.setenv("VIP_GROUP_ENABLED", "false")
    monkeypatch.setenv("VIP_CHAT_ID", "")
    assert Settings(_env_file=None).vip_chat_id is None


@pytest.mark.parametrize(
    "token,category",
    [("", "telegram_token_missing"), ("fake-token-without-colon", "telegram_token_invalid")],
)
def test_bot_credentials_fail_safely_before_database(monkeypatch, caplog, token, category):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(sys, "argv", ["deployment", "bot"])
    database = MagicMock()
    monkeypatch.setattr(deployment, "check_database", database)
    assert deployment.main() == 1
    database.assert_not_called()
    assert f"category={category}" in caplog.text
    assert "fields=TELEGRAM_BOT_TOKEN" in caplog.text
    assert "fake-token-without-colon" not in caplog.text


@pytest.mark.parametrize(
    "error,category,exception",
    [
        (
            OperationalError("fake-private-sql", {}, Exception("fake-password")),
            "database_connection_failed",
            "OperationalError",
        ),
        (RuntimeError("fake-sensitive-error"), "unexpected_startup_error", "RuntimeError"),
        (
            TelegramUnauthorizedError(GetMe(), "fake-token-in-HTTP-error"),
            "telegram_authentication_failed",
            "TelegramUnauthorizedError",
        ),
    ],
)
def test_startup_categories_do_not_log_exception_messages(
    monkeypatch, caplog, error, category, exception
):
    monkeypatch.setattr(sys, "argv", ["deployment", "check"])

    def fail(_settings):
        raise error

    monkeypatch.setattr(deployment, "check_database", fail)
    assert deployment.main() == 1
    assert f"category={category}" in caplog.text
    assert f"exception={exception}" in caplog.text
    assert "fake-" not in caplog.text


def test_configuration_error_never_echoes_pydantic_input_or_context(monkeypatch, caplog):
    def invalid_settings():
        return Settings(_env_file=None, sales_api_key="fake-secret-short")

    monkeypatch.setattr(sys, "argv", ["deployment", "bot"])
    monkeypatch.setattr(deployment, "Settings", invalid_settings)
    assert deployment.main() == 1
    assert "exception=ValidationError" in caplog.text
    assert "fields=SALES_API_KEY" in caplog.text
    assert "fake-secret-short" not in caplog.text


def test_validation_rule_rejects_nonnegative_id_without_weaking_security():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, vip_group_enabled=False, vip_chat_id=123)


@pytest.mark.parametrize(
    "code,category",
    [
        ("database_migrations_required", "database_migrations_required"),
        ("polling_already_running", "polling_ownership_unavailable"),
        ("polling_lock_connection_lost", "polling_ownership_lost"),
    ],
)
def test_explicit_startup_failures_have_distinct_categories(caplog, code, category):
    deployment.report_startup_failure(deployment.StartupError(code), "bot", "polling_ownership")
    assert f"category={category}" in caplog.text
    assert "exception=StartupError" in caplog.text
    assert "stage=polling_ownership" in caplog.text


def test_group_automation_still_requires_group_id(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:synthetic-token")
    monkeypatch.setenv("VIP_GROUP_ENABLED", "true")
    monkeypatch.setenv("VIP_CHAT_ID", "")
    monkeypatch.setattr(sys, "argv", ["deployment", "bot"])
    database = MagicMock()
    monkeypatch.setattr(deployment, "check_database", database)
    assert deployment.main() == 1
    database.assert_not_called()
    assert "category=required_configuration_missing" in caplog.text
    assert "fields=VIP_CHAT_ID" in caplog.text
    assert "synthetic-token" not in caplog.text


def test_bot_only_correction_reaches_runtime_without_disabling_ownership(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:synthetic-token")
    monkeypatch.setenv("VIP_CHAT_ID", "")
    monkeypatch.setattr(sys, "argv", ["deployment", "bot"])
    database = MagicMock()
    monkeypatch.setattr(deployment, "check_database", lambda _: database)
    calls = []

    async def guarded(engine, *, state=None):
        calls.append(engine)
        state["stage"] = "polling_ownership"
        raise deployment.StartupError("polling_already_running")

    monkeypatch.setattr(deployment, "guarded_bot", guarded)
    assert deployment.main() == 1
    assert calls == [database.engine]
    assert "category=polling_ownership_unavailable" in caplog.text


def test_runtime_unauthorized_token_is_classified_without_exposing_token(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:synthetic-token")
    monkeypatch.setattr(sys, "argv", ["deployment", "bot"])
    monkeypatch.setattr(deployment, "check_database", lambda _: MagicMock())

    async def unauthorized(_engine, *, state=None):
        state["stage"] = "bot_runtime"
        raise TelegramUnauthorizedError(GetMe(), "fake-token-in-error")

    monkeypatch.setattr(deployment, "guarded_bot", unauthorized)
    assert deployment.main() == 1
    assert "stage=bot_runtime" in caplog.text
    assert "category=telegram_authentication_failed" in caplog.text
    assert "synthetic-token" not in caplog.text
    assert "fake-token" not in caplog.text
