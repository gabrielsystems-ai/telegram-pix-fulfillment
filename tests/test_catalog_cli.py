import os
import subprocess
import sys

from sqlalchemy import select

from tests.test_migrations import ROOT
from vip.models import Order, Product


def test_cli_installs_catalog_and_records_rights_without_creating_orders(db):
    env = os.environ | {
        "DATABASE_URL": db.engine.url.render_as_string(hide_password=False),
        "VIP_CHAT_ID": "-100123",
    }
    for args in [
        ("install-catalog",),
        ("install-catalog",),
        ("review-product", "--code", "sample_video", "--rights-reference", "synthetic-review"),
        ("review-product", "--code", "recovery_bundle", "--disable"),
    ]:
        result = subprocess.run(
            [sys.executable, "-m", "vip.cli", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    with db.transaction() as session:
        assert len(list(session.scalars(select(Product)))) == 3
        assert session.get(Product, "sample_video").rights_reference == "synthetic-review"
        assert session.get(Product, "recovery_bundle").active is False
        assert session.scalar(select(Order)) is None
