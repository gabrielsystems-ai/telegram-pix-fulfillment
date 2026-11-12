import os
import subprocess
import sys

from sqlalchemy import text

from tests.test_migrations import ROOT
from vip.db import Database


def test_additive_migration_preserves_legacy_uncertain_charge(tmp_path):
    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    env = os.environ | {"DATABASE_URL": url}

    def migrate(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "f39c62e7a810")
    db = Database(url)
    try:
        with db.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO plans (id,name,amount_minor,currency,duration_days,"
                    "telegram_group_id,active) VALUES "
                    "('legacy-plan','Legacy',1000,'BRL',30,-100123,1)"
                )
            )
            c.execute(
                text(
                    "INSERT INTO orders (id,plan_id,origin,amount_minor,currency,duration_days,"
                    "telegram_group_id,status,created_at,expires_at) VALUES "
                    "('legacy-order','legacy-plan','standard',1000,'BRL',30,-100123,'pending',100,200)"
                )
            )
            c.execute(
                text(
                    "INSERT INTO omega_charges (order_id,identifier,state,created_at,"
                    "next_reconcile_at) VALUES ('legacy-order','legacy-order','uncertain',100,0)"
                )
            )
        migrate("upgrade", "head")
        migrate("check")
        with db.engine.connect() as c:
            assert c.execute(
                text(
                    "SELECT payment_provider,status,amount_minor FROM orders "
                    "WHERE id='legacy-order'"
                )
            ).one() == ("omega", "pending", 1000)
            assert c.execute(
                text("SELECT state,identifier FROM omega_charges WHERE order_id='legacy-order'")
            ).one() == ("uncertain", "legacy-order")
            assert c.execute(text("SELECT count(*) FROM pushin_charges")).scalar() == 0
    finally:
        db.engine.dispose()
