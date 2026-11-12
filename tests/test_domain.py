from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import func, select, update

from vip.errors import DomainError
from vip.linking import issue_link_token, redeem_link_token
from vip.models import AuditEvent, LinkToken, Order, Payment, Subscription
from vip.orders import create_order
from vip.payments.contracts import VerifiedPayment
from vip.payments.service import record_verified_payment
from vip.plans import create_plan
from vip.subscriptions import expire_subscriptions, memberships
from vip.users import register_user

NOW = 1_800_000_000


def purchase(db):
    with db.transaction() as session:
        plan = create_plan(
            session, name="Test VIP", amount_minor=2500, duration_days=30, telegram_group_id=-100123
        )
        order = create_order(session, plan.id, now=NOW)
        # Synthetic charge reference: real deployments must receive this from Omega.
        order.provider_transaction_id = f"tx-{order.id}"
        return order.id, plan.id


def verified(order_id, **changes):
    # Internal synthetic provider result, NOT an Omega payload or authenticity proof.
    event = VerifiedPayment(
        event_id="event-1",
        transaction_id=f"tx-{order_id}",
        order_id=order_id,
        amount_minor=2500,
        currency="BRL",
        status="paid",
    )
    return replace(event, **changes)


def pay(db, order_id, **changes):
    with db.transaction() as session:
        return record_verified_payment(session, verified(order_id, **changes), now=NOW + 1)


def token(db, order_id, now=NOW + 2):
    with db.transaction() as session:
        return issue_link_token(session, order_id, ttl_seconds=900, now=now)


def redeem(db, raw, telegram_id=123, now=NOW + 3):
    with db.transaction() as session:
        register_user(session, telegram_id)
        return redeem_link_token(session, raw, telegram_id, now=now).id


def test_order_snapshots_plan_and_pending_cannot_be_bound(db):
    order_id, plan_id = purchase(db)
    with db.transaction() as session:
        from vip.models import Plan

        session.execute(update(Plan).where(Plan.id == plan_id).values(amount_minor=9999))
        assert session.get(Order, order_id).amount_minor == 2500
        with pytest.raises(DomainError):
            issue_link_token(session, order_id, ttl_seconds=900, now=NOW + 2)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_minor": 2499},
        {"currency": "USD"},
        {"status": "pending"},
        {"status": "cancelled"},
        {"transaction_id": ""},
        {"event_id": ""},
        {"transaction_id": "unknown-transaction"},
        {"amount_minor": 2500.0},
    ],
)
def test_untrusted_or_mismatched_settlement_leaves_order_pending(db, changes):
    order_id, _ = purchase(db)
    with pytest.raises(DomainError):
        pay(db, order_id, **changes)
    with db.transaction() as session:
        assert session.get(Order, order_id).status == "pending"
        assert session.scalar(select(func.count()).select_from(Payment)) == 0


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_closed_order_cannot_be_paid(db, state):
    order_id, _ = purchase(db)
    with db.transaction() as session:
        session.get(Order, order_id).status = state
    with pytest.raises(DomainError):
        pay(db, order_id)


def test_late_payment_is_rejected(db):
    order_id, _ = purchase(db)
    with pytest.raises(DomainError):
        with db.transaction() as session:
            record_verified_payment(session, verified(order_id), now=NOW + 1800)


def test_payment_retries_are_idempotent_and_auditable(db):
    order_id, _ = purchase(db)
    assert pay(db, order_id) is True
    assert pay(db, order_id) is False
    assert pay(db, order_id, event_id="delivery-2") is False
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
        assert session.scalar(select(func.count()).select_from(Subscription)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent)) >= 1


def test_transaction_and_event_cannot_pay_different_orders(db):
    first, _ = purchase(db)
    second, _ = purchase(db)
    pay(db, first)
    with pytest.raises(DomainError, match="transaction_mismatch"):
        pay(db, second, event_id="different-event", transaction_id=f"tx-{first}")
    with pytest.raises(DomainError, match="event_conflict"):
        pay(db, second)
    with db.transaction() as session:
        assert session.get(Order, second).status == "pending"


def test_token_is_hashed_single_use_and_creates_one_membership(db):
    order_id, _ = purchase(db)
    pay(db, order_id)
    raw = token(db, order_id)
    with db.transaction() as session:
        stored = session.scalar(select(LinkToken))
        assert stored.token_hash != raw
        assert len(stored.token_hash) == 64
    subscription_id = redeem(db, raw)
    with pytest.raises(DomainError):
        redeem(db, raw, telegram_id=456)
    with db.transaction() as session:
        subscription = session.get(Subscription, subscription_id)
        assert subscription.telegram_id == 123
        assert subscription.expires_at == NOW + 3 + 30 * 86400
        assert len(memberships(session, 123, now=NOW + 4)) == 1


def test_expired_rotated_and_malformed_tokens_fail(db):
    order_id, _ = purchase(db)
    pay(db, order_id)
    old = token(db, order_id)
    current = token(db, order_id, now=NOW + 3)
    for raw, at in [(old, NOW + 4), (current, NOW + 903), ("bad", NOW + 4)]:
        with pytest.raises(DomainError):
            redeem(db, raw, now=at)


def test_renewals_add_time_and_expiration_is_effective_without_job(db):
    first, _ = purchase(db)
    second, _ = purchase(db)
    pay(db, first)
    pay(db, second, event_id="event-2")
    redeem(db, token(db, first))
    second_id = redeem(db, token(db, second))
    with db.transaction() as session:
        assert session.get(Subscription, second_id).expires_at == NOW + 3 + 60 * 86400
        assert memberships(session, 123, now=NOW + 3 + 60 * 86400) == []
        assert expire_subscriptions(session, now=NOW + 3 + 60 * 86400) == 2
        assert expire_subscriptions(session, now=NOW + 3 + 60 * 86400) == 0


def test_concurrent_payment_and_linking_are_single_use(db):
    order_id, _ = purchase(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: pay(db, order_id), range(2)))
    assert sorted(outcomes) == [False, True]
    raw = token(db, order_id)

    def attempt(telegram_id):
        try:
            return redeem(db, raw, telegram_id=telegram_id)
        except DomainError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [123, 456]))
    assert sum(result is not None for result in results) == 1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Subscription)) == 1


def test_payment_without_previously_registered_charge_is_rejected(db):
    order_id, _ = purchase(db)
    with db.transaction() as session:
        session.get(Order, order_id).provider_transaction_id = None
    with pytest.raises(DomainError):
        pay(db, order_id)


def test_failed_redemption_rolls_back_token_consumption(db):
    order_id, _ = purchase(db)
    pay(db, order_id)
    raw = token(db, order_id)
    with pytest.raises(DomainError):
        with db.transaction() as session:
            redeem_link_token(session, raw, 999, now=NOW + 3)  # user is not registered
    assert redeem(db, raw) is not None


def test_concurrent_renewals_for_distinct_orders_keep_full_duration(db):
    first, _ = purchase(db)
    second, _ = purchase(db)
    pay(db, first)
    pay(db, second, event_id="event-2")
    tokens = [token(db, first), token(db, second)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda raw: redeem(db, raw), tokens))
    assert len(set(results)) == 2
    with db.transaction() as session:
        expires = [item.expires_at for item in memberships(session, 123, now=NOW + 4)]
        assert max(expires) == NOW + 3 + 60 * 86400
