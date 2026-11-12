from sqlalchemy import or_, select

from vip.models import AccessGrant, OmegaCharge, Order, Payment, PushinCharge


def purchase_blocker(session, telegram_id, now):
    """Conservative local decision; never creates or reconciles a payment."""
    ownership = or_(Order.buyer_telegram_id == telegram_id, Order.linked_telegram_id == telegram_id)
    if session.scalar(select(Payment.transaction_id).join(Order).where(ownership).limit(1)):
        return "purchased"
    if session.scalar(
        select(AccessGrant.id)
        .join(Order)
        .where(ownership, AccessGrant.granted_at.is_not(None))
        .limit(1)
    ):
        return "purchased"
    for order, charge in session.execute(
        select(Order, OmegaCharge)
        .outerjoin(OmegaCharge, OmegaCharge.order_id == Order.id)
        .where(ownership)
    ):
        if order.payment_provider == "pushinpay":
            charge = session.get(PushinCharge, order.id)
        if order.status == "paid" or charge and charge.financial_status == "COMPLETED":
            return "purchased"
        if charge and (
            charge.state in {"creating", "uncertain"}
            or charge.financial_status
            not in {"FAILED", "EXPIRED", "REFUNDED", "CHARGED_BACK", "canceled", "expired"}
        ):
            return "pending_payment"
        if order.status == "pending" and order.expires_at > now:
            return "pending_payment"
    return None


def recovery_reason(session, task, now):
    if task is None or task.eligible_at is None or task.due_at > now:
        return "not_qualified"
    if task.opted_out_at is not None or task.state == "canceled":
        return "opted_out"
    if task.blocked_at is not None:
        return "blocked"
    return purchase_blocker(session, task.telegram_id, now)
