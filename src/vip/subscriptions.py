from dataclasses import dataclass
from time import time

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from vip.entitlements import product_accesses
from vip.models import AuditEvent, OmegaCharge, Order, Payment, Plan, Subscription


@dataclass(frozen=True)
class VIPEntitlement:
    subscription_id: str
    plan_name: str
    expires_at: int | None


def vip_entitlements(
    session: Session, telegram_id: int, chat_id: int, *, now: int | None = None
) -> list[VIPEntitlement]:
    """Entry authorization uses both subscription validity and persisted financial proof."""
    now = int(time()) if now is None else now
    rows = session.execute(
        select(Subscription, Plan.name)
        .join(Order, Subscription.order_id == Order.id)
        .join(Payment, Payment.order_id == Order.id)
        .join(Plan, Plan.id == Order.plan_id)
        .outerjoin(OmegaCharge, OmegaCharge.order_id == Order.id)
        .where(
            Subscription.telegram_id == telegram_id,
            Subscription.telegram_group_id == chat_id,
            Subscription.status == "active",
            Subscription.starts_at <= now,
            Subscription.expires_at > now,
            Order.status == "paid",
            Order.linked_telegram_id == telegram_id,
            Order.telegram_group_id == chat_id,
            Order.currency == "BRL",
            Payment.currency == Order.currency,
            Payment.amount_minor == Order.amount_minor,
            Payment.transaction_id == Order.provider_transaction_id,
            or_(
                OmegaCharge.order_id.is_(None),
                and_(
                    OmegaCharge.financial_status == "COMPLETED",
                    OmegaCharge.last_checked_at.is_not(None),
                ),
            ),
        )
        .order_by(Subscription.expires_at.desc())
    ).all()
    dated = [
        VIPEntitlement(subscription.id, name, subscription.expires_at)
        for subscription, name in rows
    ]
    permanent = [
        VIPEntitlement(access.grant_id, access.name, None)
        for access in product_accesses(session, telegram_id)
        if access.permission == "vip_permanent_access" and access.telegram_group_id == chat_id
    ]
    return permanent + dated


def memberships(
    session: Session, telegram_id: int, *, now: int | None = None
) -> list[Subscription]:
    now = int(time()) if now is None else now
    return list(
        session.scalars(
            select(Subscription)
            .where(
                Subscription.telegram_id == telegram_id,
                Subscription.status == "active",
                Subscription.starts_at <= now,
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.desc())
        )
    )


def expire_subscriptions(
    session: Session, *, now: int | None = None, chat_id: int | None = None
) -> int:
    now = int(time()) if now is None else now
    statement = update(Subscription).where(
        Subscription.status == "active", Subscription.expires_at <= now
    )
    if chat_id is not None:
        statement = statement.where(Subscription.telegram_group_id == chat_id)
    ids = session.scalars(statement.values(status="expired").returning(Subscription.id)).all()
    for subscription_id in ids:
        session.add(
            AuditEvent(action="subscription_expired", entity_id=subscription_id, occurred_at=now)
        )
    return len(ids)
