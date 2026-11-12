import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from alembic import command
from scripts.prepare_bundle import prepare
from vip import deployment
from vip.api import create_app
from vip.config import Settings
from vip.db import Database, normalize_database_url
from vip.models import Base

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scheme", ["postgres://", "postgresql://", "postgresql+psycopg://"])
def test_postgres_uses_installed_driver_without_changing_credentials(scheme):
    suffix = "fake:p%40ss@localhost:5432/test?sslmode=require"
    assert normalize_database_url(scheme + suffix) == "postgresql+psycopg://" + suffix
    db = Database(scheme + suffix)
    try:
        assert db.engine.dialect.driver == "psycopg"
    finally:
        db.engine.dispose()


def test_sqlite_url_preserved():
    assert normalize_database_url("sqlite:///test.db") == "sqlite:///test.db"


def test_hosted_entry_refuses_sqlite():
    with pytest.raises(RuntimeError, match="requires_postgresql"):
        deployment.check_database(Settings(_env_file=None, database_url="sqlite://"))


def test_hosted_migrate_refuses_sqlite_without_creating_tables(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deployment", "migrate"])
    upgrade = MagicMock()
    monkeypatch.setattr(deployment.command, "upgrade", upgrade)
    assert deployment.main() == 1
    upgrade.assert_not_called()


def test_hosted_migration_only_upgrades_head(monkeypatch):
    database = MagicMock()
    database.engine.dialect.name = "postgresql"
    monkeypatch.setattr(deployment, "Database", lambda _: database)
    monkeypatch.setattr(deployment, "schema_ready", lambda _: True)
    monkeypatch.setattr(sys, "argv", ["deployment", "migrate"])
    upgrade = MagicMock()
    monkeypatch.setattr(deployment.command, "upgrade", upgrade)
    assert deployment.main() == 0
    assert upgrade.call_args.args[1] == "head"
    database.engine.dispose.assert_called_once()


def test_hosted_migration_fails_if_head_not_reached(monkeypatch):
    database = MagicMock()
    database.engine.dialect.name = "postgresql"
    monkeypatch.setattr(deployment, "Database", lambda _: database)
    monkeypatch.setattr(deployment, "schema_ready", lambda _: False)
    monkeypatch.setattr(deployment.command, "upgrade", MagicMock())
    monkeypatch.setattr(sys, "argv", ["deployment", "migrate"])
    assert deployment.main() == 1


def test_readiness_requires_migrations_and_preserves_liveness(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'ready.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    db = Database(url)
    try:
        with TestClient(create_app(database=db)) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 503
            command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
            assert client.get("/ready").json() == {"status": "ready"}
            with db.engine.begin() as connection:
                connection.exec_driver_sql("UPDATE alembic_version SET version_num='outdated'")
            assert client.get("/ready").status_code == 503
    finally:
        db.engine.dispose()


def test_readiness_never_exposes_database_errors(monkeypatch, db):
    def fail(_engine):
        raise RuntimeError("fake-secret-dsn")

    monkeypatch.setattr(deployment, "schema_ready", fail)
    with TestClient(create_app(database=db)) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert "fake-secret" not in response.text


def test_polling_lock_refuses_another_owner():
    engine = MagicMock()
    connection = engine.connect.return_value.execution_options.return_value.__enter__.return_value
    connection.scalar.return_value = False
    with pytest.raises(RuntimeError, match="already_running"), deployment.polling_lock(engine):
        pytest.fail("Must not start polling")


def test_polling_lock_is_released():
    engine = MagicMock()
    connection = engine.connect.return_value.execution_options.return_value.__enter__.return_value
    connection.scalar.return_value = True
    with deployment.polling_lock(engine) as owned:
        assert owned is connection
    assert "pg_advisory_unlock" in str(connection.execute.call_args.args[0])


async def test_lost_lock_stops_bot(monkeypatch):
    import asyncio

    from vip import bot

    stopped = []

    async def simulated_bot():
        try:
            await asyncio.Future()
        finally:
            stopped.append(True)

    @contextmanager
    def lock(_engine):
        connection = MagicMock()
        connection.invalidated = True
        yield connection

    original_wait = asyncio.wait

    async def short_wait(tasks, **_kwargs):
        return await original_wait(tasks, timeout=0.001)

    monkeypatch.setattr(bot, "run_bot", simulated_bot)
    monkeypatch.setattr(deployment, "polling_lock", lock)
    monkeypatch.setattr(asyncio, "wait", short_wait)
    with pytest.raises(RuntimeError, match="connection_lost"):
        await deployment.guarded_bot(MagicMock())
    assert stopped == [True]


async def test_polling_lock_heartbeat_does_not_run_sql_on_event_loop(monkeypatch):
    import asyncio
    import threading

    from vip import bot

    loop_thread = threading.get_ident()
    checked = []

    def heartbeat(*_args):
        assert threading.get_ident() != loop_thread
        checked.append(True)

    async def simulated_bot():
        await asyncio.sleep(0.02)

    @contextmanager
    def lock(_engine):
        connection = MagicMock()
        connection.invalidated = connection.closed = False
        connection.execute = heartbeat
        yield connection

    original_wait = asyncio.wait

    async def short_wait(tasks, **_kwargs):
        return await original_wait(tasks, timeout=0.001)

    monkeypatch.setattr(bot, "run_bot", simulated_bot)
    monkeypatch.setattr(deployment, "polling_lock", lock)
    monkeypatch.setattr(asyncio, "wait", short_wait)
    await deployment.guarded_bot(MagicMock())
    assert checked


def test_api_start_uses_one_worker_and_no_polling(monkeypatch):
    database = MagicMock()
    monkeypatch.setattr(deployment, "check_database", lambda _: database)
    monkeypatch.setattr(sys, "argv", ["deployment", "api"])
    monkeypatch.setenv("PORT", "8123")
    execv = MagicMock()
    monkeypatch.setattr(deployment.os, "execv", execv)
    assert deployment.main() == 0
    args = execv.call_args.args[1]
    assert args[args.index("--workers") + 1] == "1"
    assert args[args.index("--port") + 1] == "8123"
    assert "--no-access-log" in args
    assert "vip.bot" not in args


def test_startup_failure_is_sanitized(monkeypatch, caplog):
    def fail(_settings):
        raise RuntimeError("fake-private-credential")

    monkeypatch.setattr(deployment, "check_database", fail)
    monkeypatch.setattr(sys, "argv", ["deployment", "check"])
    assert deployment.main() == 1
    assert "failed" in caplog.text
    assert "fake-private-credential" not in caplog.text


def test_upload_has_no_local_state_or_secrets(tmp_path):
    output = tmp_path / "upload"
    manifest = prepare(output)
    assert "src/vip/bot.py" in manifest
    assert not any(name.startswith((".env", "var/", "tests/", "assets/")) for name in manifest)
    assert not any(Path(name).suffix in (".db", ".sqlite", ".key", ".pem") for name in manifest)
    with pytest.raises(ValueError, match="new directory"):
        prepare(output)


def test_runbook_process_filter_finds_only_bot_pollers():
    runbook = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    pattern = re.search(r"\$_.CommandLine -match '([^']+)'", runbook).group(1)
    assert re.search(pattern, "python.exe -m vip.bot")
    assert re.search(pattern, "python.exe -m vip.bot --example")
    assert re.search(pattern, "python.exe -m vip.deployment bot")
    assert not re.search(pattern, "python.exe -m vip.deployment api")
    assert not re.search(pattern, "python.exe -m vip.deployment check")


def test_postgresql_model_ddl_compiles():
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))


def test_postgresql_migration_sql_compiles(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake:fake@localhost/isolated_test")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE users" in result.stdout
    assert "ALTER TABLE orders" in result.stdout
    assert "COMMIT" in result.stdout
