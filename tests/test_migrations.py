import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

from vip.db import Database

ROOT = Path(__file__).resolve().parents[1]


def test_migration_upgrade_check_downgrade_and_reupgrade(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migrations.db').as_posix()}"
    environment = os.environ | {"DATABASE_URL": url}
    database = Database(url)
    try:
        for arguments in [
            ("upgrade", "head"),
            ("check",),
            ("downgrade", "base"),
            ("upgrade", "head"),
            ("check",),
        ]:
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *arguments],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            if arguments == ("downgrade", "base"):
                assert set(inspect(database.engine).get_table_names()) == {"alembic_version"}
        assert set(inspect(database.engine).get_table_names()) == {
            "alembic_version",
            "users",
            "plans",
            "orders",
            "payments",
            "payment_events",
            "payment_window_renewals",
            "pix_replacements",
            "subscriptions",
            "link_tokens",
            "audit_events",
            "omega_charges",
            "omega_events",
            "pushin_charges",
            "pushin_notifications",
            "pushin_query_gates",
            "vip_invites",
            "vip_members",
            "vip_sync_leases",
            "products",
            "buyer_assurances",
            "access_grants",
            "recovery_tasks",
            "bot_media_cache",
            "web_purchases",
            "fulfillments",
            "bot_purchases",
        }
    finally:
        database.engine.dispose()
