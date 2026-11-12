"""Pushin Pay's documented Pix contract; no automatic financial retries."""

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from math import ceil
from time import time
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vip.errors import DomainError, GatewayError, IntegrationUnavailable
from vip.models import (
    AuditEvent,
    Fulfillment,
    PaymentWindowRenewal,
    PushinCharge,
    PushinNotification,
    PushinQueryGate,
)
from vip.payments.contracts import VerifiedPayment
from vip.payments.operations import OmegaPayments
from vip.payments.service import record_verified_payment
from vip.payments.validation import decode_object, text
from vip.purchase_attempts import lock_attempt, settled_order_id

LOG = logging.getLogger(__name__)
STATES = {"created", "paid", "canceled", "expired"}


def expiration_bounds(data):
    """Possible expiry interval, without inventing a missing provider timezone.

    A naive date between the UTC+14 and UTC-12 bounds is ambiguous, not expired.
    Explicit offsets identify a single instant. These bounds are application
    safety limits, not a claim about the provider's actual timezone.
    """
    details = data.get("pix_details")
    raw = details.get("expiration_date") if isinstance(details, dict) else None
    if not isinstance(raw, str) or len(raw) > 100 or not re.match(r"^\d{4}-\d\d-\d\d[ T]", raw):
        LOG.warning("pushinpay_validity_unverified reason=expiration_missing_or_invalid")
        raise GatewayError("pushinpay_expiration_unverified")
    try:
        expiry = datetime.fromisoformat(raw)
        if expiry.tzinfo is None:
            earliest = expiry.replace(tzinfo=timezone(timedelta(hours=14)))
            latest = expiry.replace(tzinfo=timezone(timedelta(hours=-12)))
            return raw, int(earliest.timestamp()), ceil(latest.timestamp())
        return raw, int(expiry.timestamp()), ceil(expiry.timestamp())
    except (ValueError, OverflowError, OSError):
        LOG.warning("pushinpay_validity_unverified reason=expiration_invalid")
        raise GatewayError("pushinpay_expiration_unverified") from None


def transaction_id(value):
    try:
        return str(UUID(text(value)))
    except ValueError:
        raise GatewayError("pushinpay_invalid_transaction") from None


def cents(value):
    if type(value) is int and value >= 50:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value) and int(value) >= 50:
        return int(value)
    raise GatewayError("pushinpay_invalid_amount")


class PushinGateway:
    def __init__(self, settings, *, transport=None):
        self._settings, self._transport = settings, transport

    def require_enabled(self, *, creation=False):
        s = self._settings
        if not (s.pushinpay_enabled and s.pushinpay_token):
            raise IntegrationUnavailable
        if creation and not (s.pushinpay_callback_url and s.pushinpay_webhook_token):
            raise IntegrationUnavailable

    def _request(self, method, path, *, body=None):
        self.require_enabled(creation=method == "POST")
        try:
            with httpx.Client(
                base_url=str(self._settings.pushinpay_base_url).rstrip("/") + "/",
                headers={
                    "Authorization": "Bearer " + self._settings.pushinpay_token.get_secret_value(),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = client.request(method, path, json=body)
        except httpx.TransportError as error:
            code = (
                "pushinpay_timeout"
                if isinstance(error, httpx.TimeoutException)
                else "pushinpay_unavailable"
            )
            LOG.warning("pushinpay_request_failed method=%s http_status=none code=%s", method, code)
            raise GatewayError(code) from None
        if not response.is_success:
            code = {
                401: "pushinpay_authentication_failed",
                403: "pushinpay_authentication_failed",
                404: "pushinpay_not_found",
                429: "pushinpay_rate_limited",
            }.get(response.status_code, "pushinpay_request_rejected")
            LOG.warning(
                "pushinpay_request_failed method=%s http_status=%s code=%s",
                method,
                response.status_code,
                code,
            )
            raise GatewayError(code)
        try:
            return decode_object(response.content)
        except GatewayError:
            raise GatewayError("pushinpay_invalid_response") from None

    def create_pix_charge(self, amount_minor):
        if type(amount_minor) is not int:
            raise GatewayError("pushinpay_invalid_amount")
        data = self._request(
            "POST",
            "pix/cashIn",
            body={
                "value": cents(amount_minor),
                "webhook_url": str(self._settings.pushinpay_callback_url),
            },
        )
        self.validate(data)
        text(data.get("qr_code"), maximum=20000)
        return data

    def query_payment(self, identifier):
        data = self._request("GET", "transactions/" + transaction_id(identifier))
        self.validate(data)
        return data

    @staticmethod
    def validate(data):
        transaction_id(data.get("id"))
        cents(data.get("value"))
        if not isinstance(data.get("status"), str) or data["status"] not in STATES:
            raise GatewayError("pushinpay_unknown_status")
        if data.get("payment_type", "pix") != "pix" or data.get("currency", "BRL") != "BRL":
            raise GatewayError("pushinpay_unexpected_payment")


class PushinPayments:
    def __init__(self, db, gateway, *, clock=time):
        self.db, self.gateway, self.clock = db, gateway, clock

    def enabled(self):
        self.gateway.require_enabled()

    def create_charge(self, order_id, client=None):
        self.gateway.require_enabled(creation=True)
        with self.db.transaction() as s:
            order = lock_attempt(s, order_id)
            if order.payment_provider != "pushinpay":
                raise DomainError("payment_provider_mismatch")
            winner = settled_order_id(s, order.id)
            if winner and winner != order.id:
                raise DomainError("product_already_owned")
            existing = s.get(PushinCharge, order_id)
            if existing is not None:
                return  # An existing reservation NEVER causes another POST.
            if order.status != "pending" or order.expires_at <= int(self.clock()):
                raise DomainError("order_not_payable")
            amount = order.amount_minor
            if order.currency != "BRL":
                raise DomainError("payment_mismatch")
            s.add(PushinCharge(order_id=order_id, state="creating", created_at=int(self.clock())))
            s.add(
                AuditEvent(
                    action="pushinpay_creation_started",
                    entity_id=order_id,
                    occurred_at=int(self.clock()),
                )
            )
        try:
            data = self.gateway.create_pix_charge(amount)
            if cents(data["value"]) != amount:
                raise DomainError("payment_mismatch")
        except (GatewayError, DomainError, IntegrationUnavailable):
            with self.db.transaction() as s:
                lock_attempt(s, order_id)
                s.get(PushinCharge, order_id).state = "uncertain"
            raise
        with self.db.transaction() as s:
            order = lock_attempt(s, order_id)
            charge = s.get(PushinCharge, order_id)
            charge.transaction_id = transaction_id(data["id"])
            order.provider_transaction_id = "pushinpay:" + charge.transaction_id
            charge.copy_paste, charge.state = data["qr_code"], "ready"
            # Creation never confirms payment, including a 'paid' creation response.
            s.add(
                AuditEvent(
                    action="pushinpay_created", entity_id=order_id, occurred_at=int(self.clock())
                )
            )

    def _query_slot(self, s, order_id, *, from_notification=False):
        if not from_notification and s.scalar(
            select(PushinCharge.order_id)
            .join(
                PushinNotification,
                PushinNotification.transaction_id == PushinCharge.transaction_id,
            )
            .where(
                PushinNotification.version > PushinNotification.processed_version,
                PushinCharge.order_id != order_id,
            )
            .limit(1)
        ):
            # Keep the provider quota while reserving work for authenticated signals.
            raise DomainError("reconciliation_rate_limited")
        key = hashlib.sha256(
            self.gateway._settings.pushinpay_token.get_secret_value().encode()
        ).hexdigest()
        insert = pg_insert if s.bind.dialect.name == "postgresql" else sqlite_insert
        s.execute(
            insert(PushinQueryGate)
            .values(account_key=key, next_query_at=0)
            .on_conflict_do_nothing()
        )
        now = self.clock()
        result = s.execute(
            update(PushinQueryGate)
            .where(
                PushinQueryGate.account_key == key,
                PushinQueryGate.next_query_at <= now,
            )
            .values(next_query_at=ceil(now + 60))
        )
        if result.rowcount != 1:
            raise DomainError("reconciliation_rate_limited")

    def reconcile(self, order_id, *, resume_window_seconds=None, _from_notification=False):
        self.enabled()
        with self.db.transaction() as s:
            order = lock_attempt(s, order_id)
            charge = s.get(PushinCharge, order_id)
            if order.payment_provider != "pushinpay" or charge is None:
                raise DomainError("payment_provider_mismatch")
            if not charge.transaction_id:
                raise DomainError("pushinpay_transaction_unknown")
            self._query_slot(s, order_id, from_notification=_from_notification)
            tx = charge.transaction_id
            event = s.get(PushinNotification, tx)
            version = event.version if event else 0
            if event:
                event.last_attempt_at = int(self.clock())
        data = self.gateway.query_payment(tx)
        with self.db.transaction() as s:
            order = lock_attempt(s, order_id)
            charge = s.get(PushinCharge, order_id)
            if (
                transaction_id(data["id"]) != tx
                or order.provider_transaction_id != "pushinpay:" + tx
            ):
                raise DomainError("transaction_mismatch")
            if cents(data["value"]) != order.amount_minor or order.currency != "BRL":
                raise DomainError("payment_mismatch")
            state, previous, now = data["status"], charge.financial_status, int(self.clock())
            if previous == "paid" and state in {"canceled", "expired"}:
                # No documented refund/chargeback schema: preserve legitimate access
                # and require an operator to review an inconsistent/reversed status.
                s.add(
                    AuditEvent(
                        action="pushinpay_reversal_review_required",
                        entity_id=order.id,
                        occurred_at=now,
                    )
                )
            elif (previous not in {"canceled", "expired"} or state == "paid") and not (
                previous == "paid" and state == "created"
            ):
                if (
                    resume_window_seconds is not None
                    and state == "created"
                    and order.status == "pending"
                    and (
                        order.expires_at <= now
                        or charge.expires_at is not None
                        and charge.expires_at <= now
                    )
                ):
                    if charge.state != "ready" or not charge.copy_paste:
                        raise DomainError("pushinpay_transaction_unknown")
                    raw_expiry, provider_expiry, latest_expiry = expiration_bounds(data)
                    if now >= latest_expiry:
                        # Authenticated provider date is elapsed even in the latest
                        # possible civil timezone. No new POST in this request.
                        order.status = "expired"
                        charge.expires_at = latest_expiry
                        charge.financial_status = state
                        job = s.get(Fulfillment, order.id)
                        if job:
                            job.status = "cancelled"
                        s.add(
                            AuditEvent(
                                action="pushinpay_expiration_verified",
                                entity_id=order.id,
                                occurred_at=now,
                            )
                        )
                        return
                    if provider_expiry <= now:
                        LOG.warning("pushinpay_validity_unverified reason=expiration_timezone")
                        raise DomainError("pushinpay_expiration_unverified")
                    new_expiry = min(now + resume_window_seconds, provider_expiry)
                    s.add(
                        PaymentWindowRenewal(
                            order_id=order.id,
                            transaction_id=tx,
                            previous_expires_at=order.expires_at,
                            new_expires_at=new_expiry,
                            checked_at=now,
                            provider_expiration=raw_expiry,
                            provider_expires_at=provider_expiry,
                        )
                    )
                    order.expires_at = new_expiry
                    charge.expires_at = provider_expiry
                if previous in {"canceled", "expired"} and state == "paid":
                    s.add(
                        AuditEvent(
                            action="pushinpay_paid_after_" + previous,
                            entity_id=order.id,
                            occurred_at=now,
                        )
                    )
                charge.financial_status = state
                if state == "paid":
                    try:
                        record_verified_payment(
                            s,
                            VerifiedPayment(
                                "pushinpay:" + tx + ":paid",
                                "pushinpay:" + tx,
                                order.id,
                                order.amount_minor,
                                "BRL",
                                "paid",
                            ),
                            now=now,
                            accept_late=True,
                        )
                    except DomainError as error:
                        if error.code != "order_not_payable":
                            raise
                        s.add(
                            AuditEvent(
                                action="late_payment_review_required",
                                entity_id=order.id,
                                occurred_at=now,
                            )
                        )
                elif state in {"canceled", "expired"}:
                    order.status = "cancelled" if state == "canceled" else "expired"
                    OmegaPayments._revoke_internal(s, order, now)
            event = s.get(PushinNotification, tx)
            if event and not (event.payment_hint and state == "created"):
                event.processed_version = max(event.processed_version, version)

    def process_webhook(self, raw_body, token):
        self.enabled()
        expected = self.gateway._settings.pushinpay_webhook_token
        if expected is None:
            raise IntegrationUnavailable
        if not token or not secrets.compare_digest(
            token.encode(), expected.get_secret_value().encode()
        ):
            raise DomainError("invalid_webhook_token")
        try:
            body = decode_object(raw_body)
            tx = transaction_id(body.get("id"))
        except GatewayError:
            raise DomainError("invalid_webhook") from None
        with self.db.transaction() as s:
            insert = pg_insert if s.bind.dialect.name == "postgresql" else sqlite_insert
            s.execute(
                insert(PushinNotification)
                .values(
                    transaction_id=tx,
                    version=1,
                    processed_version=0,
                    last_attempt_at=0,
                    payment_hint=body.get("status") == "paid",
                )
                .on_conflict_do_update(
                    index_elements=["transaction_id"],
                    set_={
                        "version": PushinNotification.version + 1,
                        "payment_hint": PushinNotification.payment_hint
                        | (body.get("status") == "paid"),
                    },
                )
            )
        return {"status": "accepted"}

    def process_pending(self):
        self.enabled()
        with self.db.transaction() as s:
            order_id = s.scalar(
                select(PushinCharge.order_id)
                .join(
                    PushinNotification,
                    PushinNotification.transaction_id == PushinCharge.transaction_id,
                )
                .where(PushinNotification.version > PushinNotification.processed_version)
                .order_by(PushinNotification.last_attempt_at, PushinNotification.transaction_id)
                .limit(1)
            )
        if order_id:
            try:
                self.reconcile(order_id, _from_notification=True)
            except (GatewayError, DomainError) as error:
                if error.code != "reconciliation_rate_limited":
                    LOG.warning("pushinpay_reconciliation_pending code=%s", error.code)
