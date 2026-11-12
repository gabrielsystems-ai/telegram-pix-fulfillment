import os
import subprocess
import sys
from time import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from tests.test_migrations import ROOT
from tests.test_pushinpay import Harness
from vip.catalog import create_product_order, install_catalog
from vip.db import Database
from vip.models import BotPurchase, Fulfillment, OmegaCharge, Order, Product
from vip.users import register_user


def test_migration_preserves_legacy_cache_constraints_and_allows_second_product(tmp_path):
    url = f"sqlite:///{(tmp_path / 'per-product.db').as_posix()}"
    env = os.environ | {"DATABASE_URL": url}

    def migrate(*args, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert (result.returncode == 0) == (expected == 0), result.stdout + result.stderr
        return result

    migrate("upgrade", "a41d7e8c902b")
    db = Database(url)
    try:
        with db.transaction() as s:
            install_catalog(s, -100123)
            for product in s.scalars(select(Product)):
                product.rights_reference = "synthetic-test-authorization"
            register_user(s, 123)
            order = create_product_order(s, "sample_video", 123, "synthetic-request-0001")
            oid = order.id
            s.add(
                Fulfillment(
                    order_id=oid,
                    authenticated_user_id=123,
                    verified_at=int(time()),
                    video_state="pending",
                    vip_state="not_required",
                )
            )
            s.add(
                OmegaCharge(order_id=oid, identifier=oid, state="uncertain", created_at=int(time()))
            )
        with db.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO bot_purchases "
                    "(telegram_id,order_id,stage,pix_message_id,pix_file_id,pix_code_hash) "
                    "VALUES (123,:oid,'payment',77,'synthetic-file','synthetic-hash')"
                ),
                {"oid": oid},
            )
        migrate("upgrade", "head")
        migrate("check")
        with db.transaction() as s:
            old = s.get(BotPurchase, (123, "sample_video"))
            assert old.order_id == oid and old.selection_order == 1
            assert old.pix_message_id == 77 and old.pix_file_id == "synthetic-file"
            assert old.pix_code_hash == "synthetic-hash"
        h = Harness(db)
        vip = h.bot.begin(123, "vip_permanent")
        assert vip["copy_paste"] and len(h.posts) == 1
        with pytest.raises(IntegrityError), db.engine.begin() as c:
            c.execute(text("UPDATE bot_purchases SET stage='invalid' WHERE telegram_id=123"))
        result = migrate("downgrade", "a41d7e8c902b", expected=1)
        assert "Unsafe downgrade" in result.stderr
        with db.transaction() as s:
            assert len(list(s.scalars(select(BotPurchase)))) == 2
            assert s.get(Order, oid).status == "pending"
            assert s.get(OmegaCharge, oid).state == "uncertain"
    finally:
        db.engine.dispose()
