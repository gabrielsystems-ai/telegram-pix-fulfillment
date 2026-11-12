from time import time
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vip.entitlements import cancel_recovery
from vip.models import AuditEvent, Product, RecoveryTask, VIPSyncLease
from vip.recovery_rules import purchase_blocker
from vip.users import lock_buyer, register_user

TERMINAL = {"sent", "canceled", "skipped", "uncertain"}


class DeliveryError(Exception):
    def __init__(self, kind, *, retry_after=60):
        self.kind, self.retry_after = kind, max(5, retry_after)
        super().__init__(kind)


def record_start(session, telegram_id, *, delay_seconds=1200, now=None):
    now = int(time()) if now is None else now
    register_user(session, telegram_id)
    lock_buyer(session, telegram_id)
    task = session.get(RecoveryTask, telegram_id)
    if task is None:
        task = RecoveryTask(
            telegram_id=telegram_id,
            first_started_at=now,
            due_at=now + delay_seconds,
            next_check_at=now + delay_seconds,
            state="pending",
        )
        session.add(task)
        session.flush()
        session.add(
            AuditEvent(action="recovery_scheduled", entity_id=str(telegram_id), occurred_at=now)
        )
    if purchase_blocker(session, telegram_id, now) == "purchased":
        cancel_recovery(session, telegram_id, now)
    return task


def opt_out(session, telegram_id, *, now=None):
    now = int(time()) if now is None else now
    task = record_start(session, telegram_id, now=now)
    task.opted_out_at = now
    task.eligible_at = None
    if task.state not in {"sent", "uncertain"}:
        task.state = "canceled"
    session.add(
        AuditEvent(action="recovery_opted_out", entity_id=str(telegram_id), occurred_at=now)
    )


def opt_in(session, telegram_id, *, now=None):
    now = int(time()) if now is None else now
    task = record_start(session, telegram_id, now=now)
    if task.opted_in_at is None and task.opted_out_at is None:
        task.opted_in_at = now
        session.add(
            AuditEvent(action="recovery_opted_in", entity_id=str(telegram_id), occurred_at=now)
        )


class RecoveryService:
    """Persistent qualification; real delivery requires explicit compatibility approval."""

    def __init__(self, settings, db, sender=None, *, clock=time):
        self.settings, self.db, self.sender, self.clock = settings, db, sender, clock

    def now(self):
        return int(self.clock())

    def _guard(self, session, owner):
        claimed = session.execute(
            update(VIPSyncLease)
            .where(VIPSyncLease.key == "recovery", VIPSyncLease.owner == owner)
            .values(expires_at=self.now() + 300)
        )
        if claimed.rowcount != 1:
            raise RuntimeError("Recovery lease lost")

    def _evaluate(self, session, task):
        now = self.now()
        if task.state in TERMINAL or task.due_at > now:
            return False
        if task.state == "sending":
            if task.send_attempt_at is not None and task.send_attempt_at + 300 <= now:
                task.state, task.last_error = "uncertain", "interrupted_send"
            return False
        reason = purchase_blocker(session, task.telegram_id, now)
        if reason == "purchased":
            cancel_recovery(session, task.telegram_id, now)
            return False
        if task.opted_out_at is not None or task.blocked_at is not None:
            task.state, task.eligible_at = "canceled", None
            return False
        offer = session.get(Product, "recovery_bundle")
        if not offer or not offer.active or not offer.rights_reference:
            reason = "offer_unavailable"
        if reason:
            task.last_error, task.next_check_at = reason, now + 60
            # Keep qualification history, but all checkout/send decisions revalidate.
            return False
        if task.eligible_at is None:
            task.eligible_at = now
            session.add(
                AuditEvent(
                    action="recovery_qualified", entity_id=str(task.telegram_id), occurred_at=now
                )
            )
        task.state, task.last_error, task.next_check_at = "eligible", None, now + 60
        return True

    def _process(self, telegram_id, owner):
        with self.db.transaction() as session:
            self._guard(session, owner)
            lock_buyer(session, telegram_id)
            task = session.get(RecoveryTask, telegram_id)
            if not self._evaluate(session, task):
                return
            if not (
                self.settings.recovery_promotions_approved
                and self.sender
                and task.opted_in_at is not None
            ):
                return
            task.state, task.send_attempt_at = "sending", self.now()
            task.attempts += 1
            task.next_check_at = self.now() + 300
        # Durable intent precedes the remote call. Never replay a stale sending intent.
        with self.db.transaction() as session:
            self._guard(session, owner)
            lock_buyer(session, telegram_id)
            task = session.get(RecoveryTask, telegram_id)
            if task.state != "sending":
                return
            now = self.now()
            reason = purchase_blocker(session, telegram_id, now)
            offer = session.get(Product, "recovery_bundle")
            if (
                reason
                or task.opted_out_at is not None
                or task.blocked_at is not None
                or not (offer and offer.active and offer.rights_reference)
            ):
                task.state = (
                    "canceled"
                    if reason == "purchased" or task.opted_out_at is not None
                    else "pending"
                )
                task.last_error, task.next_check_at = reason or "eligibility_changed", now + 60
                return
            try:
                message_id = self.sender.send(telegram_id, self.settings.recovery_template)
                if type(message_id) is not int or message_id <= 0:
                    raise DeliveryError("ambiguous")
            except DeliveryError as error:
                task.last_error = "delivery_" + error.kind
                if error.kind == "blocked":
                    task.state, task.blocked_at, task.eligible_at = "canceled", now, None
                elif error.kind == "retry":
                    task.state, task.next_check_at = "failed", now + error.retry_after
                else:
                    task.state = "uncertain"
                session.add(
                    AuditEvent(
                        action="recovery_delivery_failed",
                        entity_id=str(telegram_id),
                        occurred_at=now,
                    )
                )
                return
            except Exception:
                task.state, task.last_error = "uncertain", "delivery_ambiguous"
                return
            task.state, task.sent_at, task.message_id = "sent", self.now(), message_id
            task.last_error = None
            session.add(
                AuditEvent(
                    action="recovery_sent", entity_id=str(telegram_id), occurred_at=self.now()
                )
            )

    def sync_once(self, stop=None):
        if not self.settings.recovery_enabled:
            return {"enabled": False, "processed": 0}
        owner, now = uuid4().hex, self.now()
        with self.db.transaction() as session:
            insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
            session.execute(
                insert(VIPSyncLease)
                .values(key="recovery", owner="", expires_at=0)
                .on_conflict_do_nothing(index_elements=[VIPSyncLease.key])
            )
            result = session.execute(
                update(VIPSyncLease)
                .where(VIPSyncLease.key == "recovery", VIPSyncLease.expires_at <= now)
                .values(owner=owner, expires_at=now + 300)
            )
            if result.rowcount != 1:
                return {"busy": True, "processed": 0}
        processed = 0
        try:
            with self.db.transaction() as session:
                users = list(
                    session.scalars(
                        select(RecoveryTask.telegram_id)
                        .where(
                            RecoveryTask.next_check_at <= now, RecoveryTask.state.not_in(TERMINAL)
                        )
                        .order_by(RecoveryTask.next_check_at)
                        .limit(100)
                    )
                )
            for user_id in users:
                if stop is not None and stop.is_set():
                    break
                self._process(user_id, owner)
                processed += 1
            return {"enabled": True, "processed": processed}
        finally:
            with self.db.transaction() as session:
                session.execute(
                    update(VIPSyncLease)
                    .where(VIPSyncLease.key == "recovery", VIPSyncLease.owner == owner)
                    .values(expires_at=0)
                )
