from dataclasses import dataclass

from sqlalchemy import and_, or_, select

from vip.models import (
    AccessGrant,
    AuditEvent,
    Fulfillment,
    OmegaCharge,
    Order,
    Payment,
    Product,
    RecoveryTask,
)
from vip.users import lock_buyer


@dataclass(frozen=True)
class ProductAccess:
    grant_id: str
    order_id: str
    permission: str
    name: str
    telegram_group_id: int | None


def cancel_recovery(session, telegram_id, now):
    if telegram_id is None:
        return
    lock_buyer(session, telegram_id)
    task = session.get(RecoveryTask, telegram_id)
    if task:
        task.eligible_at = None
        if task.state not in {"sent", "uncertain"}:
            task.state = "canceled"
        task.last_error = "purchased"


def grant_product_order(session, order, now):
    if order.product_id is None:
        return
    # Called only after the existing authenticated payment validation succeeds.
    cancel_recovery(session, order.buyer_telegram_id, now)
    for grant in session.scalars(select(AccessGrant).where(AccessGrant.order_id == order.id)):
        if grant.granted_at is None and grant.revoked_at is None:
            grant.granted_at = now
            session.add(
                AuditEvent(action="product_access_granted", entity_id=grant.id, occurred_at=now)
            )


def revoke_product_order(session, order, now):
    if order.product_id is None:
        return
    cancel_recovery(session, order.buyer_telegram_id, now)
    job = session.get(Fulfillment, order.id)
    if job:
        job.status = "cancelled"
    for grant in session.scalars(select(AccessGrant).where(AccessGrant.order_id == order.id)):
        if grant.revoked_at is None:
            grant.revoked_at = now
            session.add(
                AuditEvent(action="product_access_revoked", entity_id=grant.id, occurred_at=now)
            )


def product_accesses(session, telegram_id):
    rows = session.execute(
        select(AccessGrant, Order.product_name)
        .join(Order)
        .join(Product, Product.code == Order.product_id)
        .join(Payment, Payment.order_id == Order.id)
        .outerjoin(OmegaCharge, OmegaCharge.order_id == Order.id)
        .where(
            Order.buyer_telegram_id == telegram_id,
            Order.linked_telegram_id == telegram_id,
            Order.status == "paid",
            AccessGrant.granted_at.is_not(None),
            AccessGrant.revoked_at.is_(None),
            Product.rights_reference.is_not(None),
            Payment.transaction_id == Order.provider_transaction_id,
            Payment.amount_minor == Order.amount_minor,
            Payment.currency == Order.currency,
            Order.currency == "BRL",
            or_(
                OmegaCharge.order_id.is_(None),
                and_(
                    OmegaCharge.financial_status == "COMPLETED",
                    OmegaCharge.last_checked_at.is_not(None),
                ),
            ),
        )
        .order_by(AccessGrant.id)
    ).all()
    return [
        ProductAccess(grant.id, grant.order_id, grant.permission, name, grant.telegram_group_id)
        for grant, name in rows
    ]
