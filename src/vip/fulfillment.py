"""Durable delivery outbox, released by trusted payment confirmation only."""

import logging
from time import time
from uuid import uuid4

from sqlalchemy import select, update

from vip.entitlements import product_accesses
from vip.errors import DomainError
from vip.models import AuditEvent, Fulfillment, Order
from vip.video_delivery import deliver_video

LOG = logging.getLogger(__name__)
DONE = {"not_required", "sent"}


class DeliverySendError(Exception):
    def __init__(self, code, *, retry_after=30):
        self.code = code
        self.retry_after = max(30, retry_after)
        super().__init__(code)


def activate_fulfillment(session, order, now):
    job = session.get(Fulfillment, order.id)
    if job is None:
        return  # Historical/unverified orders retain their existing binding flow.
    if (
        order.product_id is None
        or order.status != "paid"
        or order.buyer_telegram_id != job.authenticated_user_id
        or order.linked_telegram_id not in (None, job.authenticated_user_id)
    ):
        raise DomainError("verified_purchase_conflict")
    order.linked_telegram_id = job.authenticated_user_id
    job.status, job.next_check_at = "queued", now
    session.add(AuditEvent(action="purchase_auto_associated", entity_id=order.id, occurred_at=now))


def fulfillment_view(session, order_id, *, now):
    order, job = session.get(Order, order_id), session.get(Fulfillment, order_id)
    if order.status in {"cancelled", "expired"} or (
        order.status == "pending" and order.expires_at <= now
    ):
        state, message = "unavailable", "Este pagamento não está disponível."
    elif order.status != "paid":
        state, message = "awaiting_payment", "Aguardando pagamento."
    elif job.status == "complete":
        state, message = "available", "Produto disponível."
    elif job.status == "review" or "uncertain" in (job.video_state, job.vip_state):
        state, message = (
            "review_required",
            "Pagamento confirmado. Confira a entrega e fale com o suporte.",
        )
    elif "retry" in (job.video_state, job.vip_state):
        state, message = "retrying", "Pagamento confirmado. Falha recuperável na entrega."
    else:
        state, message = "paid", "Pagamento confirmado. Preparando sua entrega."
    return {
        "order_id": order.id,
        "state": state,
        "message": message,
        "video": job.video_state,
        "vip": job.vip_state,
    }


class FulfillmentService:
    def __init__(self, db, bot_id, video_sender, *, access=None, invite_sender=None, clock=time):
        self.db, self.bot_id, self.video_sender = db, bot_id, video_sender
        self.access, self.invite_sender, self.clock = access, invite_sender, clock

    def run_once(self, stop=None):
        now = int(self.clock())
        with self.db.transaction() as s:
            ids = list(
                s.scalars(
                    select(Fulfillment.order_id)
                    .where(
                        Fulfillment.status.in_(["queued", "processing"]),
                        Fulfillment.next_check_at <= now,
                        Fulfillment.lease_until <= now,
                    )
                    .order_by(Fulfillment.next_check_at, Fulfillment.order_id)
                    .limit(10)
                )
            )
        count = 0
        for oid in ids:
            if stop is not None and stop.is_set():
                break
            owner = uuid4().hex
            with self.db.transaction() as s:
                claimed = s.execute(
                    update(Fulfillment)
                    .where(
                        Fulfillment.order_id == oid,
                        Fulfillment.status.in_(["queued", "processing"]),
                        Fulfillment.next_check_at <= int(self.clock()),
                        Fulfillment.lease_until <= int(self.clock()),
                    )
                    .values(
                        status="processing",
                        lease_owner=owner,
                        lease_until=int(self.clock()) + 120,
                        attempts=Fulfillment.attempts + 1,
                    )
                ).rowcount
            if not claimed:
                continue
            count += 1
            try:
                self._process(oid, owner)
            except Exception as error:
                # Keep sending/claim markers: uncertain outcomes must not auto-resend.
                LOG.warning("fulfillment_attempt_failed exception=%s", type(error).__name__)
                self._save(
                    oid,
                    owner,
                    status="queued",
                    next_check_at=int(self.clock()) + 30,
                    last_error="delivery_processing_unavailable",
                    lease_owner=None,
                    lease_until=0,
                )
        return count

    def _save(self, oid, owner, **values):
        with self.db.transaction() as s:
            return (
                s.execute(
                    update(Fulfillment)
                    .where(
                        Fulfillment.order_id == oid,
                        Fulfillment.lease_owner == owner,
                        Fulfillment.status == "processing",
                    )
                    .values(**values)
                ).rowcount
                == 1
            )

    def _process(self, oid, owner):
        with self.db.transaction() as s:
            order, job = s.get(Order, oid), s.get(Fulfillment, oid)
            permissions = {
                a.permission
                for a in product_accesses(s, job.authenticated_user_id)
                if a.order_id == oid
            }
            required = set()
            if job.video_state != "not_required":
                required.add("sample_video_access")
            if job.vip_state != "not_required":
                required.add("vip_permanent_access")
            if (
                order.status != "paid"
                or order.linked_telegram_id != job.authenticated_user_id
                or not required <= permissions
            ):
                # No delivery on financial revocation or missing distribution permission.
                allowed = False
            else:
                allowed = True
            user_id, group_id = job.authenticated_user_id, order.telegram_group_id
            video, vip, attempts = job.video_state, job.vip_state, job.attempts
        if not allowed:
            self._save(
                oid,
                owner,
                status="cancelled",
                lease_owner=None,
                lease_until=0,
                last_error="delivery_not_authorized",
            )
            return
        delay = min(30 * 2 ** min(attempts - 1, 6), 1800)
        error = None
        if video not in DONE | {"uncertain"}:
            result = deliver_video(self.db, self.bot_id, user_id, self.video_sender)
            video = (
                "sent"
                if result in {"sent", "already_sent"}
                else "uncertain"
                if result == "uncertain"
                else "retry"
            )
            delay = max(delay, getattr(self.video_sender, "retry_after", 0))
            if not self._save(oid, owner, video_state=video):
                return
            if video == "retry":
                error = "video_temporarily_unavailable"
        if vip == "sending":
            # Restart/crash after committing intent: delivery may have happened.
            vip = "uncertain"
            if not self._save(oid, owner, vip_state=vip):
                return
        if vip not in DONE | {"uncertain"}:
            if not self.access or not self.invite_sender:
                vip, error = "retry", "vip_automation_unavailable"
            elif group_id != self.access.chat_id:
                vip, error = "retry", "vip_group_mismatch"
            else:
                from vip.groups import GroupError

                try:
                    ticket = self.access.issue_invite(user_id)
                    if ticket.status != "already_member" and not ticket.url:
                        raise GroupError("vip_invite_unavailable")
                except GroupError as failure:
                    vip, error = "retry", failure.code
                    delay = max(delay, failure.retry_after)
                else:
                    # Reserve notification durably before the external send.
                    if not self._save(oid, owner, vip_state="sending"):
                        return
                    try:
                        self.invite_sender.send_invite(user_id, ticket.url)
                        vip = "sent"
                    except DeliverySendError as failure:
                        vip = "retry" if failure.code == "rejected" else "uncertain"
                        error, delay = "vip_notification_" + vip, max(delay, failure.retry_after)
                    except Exception:
                        vip, error = "uncertain", "vip_notification_uncertain"
            if not self._save(oid, owner, vip_state=vip):
                return
        if video in DONE and vip in DONE:
            status = "complete"
        elif "retry" in (video, vip) or "pending" in (video, vip):
            status = "queued"
        else:
            status = "review"
        self._save(
            oid,
            owner,
            status=status,
            lease_owner=None,
            lease_until=0,
            next_check_at=int(self.clock()) + delay,
            last_error=error,
        )
