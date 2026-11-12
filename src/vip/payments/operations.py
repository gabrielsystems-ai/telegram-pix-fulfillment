import hashlib
from decimal import Decimal
from time import time

from sqlalchemy import select, update

from vip.db import Database
from vip.errors import DomainError, GatewayError, IntegrationUnavailable
from vip.models import AuditEvent, LinkToken, OmegaCharge, OmegaEvent, Order, Subscription, User
from vip.orders import lock_order
from vip.payments.contracts import Buyer, ChargeRequest, TransactionSnapshot, VerifiedPayment
from vip.payments.omega import OmegaGateway
from vip.payments.service import record_verified_payment
from vip.payments.validation import decode_object, text

EVENTS = frozenset(
    {
        "TRANSACTION_CREATED",
        "TRANSACTION_PAID",
        "TRANSACTION_CANCELED",
        "TRANSACTION_REFUNDED",
        "TRANSACTION_CHARGED_BACK",
    }
)
FINAL_NEGATIVE = frozenset({"FAILED", "EXPIRED", "REFUNDED", "CHARGED_BACK"})


def event_key(transaction_id: str, name: str) -> str:
    # Local idempotency reference, not an invented provider event ID.
    return hashlib.sha256(f"omega\0{transaction_id}\0{name}".encode()).hexdigest()


class OmegaPayments:
    def __init__(self, db: Database, gateway: OmegaGateway, *, clock=time):
        self.db = db
        self.gateway = gateway
        self.clock = clock

    def enabled(self):
        self.gateway.require_enabled()

    def _view(self, order: Order, charge: OmegaCharge) -> dict:
        effective_status = order.status
        if effective_status == "pending" and order.expires_at <= int(self.clock()):
            effective_status = "expired"
        return {
            "order_id": order.id,
            "transaction_id": order.provider_transaction_id,
            "status": effective_status,
            "financial_status": charge.financial_status,
            "amount_minor": order.amount_minor,
            "currency": order.currency,
            "eligible_for_linking": order.status == "paid",
            "copy_paste": charge.copy_paste,
            "expires_at": charge.expires_at,
            "order_expires_at": order.expires_at,
            "webhook_ready": charge.webhook_token_hash is not None,
        }

    def create_charge(self, order_id: str, client: Buyer) -> dict:
        self.gateway.require_enabled(creation=True)
        now = int(self.clock())
        with self.db.transaction() as session:
            order = lock_order(session, order_id)
            if order.payment_provider != "omega":
                raise DomainError("payment_provider_mismatch")
            if order.status != "pending" or order.expires_at <= now:
                raise DomainError("order_not_payable")
            existing = session.get(OmegaCharge, order_id)
            if existing is not None:
                if existing.state == "ready":
                    return self._view(order, existing)
                if existing.state == "creating":
                    raise DomainError("charge_creation_in_progress")
                reconcile = True
            else:
                if order.provider_transaction_id is not None:
                    raise DomainError("transaction_already_attached")
                session.add(
                    OmegaCharge(
                        order_id=order.id, identifier=order.id, state="creating", created_at=now
                    )
                )
                session.add(
                    AuditEvent(action="pix_creation_started", entity_id=order.id, occurred_at=now)
                )
                request = ChargeRequest(
                    order.id, order.amount_minor, order.currency, order.id, client
                )
                reconcile = False
        if reconcile:
            return self.reconcile(order_id)
        try:
            result = self.gateway.create_pix_charge(request)
        except (GatewayError, IntegrationUnavailable):
            self._uncertain(order_id)
            raise
        # A reservation survives failures here. A subsequent attempt can never POST again.
        with self.db.transaction() as session:
            order = lock_order(session, order_id)
            charge = session.get(OmegaCharge, order_id)
            if order.provider_transaction_id not in (None, result.transaction_id):
                raise DomainError("transaction_conflict")
            order.provider_transaction_id = result.transaction_id
            charge.copy_paste = result.copy_paste
            charge.expires_at = result.expires_at
            if charge.last_checked_at is None:
                charge.financial_status = result.financial_status
            if result.webhook_token:
                charge.webhook_token_hash = hashlib.sha256(
                    result.webhook_token.encode()
                ).hexdigest()
            charge.state = "ready"
            session.add(AuditEvent(action="pix_created", entity_id=order_id, occurred_at=now))
            return self._view(order, charge)

    def _uncertain(self, order_id: str):
        with self.db.transaction() as session:
            lock_order(session, order_id)
            charge = session.get(OmegaCharge, order_id)
            if charge.state == "creating":
                charge.state = "uncertain"
                session.add(
                    AuditEvent(
                        action="pix_creation_uncertain",
                        entity_id=order_id,
                        occurred_at=int(self.clock()),
                    )
                )

    def reconcile(self, order_id: str) -> dict:
        self.enabled()
        now = int(self.clock())
        with self.db.transaction() as session:
            order = lock_order(session, order_id)
            if order.payment_provider != "omega":
                raise DomainError("payment_provider_mismatch")
            charge = session.get(OmegaCharge, order_id)
            if charge is None:
                raise DomainError("charge_not_found")
            if charge.next_reconcile_at > now:
                raise DomainError("reconciliation_rate_limited")
            charge.next_reconcile_at = now + 60
            transaction_id, identifier = order.provider_transaction_id, charge.identifier
        # Never hold a DB transaction open while waiting for Omega HTTP.
        snapshot = (
            self.gateway.query_payment(transaction_id)
            if transaction_id
            else (self.gateway.query_payment(client_identifier=identifier))
        )
        with self.db.transaction() as session:
            order = lock_order(session, order_id)
            charge = session.get(OmegaCharge, order_id)
            self._validate(order, charge, snapshot)
            order.provider_transaction_id = snapshot.transaction_id
            if snapshot.copy_paste:
                charge.copy_paste = snapshot.copy_paste
            charge.state = "ready"
            self._apply(session, order, charge, snapshot, int(self.clock()), allow_paid=True)
            return self._view(order, charge)

    @staticmethod
    def _validate(order: Order, charge: OmegaCharge, snapshot: TransactionSnapshot):
        expected = Decimal(order.amount_minor) / Decimal(100)
        if snapshot.client_identifier != charge.identifier or order.provider_transaction_id not in (
            None,
            snapshot.transaction_id,
        ):
            raise DomainError("transaction_mismatch")
        # This application creates only BRL Pix with no financing, adjustments or split.
        # Both sale value (before provider fees) and buyer charge must equal the frozen price.
        if (
            snapshot.currency != "BRL"
            or snapshot.payment_method != "PIX"
            or snapshot.purchase_type != "ONCE"
            or snapshot.exchange_rate != Decimal(1)
            or snapshot.producer_extra_amount != Decimal(0)
            or snapshot.amount != expected
            or snapshot.charge_amount != expected
        ):
            raise DomainError("payment_mismatch")
        if snapshot.status == "COMPLETED" and (
            snapshot.paid_at is None or snapshot.refunded_at is not None
        ):
            raise DomainError("inconsistent_payment")

    def _apply(self, session, order, charge, snapshot, now, *, allow_paid: bool):
        previous = charge.financial_status if charge.last_checked_at is not None else None
        state = snapshot.status
        if (
            previous == "CHARGED_BACK"
            or previous == "REFUNDED"
            and state != "CHARGED_BACK"
            or previous in {"FAILED", "EXPIRED"}
            and state in {"COMPLETED", "PENDING"}
            or previous == "COMPLETED"
            and state in {"PENDING", "FAILED", "EXPIRED"}
        ):
            return
        if state == "COMPLETED" and not allow_paid:
            return
        key = event_key(snapshot.transaction_id, "observed:" + state)
        if session.get(OmegaEvent, key) is not None:
            return
        charge.financial_status = state
        charge.last_checked_at = now
        if state == "COMPLETED":
            if order.status == "pending" and order.expires_at > now:
                record_verified_payment(
                    session,
                    VerifiedPayment(
                        event_id=key,
                        transaction_id=snapshot.transaction_id,
                        order_id=order.id,
                        amount_minor=order.amount_minor,
                        currency="BRL",
                        status="paid",
                    ),
                    now=now,
                )
            elif order.status != "paid":
                session.add(
                    AuditEvent(
                        action="late_payment_review_required", entity_id=order.id, occurred_at=now
                    )
                )
        elif state in FINAL_NEGATIVE:
            if state in {"REFUNDED", "CHARGED_BACK"} or order.status != "paid":
                order.status = "expired" if state == "EXPIRED" else "cancelled"
                session.execute(
                    update(LinkToken).where(LinkToken.order_id == order.id).values(consumed_at=now)
                )
                self._revoke_internal(session, order, now)
        session.add(OmegaEvent(id=key, order_id=order.id, name=state, created_at=now))
        session.add(
            AuditEvent(action="omega_" + state.lower(), entity_id=order.id, occurred_at=now)
        )

    @staticmethod
    def _revoke_internal(session, order, now):
        from vip.entitlements import revoke_product_order

        revoke_product_order(session, order, now)
        if order.linked_telegram_id is None:
            return
        session.execute(
            update(User)
            .where(User.telegram_id == order.linked_telegram_id)
            .values(telegram_id=User.telegram_id)
        )
        revoked = session.scalar(select(Subscription).where(Subscription.order_id == order.id))
        if revoked:
            revoked.status = "expired"
        session.flush()
        # Renewals accumulate time. Remove revoked purchase duration from the remaining
        # paid purchases so a later renewal cannot retain refunded days.
        rows = session.execute(
            select(Subscription, Order.duration_days)
            .join(Order, Subscription.order_id == Order.id)
            .where(
                Subscription.telegram_id == order.linked_telegram_id,
                Subscription.telegram_group_id == order.telegram_group_id,
                Order.status == "paid",
            )
            .order_by(Subscription.starts_at, Subscription.id)
        ).all()
        end = 0
        for subscription, days in rows:
            end = max(end, subscription.starts_at) + days * 86400
            subscription.expires_at = end
            subscription.status = "active" if end > now else "expired"

    def process_webhook(self, raw_body: bytes) -> dict:
        self.enabled()
        try:
            data = decode_object(raw_body)
            tx_id = (
                text(data.get("transaction", {}).get("id"))
                if isinstance(data.get("transaction"), dict)
                else None
            )
            text(data.get("token"), maximum=1000)
        except GatewayError:
            raise DomainError("invalid_webhook") from None
        event = data.get("event")
        if not isinstance(event, str) or event not in EVENTS or tx_id is None:
            raise DomainError("invalid_webhook")
        key = event_key(tx_id, "notification:" + event)
        with self.db.transaction() as session:
            order_id = session.scalar(
                select(Order.id).where(Order.provider_transaction_id == tx_id)
            )
            if order_id is None:
                raise DomainError("unknown_webhook_transaction")
            order = lock_order(session, order_id)
            charge = session.get(OmegaCharge, order_id)
            if charge is None or charge.webhook_token_hash is None:
                raise IntegrationUnavailable
            try:
                self.gateway.verify_webhook(
                    raw_body, {}, expected_token_hash=charge.webhook_token_hash
                )
            except GatewayError:
                raise DomainError("invalid_webhook_token") from None
            if session.get(OmegaEvent, key) is not None:
                return {"status": "ok", "duplicate": True}
        snapshot = self.gateway.query_payment(tx_id)
        expected = {
            "TRANSACTION_PAID": {"COMPLETED", "REFUNDED", "CHARGED_BACK"},
            "TRANSACTION_REFUNDED": {"REFUNDED", "CHARGED_BACK"},
            "TRANSACTION_CHARGED_BACK": {"CHARGED_BACK"},
            "TRANSACTION_CANCELED": FINAL_NEGATIVE,
        }
        if event in expected and snapshot.status not in expected[event]:
            raise DomainError("webhook_not_confirmed")
        with self.db.transaction() as session:
            order = lock_order(session, order_id)
            charge = session.get(OmegaCharge, order_id)
            self._validate(order, charge, snapshot)
            if session.get(OmegaEvent, key) is not None:
                return {"status": "ok", "duplicate": True}
            now = int(self.clock())
            self._apply(
                session, order, charge, snapshot, now, allow_paid=event != "TRANSACTION_CREATED"
            )
            session.add(OmegaEvent(id=key, order_id=order.id, name=event, created_at=now))
            return {"status": "ok", "duplicate": False}
