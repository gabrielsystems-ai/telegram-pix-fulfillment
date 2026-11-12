import os
import sqlite3
import subprocess
import sys

from tests.test_migrations import ROOT


def test_product_migration_preserves_historical_orders_and_references(tmp_path):
    path = tmp_path / "legacy.db"
    env = os.environ | {"DATABASE_URL": "sqlite:///" + path.as_posix()}

    def migrate(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "c36678beb654")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO users VALUES (123, 1000)")
        connection.execute(
            "INSERT INTO plans VALUES ('old-plan', 'Legacy', 2500, 'BRL', 30, -100123, 1)"
        )
        connection.execute(
            "INSERT INTO orders VALUES ('old-order','old-plan',2500,'BRL',30,"
            "-100123,'paid',1000,2800,1001,'old-tx',123)"
        )
        connection.execute("INSERT INTO payments VALUES ('old-tx','old-order',2500,'BRL',1001)")
        connection.execute(
            "INSERT INTO subscriptions VALUES "
            "('old-sub','old-order',123,-100123,1002,2593002,'active')"
        )
        old_order = connection.execute("SELECT * FROM orders").fetchone()
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT id,plan_id,amount_minor,currency,duration_days,telegram_group_id,"
                "status,created_at,expires_at,paid_at,provider_transaction_id,"
                "linked_telegram_id FROM orders"
            ).fetchone()
            == old_order
        )
        assert connection.execute("SELECT order_id FROM payments").fetchone() == ("old-order",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        ddl = connection.execute("SELECT sql FROM sqlite_master WHERE name='orders'").fetchone()[0]
        assert "amount_minor > 0" in ddl and "currency = 'BRL'" in ddl
        assert "ck_orders_kind" in ddl
    migrate("downgrade", "c36678beb654")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM orders").fetchone() == old_order
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
