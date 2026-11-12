"""One purchase, multiple explicit Pix attempts; settlement stays serialized."""

from sqlalchemy import or_, select

from vip.errors import DomainError
from vip.models import AuditEvent, Order, Payment, PixReplacement
from vip.orders import lock_order
from vip.users import lock_buyer


def root_order_id(session, order_id):
    return (
        session.scalar(
            select(PixReplacement.root_order_id)
            .where(
                or_(
                    PixReplacement.old_order_id == order_id, PixReplacement.new_order_id == order_id
                )
            )
            .limit(1)
        )
        or order_id
    )


def lock_attempt(session, order_id):
    snapshot = session.get(Order, order_id)
    if snapshot is None:
        raise DomainError("order_not_found")
    if snapshot.payment_provider == "pushinpay" and snapshot.buyer_telegram_id is not None:
        # The same buyer lock serializes creation and settlement across the chain.
        # Acquire it before the individual order, consistently with BotPurchases.
        lock_buyer(session, snapshot.buyer_telegram_id)
    return lock_order(session, order_id)


def settled_order_id(session, order_id):
    root = root_order_id(session, order_id)
    canonical = session.get(PixReplacement, root)
    if canonical and canonical.settled_order_id:
        return canonical.settled_order_id
    replacements = select(PixReplacement.new_order_id).where(PixReplacement.root_order_id == root)
    return session.scalar(
        select(Payment.order_id)
        .where(or_(Payment.order_id == root, Payment.order_id.in_(replacements)))
        .order_by(Payment.confirmed_at, Payment.order_id)
        .limit(1)
    )


def mark_settled(session, order_id):
    canonical = session.get(PixReplacement, root_order_id(session, order_id))
    if canonical and canonical.settled_order_id is None:
        canonical.settled_order_id = order_id


def link_replacement(session, old, new, now):
    if (
        old.payment_provider != "pushinpay"
        or new.payment_provider != "pushinpay"
        or old.buyer_telegram_id != new.buyer_telegram_id
        or old.product_id != new.product_id
        or old.amount_minor != new.amount_minor
        or old.currency != new.currency
    ):
        raise DomainError("replacement_mismatch")
    session.add(
        PixReplacement(
            old_order_id=old.id,
            new_order_id=new.id,
            root_order_id=root_order_id(session, old.id),
            replaced_at=now,
        )
    )
    session.add(AuditEvent(action="pix_attempt_replaced", entity_id=old.id, occurred_at=now))
