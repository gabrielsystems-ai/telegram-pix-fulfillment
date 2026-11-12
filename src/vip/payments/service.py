import logging
from time import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from vip.entitlements import grant_product_order
from vip.errors import DomainError
from vip.models import (
    AccessGrant,
    AuditEvent,
    BotPurchase,
    Fulfillment,
    Payment,
    PaymentEvent,
    PixReplacement,
    PushinCharge,
)
from vip.payments.contracts import VerifiedPayment
from vip.purchase_attempts import lock_attempt, mark_settled, settled_order_id

LOG = logging.getLogger(__name__)


def record_verified_payment(
    session: Session, event: VerifiedPayment, *, now: int | None = None, accept_late=False
) -> bool:
    """Persist a trusted adapter result atomically; return False for matching retries.

    This function does not authenticate webhooks. Never call it with client JSON.
    OmegaPayments produces this object only after an authenticated transaction query
    and independent checks against the frozen order and known charge reference.
    """
    now = int(time()) if now is None else now
    if (
        type(event.amount_minor) is not int
        or event.status != "paid"
        or not event.event_id
        or not event.transaction_id
        or len(event.event_id) > 200
        or len(event.transaction_id) > 200
    ):
        raise DomainError("invalid_payment")
    order = lock_attempt(session, event.order_id)
    if event.amount_minor != order.amount_minor or event.currency != order.currency:
        raise DomainError("payment_mismatch")
    if not order.provider_transaction_id or event.transaction_id != order.provider_transaction_id:
        raise DomainError("transaction_mismatch")
    previous_event = session.get(PaymentEvent, event.event_id)
    if previous_event is not None:
        if (
            previous_event.order_id != order.id
            or previous_event.transaction_id != event.transaction_id
        ):
            raise DomainError("event_conflict")
        return False
    payment = session.get(Payment, event.transaction_id)
    order_payment = session.scalar(select(Payment).where(Payment.order_id == order.id))
    if payment is not None and payment.order_id != order.id:
        raise DomainError("transaction_conflict")
    if order_payment is not None:
        if order_payment.transaction_id != event.transaction_id or order.status != "paid":
            raise DomainError("payment_conflict")
        session.add(
            PaymentEvent(
                event_id=event.event_id,
                transaction_id=event.transaction_id,
                order_id=order.id,
                received_at=now,
            )
        )
        return False
    job = session.get(Fulfillment, order.id)
    charge = session.get(PushinCharge, order.id) if order.payment_provider == "pushinpay" else None
    known_bot_attempt = (
        session.scalar(select(BotPurchase.order_id).where(BotPurchase.order_id == order.id))
        or session.get(PixReplacement, order.id)
        or session.scalar(
            select(PixReplacement.old_order_id).where(PixReplacement.new_order_id == order.id)
        )
    )
    late_allowed = (
        accept_late
        and order.payment_provider == "pushinpay"
        and order.status in {"pending", "expired", "cancelled"}
        and charge
        and charge.state == "ready"
        and charge.financial_status == "paid"
        and charge.transaction_id
        and event.transaction_id == "pushinpay:" + charge.transaction_id
        and job
        and job.authenticated_user_id == order.buyer_telegram_id
        and known_bot_attempt
    )
    if (order.status != "pending" or order.expires_at <= now) and not late_allowed:
        raise DomainError("order_not_payable")
    was_cancelled = order.status == "cancelled"
    winner = settled_order_id(session, order.id)
    order.status = "paid"
    order.paid_at = now
    session.add(
        Payment(
            transaction_id=event.transaction_id,
            order_id=order.id,
            amount_minor=event.amount_minor,
            currency=event.currency,
            confirmed_at=now,
        )
    )
    session.flush()
    session.add(
        PaymentEvent(
            event_id=event.event_id,
            transaction_id=event.transaction_id,
            order_id=order.id,
            received_at=now,
        )
    )
    session.add(AuditEvent(action="payment_confirmed", entity_id=order.id, occurred_at=now))
    if winner and winner != order.id:
        # Keep both real receipts; a second payment never grants another delivery.
        if job:
            job.status, job.last_error = "review", "duplicate_payment_review_required"
        session.add(
            AuditEvent(
                action="duplicate_payment_review_required",
                entity_id=order.id,
                occurred_at=now,
            )
        )
        LOG.warning("duplicate_payment_review_required")
        return True
    mark_settled(session, order.id)
    if late_allowed and (
        was_cancelled
        or session.scalar(
            select(AccessGrant.id).where(
                AccessGrant.order_id == order.id, AccessGrant.revoked_at.is_not(None)
            )
        )
    ):
        if job:
            job.status, job.last_error = "review", "late_access_review_required"
        session.add(
            AuditEvent(
                action="late_payment_review_required",
                entity_id=order.id,
                occurred_at=now,
            )
        )
        return True
    grant_product_order(session, order, now)
    from vip.fulfillment import activate_fulfillment

    activate_fulfillment(session, order, now)
    return True
