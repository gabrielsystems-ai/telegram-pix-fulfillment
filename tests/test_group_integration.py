from datetime import UTC, datetime

import pytest
from aiogram.methods import BanChatMember, UnbanChatMember
from sqlalchemy import select

from tests.test_groups import CHAT, Scenario
from tests.test_omega_operations import Harness
from vip.groups import GroupError
from vip.linking import issue_link_token, redeem_link_token
from vip.models import Order, Subscription
from vip.orders import create_order
from vip.plans import create_plan
from vip.users import register_user


@pytest.mark.parametrize(
    "status,event",
    [("REFUNDED", "TRANSACTION_REFUNDED"), ("CHARGED_BACK", "TRANSACTION_CHARGED_BACK")],
)
def test_confirmed_negative_financial_event_causes_member_removal(db, status, event):
    finance = Harness(db)
    finance.create()
    finance.complete()
    finance.reconcile()
    with db.transaction() as session:
        raw = issue_link_token(session, finance.order_id, ttl_seconds=900, now=finance.now)
        register_user(session, 123)
        redeem_link_token(session, raw, 123, now=finance.now)
    s = Scenario(db)
    s.now = finance.now
    ticket = s.service.issue_invite(123)
    s.service.join_request(s.request(123, ticket.url))
    finance.changes = {
        "status": status,
        "refundedAt": datetime.fromtimestamp(finance.now, UTC).isoformat(),
    }
    finance.webhook(event)
    s.now += 11
    s.service.sync_once()
    assert s.telegram.members[123] == "left"
    assert s.telegram.count(BanChatMember) == 1


def test_refund_of_one_purchase_keeps_an_independent_paid_renewal(db):
    finance = Harness(db)
    for order_id in [finance.order_id, finance.order()]:
        finance.create(order_id)
        finance.complete()
        finance.reconcile(order_id)
        with db.transaction() as session:
            raw = issue_link_token(session, order_id, ttl_seconds=900, now=finance.now)
            register_user(session, 123)
            redeem_link_token(session, raw, 123, now=finance.now)
    s = Scenario(db)
    s.now = finance.now
    s.telegram.members[123] = "member"
    s.service.sync_once()
    finance.changes = {
        "status": "REFUNDED",
        "refundedAt": datetime.fromtimestamp(finance.now, UTC).isoformat(),
    }
    finance.webhook("TRANSACTION_REFUNDED")
    s.now += 11
    s.service.sync_once()
    assert s.telegram.members[123] == "member"
    assert s.telegram.count(BanChatMember) == 0


def test_subscription_flags_without_a_confirmed_payment_cannot_grant_access(db):
    s = Scenario(db)
    with db.transaction() as session:
        register_user(session, 123)
        plan = create_plan(
            session, name="Synthetic", amount_minor=100, duration_days=1, telegram_group_id=CHAT
        )
        order = create_order(session, plan.id, now=s.now)
        order.linked_telegram_id = 123
        session.add(
            Subscription(
                order_id=order.id,
                telegram_id=123,
                telegram_group_id=CHAT,
                starts_at=s.now,
                expires_at=s.now + 86400,
                status="active",
            )
        )
    with pytest.raises(GroupError, match="vip_not_eligible"):
        s.service.issue_invite(123)


def test_a_renewed_user_can_request_a_fresh_invite_after_bot_removal(db):
    s = Scenario(db)
    s.paid()
    s.telegram.members[123] = "member"
    s.now += 31 * 86400
    s.service.sync_once()
    assert s.telegram.members[123] == "left"
    # A new valid purchase is linked by the existing mechanism, without changing Telegram IDs.
    with db.transaction() as session:
        from vip.payments.contracts import VerifiedPayment
        from vip.payments.service import record_verified_payment

        plan = session.scalar(select(Order.plan_id))
        order = create_order(session, plan, now=s.now)
        order.provider_transaction_id = "synthetic-renewal"
        record_verified_payment(
            session,
            VerifiedPayment("renewal-event", "synthetic-renewal", order.id, 2500, "BRL", "paid"),
            now=s.now,
        )
        raw = issue_link_token(session, order.id, ttl_seconds=900, now=s.now)
        redeem_link_token(session, raw, 123, now=s.now)
    ticket = s.service.issue_invite(123)
    assert s.service.join_request(s.request(123, ticket.url)) == "approved"
    assert s.telegram.members[123] == "member"


@pytest.mark.parametrize("rate_limited", [False, True])
def test_gateway_errors_do_not_expose_token_or_method(monkeypatch, rate_limited):
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from pydantic import SecretStr

    import vip.telegram_gateway as gateway_module

    class BotDouble:
        def __init__(self, token, session):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.session.close()

        async def __call__(self, method):
            if rate_limited:
                raise TelegramRetryAfter(method, "synthetic-private-token", retry_after=7200)
            raise TelegramForbiddenError(method, "synthetic-private-token")

    monkeypatch.setattr(gateway_module, "Bot", BotDouble)
    gateway = gateway_module.AiogramGroupGateway(SecretStr("synthetic-private-token"))
    with pytest.raises(GroupError) as error:
        gateway.call(UnbanChatMember(chat_id=CHAT, user_id=123, only_if_banned=True))
    assert error.value.code == ("telegram_rate_limited" if rate_limited else "telegram_forbidden")
    if rate_limited:
        assert error.value.retry_after == 7200
    assert "synthetic-private-token" not in str(error.value)
