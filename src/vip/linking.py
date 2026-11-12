import hashlib
import re
import secrets
from time import time

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from vip.entitlements import cancel_recovery
from vip.errors import DomainError
from vip.models import AuditEvent, LinkToken, Payment, Subscription, User
from vip.orders import lock_order


def issue_link_token(
    session: Session, order_id: str, *, ttl_seconds: int, now: int | None = None
) -> str:
    now = int(time()) if now is None else now
    order = lock_order(session, order_id)
    if (
        order.status != "paid"
        or order.linked_telegram_id is not None
        or not session.scalar(select(Payment).where(Payment.order_id == order.id))
        or not 60 <= ttl_seconds <= 3600
    ):
        raise DomainError("order_not_linkable")
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    existing = session.scalar(select(LinkToken).where(LinkToken.order_id == order.id))
    if existing is None:
        session.add(LinkToken(token_hash=digest, order_id=order.id, expires_at=now + ttl_seconds))
    else:
        existing.token_hash = digest
        existing.expires_at = now + ttl_seconds
        existing.consumed_at = None
    session.add(AuditEvent(action="link_token_issued", entity_id=order.id, occurred_at=now))
    session.flush()
    return raw


def redeem_link_token(
    session: Session, raw: str, telegram_id: int, *, now: int | None = None
) -> Subscription | list:
    now = int(time()) if now is None else now
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw):
        raise DomainError("invalid_link_token")
    digest = hashlib.sha256(raw.encode()).hexdigest()
    token = session.get(LinkToken, digest)
    if token is None:
        raise DomainError("invalid_link_token")
    order = lock_order(session, token.order_id)
    # Atomic consumption is checked after the order lock, even if an earlier read was stale.
    if order.status != "paid" or order.linked_telegram_id is not None:
        raise DomainError("invalid_link_token")
    if order.buyer_telegram_id is not None and order.buyer_telegram_id != telegram_id:
        raise DomainError("invalid_link_token")
    payment = session.scalar(select(Payment).where(Payment.order_id == order.id))
    if (
        payment is None
        or payment.amount_minor != order.amount_minor
        or payment.currency != order.currency
    ):
        raise DomainError("invalid_link_token")
    consumed = session.execute(
        update(LinkToken)
        .where(
            LinkToken.token_hash == digest,
            LinkToken.consumed_at.is_(None),
            LinkToken.expires_at > now,
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    if consumed.rowcount != 1:
        raise DomainError("invalid_link_token")
    # Serialize renewals for this Telegram identity, including different orders.
    user_lock = session.execute(
        update(User).where(User.telegram_id == telegram_id).values(telegram_id=User.telegram_id)
    )
    if user_lock.rowcount != 1:
        raise DomainError("unknown_user")
    order.linked_telegram_id = telegram_id
    cancel_recovery(session, telegram_id, now)
    if order.product_id is not None:
        from vip.models import AccessGrant

        session.add(AuditEvent(action="purchase_linked", entity_id=order.id, occurred_at=now))
        session.flush()
        return list(session.scalars(select(AccessGrant).where(AccessGrant.order_id == order.id)))
    latest_expiry = session.scalar(
        select(func.max(Subscription.expires_at)).where(
            Subscription.telegram_id == telegram_id,
            Subscription.telegram_group_id == order.telegram_group_id,
            Subscription.status == "active",
        )
    )
    subscription = Subscription(
        order_id=order.id,
        telegram_id=telegram_id,
        telegram_group_id=order.telegram_group_id,
        starts_at=now,
        expires_at=max(now, latest_expiry or now) + order.duration_days * 86400,
    )
    order.linked_telegram_id = telegram_id
    session.add(subscription)
    session.add(AuditEvent(action="purchase_linked", entity_id=order.id, occurred_at=now))
    session.flush()
    return subscription
