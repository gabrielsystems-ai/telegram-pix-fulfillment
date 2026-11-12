import os
import subprocess
import sys
from time import time
from uuid import uuid4

from sqlalchemy import func, select

from tests.test_catalog import setup_catalog
from tests.test_migrations import ROOT
from tests.test_pushinpay import Harness
from vip.catalog import create_product_order
from vip.db import Database
from vip.models import BotPurchase, Fulfillment, Order, PixReplacement, PushinCharge


def test_additive_migration_preserves_old_ready_pix_and_refuses_history_loss(tmp_path):
    url = f"sqlite:///{(tmp_path / 'replacements.db').as_posix()}"
    env = os.environ | {"DATABASE_URL": url}

    def migrate(*args, expected=0):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert (r.returncode == 0) == (expected == 0), r.stdout + r.stderr
        return r

    migrate("upgrade", "c63f014be725")
    db = Database(url)
    now, tx = int(time()), str(uuid4())
    try:
        setup_catalog(db)
        with db.transaction() as s:
            old = create_product_order(
                s,
                "sample_video",
                123,
                "synthetic-old-ready-request",
                now=now - 3600,
                payment_provider="pushinpay",
            )
            oid = old.id
            old.provider_transaction_id = "pushinpay:" + tx
            s.add(
                Fulfillment(
                    order_id=oid,
                    authenticated_user_id=123,
                    verified_at=now - 3600,
                    video_state="pending",
                    vip_state="not_required",
                )
            )
            s.add(
                BotPurchase(
                    telegram_id=123,
                    product_code="sample_video",
                    order_id=oid,
                    stage="payment",
                    pix_message_id=99,
                    pix_file_id="synthetic-cache",
                )
            )
            s.add(
                PushinCharge(
                    order_id=oid,
                    transaction_id=tx,
                    state="ready",
                    created_at=now - 3600,
                    copy_paste="synthetic-original-code",
                    financial_status="created",
                )
            )
        migrate("upgrade", "head")
        migrate("check")
        h = Harness(db)
        new = h.bot.replace(123, oid)
        with db.transaction() as s:
            assert s.get(Order, oid).status == "pending"
            assert s.get(Order, oid).amount_minor == 1000
            assert s.get(PushinCharge, oid).transaction_id == tx
            assert s.get(PushinCharge, oid).copy_paste == "synthetic-original-code"
            assert s.get(PixReplacement, oid).new_order_id == new["order_id"]
        failed = migrate("downgrade", "c63f014be725", expected=1)
        assert "Unsafe downgrade" in failed.stderr
        with db.transaction() as s:
            assert s.scalar(select(func.count()).select_from(PixReplacement)) == 1
    finally:
        db.engine.dispose()
