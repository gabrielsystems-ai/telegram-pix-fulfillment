from time import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from vip.errors import DomainError
from vip.models import AuditEvent, Order, Plan


def create_order(
    session: Session, plan_id: str, *, ttl_seconds: int = 1800, now: int | None = None
) -> Order:
    now = int(time()) if now is None else now
    plan = session.get(Plan, plan_id)
    if plan is None or not plan.active or ttl_seconds <= 0:
        raise DomainError("unavailable_plan")
    order = Order(
        plan_id=plan.id,
        amount_minor=plan.amount_minor,
        currency=plan.currency,
        duration_days=plan.duration_days,
        telegram_group_id=plan.telegram_group_id,
        created_at=now,
        expires_at=now + ttl_seconds,
    )
    session.add(order)
    session.flush()
    session.add(AuditEvent(action="order_created", entity_id=order.id, occurred_at=now))
    return order


def lock_order(session: Session, order_id: str) -> Order:
    # A write locks the row on PostgreSQL; BEGIN IMMEDIATE serializes SQLite transactions.
    result = session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(id=Order.id)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise DomainError("order_not_found")
    return session.scalar(
        select(Order).where(Order.id == order_id).execution_options(populate_existing=True)
    )


def expire_orders(session: Session, *, now: int | None = None) -> int:
    now = int(time()) if now is None else now
    ids = session.scalars(
        update(Order)
        .where(Order.status == "pending", Order.expires_at <= now)
        .values(status="expired")
        .returning(Order.id)
    ).all()
    for order_id in ids:
        session.add(AuditEvent(action="order_expired", entity_id=order_id, occurred_at=now))
    return len(ids)
