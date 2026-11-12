"""Offline fixtures in a fixed, isolated SQLite file. Never calls Telegram or Omega.

This is not imported by the bot/API and provides no production payment command.
"""

import argparse
import json
import os
from pathlib import Path
from time import time

from alembic.config import Config
from sqlalchemy import select

from alembic import command
from vip.db import Database
from vip.linking import issue_link_token
from vip.models import AuditEvent, Order, Payment, Plan, Subscription
from vip.orders import create_order
from vip.payments.contracts import VerifiedPayment
from vip.payments.operations import OmegaPayments
from vip.payments.service import record_verified_payment
from vip.plans import create_plan

SOURCE_ROOT = Path(__file__).resolve().parents[1]


class DemoData:
    def __init__(self, root=SOURCE_ROOT):
        root = Path(root).resolve()
        self.directory = (root / "var" / "telegram-demo").resolve()
        self.path = self.directory / "vip-demo.db"
        if not self.directory.is_relative_to(root) or self.path.is_symlink():
            raise ValueError("Unsafe demo location")
        self.marker_path = self.directory / "fixture.json"
        self.url = "sqlite:///" + self.path.as_posix()

    def seed(self, telegram_id, chat_id):
        if telegram_id <= 0 or chat_id >= 0:
            raise ValueError("Use a positive test Telegram ID and negative test supergroup ID")
        if self.path.exists() or self.marker_path.exists():
            raise ValueError("Demo already exists; refusing to overwrite it")
        self.directory.mkdir(parents=True, exist_ok=True)
        original = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = self.url
        try:
            command.upgrade(Config(str(SOURCE_ROOT / "alembic.ini")), "head")
        finally:
            if original is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = original
        db = Database(self.url)
        try:
            with db.transaction() as session:
                plan = create_plan(
                    session,
                    name="ISOLATED MANUAL TEST FIXTURE",
                    amount_minor=100,
                    duration_days=1,
                    telegram_group_id=chat_id,
                )
                order, raw = self._purchase(session, plan.id)
                session.add(
                    AuditEvent(
                        action="manual_test_fixture", entity_id=order.id, occurred_at=int(time())
                    )
                )
                marker = {
                    "version": 1,
                    "telegram_id": telegram_id,
                    "chat_id": chat_id,
                    "plan_id": plan.id,
                    "orders": [order.id],
                }
            self.marker_path.write_text(json.dumps(marker), encoding="utf-8")
            return raw
        finally:
            db.engine.dispose()

    @staticmethod
    def _purchase(session, plan_id):
        order = create_order(session, plan_id)
        reference = "manual-demo-" + order.id
        order.provider_transaction_id = reference
        record_verified_payment(
            session,
            VerifiedPayment(
                event_id=reference,
                transaction_id=reference,
                order_id=order.id,
                amount_minor=order.amount_minor,
                currency="BRL",
                status="paid",
            ),
        )
        return order, issue_link_token(session, order.id, ttl_seconds=900)

    def change(self, action):
        if not self.marker_path.is_file() or not self.path.is_file():
            raise ValueError("Isolated demo marker/database not found")
        marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        if marker.get("version") != 1:
            raise ValueError("Unrecognized demo marker")
        db = Database(self.url)
        raw = None
        try:
            with db.transaction() as session:
                orders = list(session.scalars(select(Order)))
                references = list(session.scalars(select(Payment.transaction_id)))
                plan = session.get(Plan, marker["plan_id"])
                if (
                    set(o.id for o in orders) != set(marker["orders"])
                    or any(not ref.startswith("manual-demo-") for ref in references)
                    or plan is None
                    or plan.telegram_group_id != marker["chat_id"]
                    or any(
                        o.linked_telegram_id not in (None, marker["telegram_id"]) for o in orders
                    )
                ):
                    raise ValueError(
                        "Database is not the expected isolated fixture; refusing changes"
                    )
                if action == "renew":
                    order, raw = self._purchase(session, marker["plan_id"])
                    marker["orders"].append(order.id)
                elif action == "expire":
                    subscriptions = list(session.scalars(select(Subscription)))
                    if not subscriptions or any(
                        int(time()) <= sub.starts_at + 1 for sub in subscriptions
                    ):
                        raise ValueError("Bind the fixture first and wait at least two seconds")
                    for sub in subscriptions:
                        sub.expires_at = sub.starts_at + 1
                elif action == "revoke":
                    for order in orders:
                        order.status = "cancelled"
                        OmegaPayments._revoke_internal(session, order, int(time()))
                else:
                    raise ValueError("Unknown fixture action")
            self.marker_path.write_text(json.dumps(marker), encoding="utf-8")
            return raw
        finally:
            db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(
        description="Offline fixtures ONLY in var/telegram-demo/vip-demo.db"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("--telegram-id", type=int, required=True)
    seed.add_argument("--chat-id", type=int, required=True)
    for action in ["renew", "expire", "revoke"]:
        commands.add_parser(action)
    args = parser.parse_args()
    data = DemoData()
    try:
        raw = (
            data.seed(args.telegram_id, args.chat_id)
            if args.action == "seed"
            else data.change(args.action)
        )
    except (ValueError, OSError) as error:
        parser.exit(1, f"Fixture operation rejected: {error}\n")
    print("Isolated test database: " + str(data.path))
    if raw:
        # Deliberate one-time delivery to the tester, not an application/audit log.
        print("One-use test binding instruction: /vincular " + raw)
    else:
        print("Isolated fixture updated. No Telegram/Omega request was made.")


if __name__ == "__main__":
    main()
