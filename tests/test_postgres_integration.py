"""Optional real PostgreSQL check. Only an explicitly named local test DB is accepted."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from alembic import command
from tests.test_catalog import setup_catalog
from tests.test_pushinpay import Harness, notify
from tests.test_sample_delivery import Sender, registered
from vip.db import Database
from vip.errors import DomainError
from vip.fulfillment import FulfillmentService
from vip.models import Fulfillment, Payment


def test_postgres_migration_concurrent_charge_replay_and_delivery(monkeypatch):
    raw_url = os.environ.get("LOCAL_TEST_POSTGRES_URL")
    if not raw_url:
        pytest.skip("Set LOCAL_TEST_POSTGRES_URL to a disposable loopback test_ database")
    url = make_url(raw_url)
    if url.host not in {"127.0.0.1", "localhost", "::1"} or not (url.database or "").startswith(
        "test_"
    ):
        pytest.fail("Only loopback PostgreSQL databases named test_* are permitted")
    monkeypatch.setenv("DATABASE_URL", raw_url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    database = Database(raw_url)
    try:
        setup_catalog(database)
        harness = Harness(database)

        def buy(_):
            try:
                return harness.bot.begin(123, "sample_video")
            except DomainError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(buy, range(2)))
        assert len(harness.posts) == 1
        order_id = harness.bot.status(123)["order_id"]
        transaction = next(iter(harness.records))
        harness.changes = {"status": "paid", "value": 1000}
        notify(harness, transaction)
        harness.service.process_pending()
        notify(harness, transaction)
        harness.now += 60
        harness.service.process_pending()
        registered(database)
        sender = Sender()
        worker = FulfillmentService(database, 999, sender, clock=lambda: harness.now)
        worker.run_once()
        worker.run_once()
        with database.transaction() as session:
            assert session.scalar(select(func.count()).select_from(Payment)) == 1
            assert session.get(Fulfillment, order_id).status == "complete"
        assert len(sender.calls) == 1
    finally:
        database.engine.dispose()
