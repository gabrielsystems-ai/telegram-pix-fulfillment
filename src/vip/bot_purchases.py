"""Private-chat adapter over existing orders, OmegaPayments and fulfillment.

The caller supplies identity only from an authentic Telegram update. Real
financial operation remains opt-in; platform limitations are documented.
"""

import hashlib
from time import time
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select

from vip.catalog import OFFERS, create_product_order
from vip.entitlements import product_accesses
from vip.errors import DomainError, GatewayError, IntegrationUnavailable
from vip.fulfillment import fulfillment_view
from vip.models import (
    BotPurchase,
    Fulfillment,
    OmegaCharge,
    Order,
    PixReplacement,
    Product,
    PushinCharge,
)
from vip.orders import lock_order
from vip.payments.contracts import Buyer
from vip.payments.omega import OmegaGateway
from vip.payments.operations import OmegaPayments
from vip.payments.pushinpay import PushinGateway, PushinPayments
from vip.purchase_attempts import link_replacement, lock_attempt, root_order_id, settled_order_id
from vip.users import lock_buyer, register_user

CREATION_GRACE_SECONDS = 60


class BotPurchases:
    def __init__(self, db, settings, *, payments=None, clock=time):
        self.db, self.settings, self.clock = db, settings, clock
        self.payments = payments or (
            PushinPayments(db, PushinGateway(settings), clock=clock)
            if settings.payment_provider == "pushinpay"
            else OmegaPayments(db, OmegaGateway(settings), clock=clock)
        )

    def _service(self, provider):
        if provider == self.settings.payment_provider:
            return self.payments
        if provider == "omega":
            return OmegaPayments(self.db, OmegaGateway(self.settings), clock=self.clock)
        return PushinPayments(self.db, PushinGateway(self.settings), clock=self.clock)

    @staticmethod
    def _charge(session, order):
        return session.get(
            PushinCharge if order.payment_provider == "pushinpay" else OmegaCharge, order.id
        )

    def require_enabled(self):
        if not (
            self.settings.pushinpay_enabled
            if self.settings.payment_provider == "pushinpay"
            else self.settings.omega_pix_enabled
        ):
            raise IntegrationUnavailable
        self.payments.gateway.require_enabled(creation=True)

    def available(self):
        try:
            self.require_enabled()
            return True
        except IntegrationUnavailable:
            return False

    def can_buy(self, product_code):
        if not self.available() or product_code not in {"sample_video", "vip_permanent"}:
            return False
        with self.db.transaction() as s:
            product = s.get(Product, product_code)
            return bool(
                product
                and product.active
                and not product.recovery_only
                and product.rights_reference
                and product.amount_minor == OFFERS[product_code][1]
            )

    def _current(self, session, user_id, order_id=None, *, product_code=None):
        query = select(BotPurchase).where(BotPurchase.telegram_id == user_id)
        if order_id is not None:
            query = query.where(BotPurchase.order_id == order_id)
        if product_code is not None:
            query = query.where(BotPurchase.product_code == product_code)
        current = session.scalar(query.order_by(BotPurchase.selection_order.desc()).limit(1))
        historical = session.get(Order, order_id) if current is None and order_id else None
        if (
            historical
            and historical.buyer_telegram_id == user_id
            and session.get(PixReplacement, historical.id)
        ):
            current = session.get(BotPurchase, (user_id, historical.product_id))
            if current and root_order_id(session, current.order_id) != root_order_id(
                session, historical.id
            ):
                current = None
        if current is None:
            raise DomainError("bot_purchase_not_found")
        order = historical or session.get(Order, current.order_id)
        job = session.get(Fulfillment, order.id)
        if (
            order.buyer_telegram_id != user_id
            or current.product_code != order.product_id
            or not job
            or job.authenticated_user_id != user_id
        ):
            raise DomainError("bot_purchase_not_found")
        return current, order

    def begin(self, user_id, product_code):
        data = self._begin(user_id, product_code)
        return self._create_if_needed(user_id, data)

    def _create_if_needed(self, user_id, data):
        if (
            data["payment_provider"] == "pushinpay"
            and data["charge_state"] == "ready"
            and data["state"] == "validity_unverified"
        ):
            # Explicit buyer resume only: authenticated GET of this exact Pix.
            # No replacement POST and no renewal for legacy/uncertain attempts.
            return self._resume_existing(user_id, data)
        if (
            data["payment_provider"] == "pushinpay"
            and data["charge_state"] is None
            and data["state"] == "pix_pending"
        ):
            self._service("pushinpay").create_charge(data["order_id"])
            return self.status(user_id, data["order_id"])
        return data

    def replace(self, user_id, order_id):
        self.require_enabled()
        with self.db.transaction() as s:
            _, old = self._current(s, user_id, order_id)
            product = old.product_id
        data = self._begin(user_id, product, replace_order_id=order_id)
        return self._create_if_needed(user_id, data)

    def _resume_existing(self, user_id, data):
        try:
            self._service("pushinpay").reconcile(
                data["order_id"], resume_window_seconds=self.settings.order_ttl_seconds
            )
        except (DomainError, GatewayError) as error:
            if error.code not in {
                "pushinpay_expiration_unverified",
                "reconciliation_rate_limited",
            }:
                raise
            result = self.status(user_id, data["order_id"])
            return result
        return self.status(user_id, data["order_id"])

    def _begin(self, user_id, product_code, *, replace_order_id=None):
        self.require_enabled()
        if product_code not in {"sample_video", "vip_permanent"}:
            raise DomainError("unavailable_product")
        now = int(self.clock())
        with self.db.transaction() as s:
            register_user(s, user_id)
            lock_buyer(s, user_id)
            if set(OFFERS[product_code][2]) <= {a.permission for a in product_accesses(s, user_id)}:
                raise DomainError("product_already_owned")
            selection_order = (
                s.scalar(
                    select(func.max(BotPurchase.selection_order)).where(
                        BotPurchase.telegram_id == user_id
                    )
                )
                or 0
            ) + 1
            current = s.get(BotPurchase, (user_id, product_code))
            previous_order = None
            if current:
                current.selection_order = selection_order
                order = s.get(Order, current.order_id)
                if order.payment_provider == "pushinpay":
                    order = lock_attempt(s, current.order_id)
                charge = self._charge(s, order)
                if replace_order_id and order.id != replace_order_id:
                    requested = s.get(Order, replace_order_id)
                    if (
                        not requested
                        or requested.buyer_telegram_id != user_id
                        or requested.product_id != product_code
                        or not s.get(PixReplacement, replace_order_id)
                        or root_order_id(s, requested.id) != root_order_id(s, order.id)
                    ):
                        raise DomainError("bot_purchase_not_found")
                    return self._view(s, user_id, order.id)
                replacing = bool(replace_order_id)
                if replacing:
                    if (
                        order.payment_provider != "pushinpay"
                        or self.settings.payment_provider != "pushinpay"
                    ):
                        raise DomainError("payment_provider_mismatch")
                    state = self._view(s, user_id, order.id)["state"]
                    if state not in {"validity_unverified", "unavailable", "pix_pending"}:
                        return self._view(s, user_id, order.id)
                    if not charge or (
                        charge.state == "creating"
                        and now - charge.created_at < CREATION_GRACE_SECONDS
                    ):
                        raise DomainError("purchase_in_progress")
                    if settled_order_id(s, order.id) or charge.financial_status == "paid":
                        return self._view(s, user_id, order.id)
                    previous_order = order
                if (
                    not replacing
                    and charge
                    and (
                        charge.state in {"creating", "uncertain"}
                        or order.payment_provider == "pushinpay"
                        and charge.financial_status == "paid"
                        or order.payment_provider == "pushinpay"
                        and order.status == "pending"
                        and charge.financial_status not in {"canceled", "expired"}
                    )
                ):
                    if order.product_id != product_code:
                        raise DomainError("purchase_reconciliation_required")
                    return self._view(s, user_id, order.id)
                if not replacing and order.status == "pending" and order.expires_at > now:
                    if order.product_id != product_code:
                        raise DomainError("purchase_in_progress")
                    return self._view(s, user_id, order.id)
                if order.payment_provider == "pushinpay":
                    previous_order = order
            elif replace_order_id:
                raise DomainError("bot_purchase_not_found")
            product = s.get(Product, product_code)
            if not product or product.amount_minor != OFFERS[product_code][1]:
                raise DomainError("product_price_mismatch")
            order = create_product_order(
                s,
                product_code,
                user_id,
                "bot-" + uuid4().hex,
                ttl_seconds=self.settings.order_ttl_seconds,
                now=now,
                payment_provider=self.settings.payment_provider,
            )
            if previous_order:
                link_replacement(s, previous_order, order, now)
            permissions = OFFERS[product_code][2]
            s.add(
                Fulfillment(
                    order_id=order.id,
                    authenticated_user_id=user_id,
                    verified_at=now,
                    video_state="pending"
                    if "sample_video_access" in permissions
                    else "not_required",
                    vip_state="pending"
                    if "vip_permanent_access" in permissions
                    else "not_required",
                )
            )
            if current is None:
                current = BotPurchase(
                    telegram_id=user_id, product_code=product_code, order_id=order.id
                )
                s.add(current)
            current.selection_order = selection_order
            current.order_id = order.id
            current.stage = "payment" if order.payment_provider == "pushinpay" else "buyer"
            current.pix_message_id = current.pix_file_id = None
            current.pix_code_hash = None
            s.flush()
            return self._view(s, user_id, order.id)

    def _view(self, s, user_id, order_id=None, *, product_code=None):
        current, order = self._current(s, user_id, order_id, product_code=product_code)
        charge = self._charge(s, order)
        data = fulfillment_view(s, order.id, now=int(self.clock()))
        data.update(
            product_code=order.product_id,
            product_name=order.product_name,
            amount_minor=order.amount_minor,
            currency=order.currency,
            charge_state=charge.state if charge else None,
            payment_provider=order.payment_provider,
            copy_paste=None,
            expires_at=order.expires_at,
            pix_message_id=current.pix_message_id if current.order_id == order.id else None,
            pix_file_id=current.pix_file_id if current.order_id == order.id else None,
            replacement_allowed=False,
        )
        if job := s.get(Fulfillment, order.id):
            if job.last_error == "duplicate_payment_review_required":
                data.update(
                    state="payment_review",
                    message="Recebemos um pagamento adicional. "
                    "Fale com o suporte para conferir a devolução.",
                )
                return data
        winner = settled_order_id(s, order.id)
        if winner and winner != order.id:
            paid_view = fulfillment_view(s, winner, now=int(self.clock()))
            data.update({k: v for k, v in paid_view.items() if k != "order_id"})
            return data
        if s.get(PixReplacement, order.id) and order.status != "paid":
            data.update(
                state="replaced",
                message="Este Pix foi substituído. "
                "Use apenas o Pix mais recente. Se já pagou, aguarde a confirmação.",
            )
            return data
        # An uncertain POST is retained. Only an explicit, separately linked
        # replacement can create another Pix; this code never retries that POST.
        if order.status == "pending" and charge and charge.state in {"creating", "uncertain"}:
            data.update(
                state="pix_pending",
                message="Ainda não foi possível confirmar a geração do Pix. "
                "Verifique esta tentativa antes de iniciar outra compra. "
                "Não efetue pagamento sem o código Pix.",
            )
            data["replacement_allowed"] = order.payment_provider == "pushinpay" and (
                charge.state == "uncertain"
                or int(self.clock()) - charge.created_at >= CREATION_GRACE_SECONDS
            )
            if data["replacement_allowed"]:
                data["message"] = (
                    "Você pode gerar um novo Pix para continuar. Se já pagou, não pague novamente."
                )
            return data
        if (
            order.payment_provider == "pushinpay"
            and charge
            and charge.financial_status == "paid"
            and order.status != "paid"
        ):
            data.update(
                state="payment_review",
                message="Seu pagamento precisa de conferência. "
                "Fale com o suporte; não faça outro pagamento.",
            )
            return data
        if order.payment_provider == "pushinpay" and order.status in {"expired", "cancelled"}:
            data["replacement_allowed"] = bool(charge and charge.state != "creating")
            data["message"] = (
                "Este Pix expirou. Você pode gerar um novo Pix."
                if order.status == "expired"
                else "Este Pix foi cancelado. Você pode gerar um novo Pix."
            )
            return data
        if (
            order.payment_provider == "pushinpay"
            and order.status == "pending"
            and charge
            and charge.state == "ready"
            and (
                order.expires_at <= int(self.clock())
                or charge.expires_at is not None
                and charge.expires_at <= int(self.clock())
            )
        ):
            data.update(
                state="validity_unverified",
                replacement_allowed=True,
                message="Você pode gerar um novo Pix para continuar. "
                "Use apenas o código mais recente. Se já pagou, não pague novamente.",
            )
            return data
        if data["state"] == "awaiting_payment":
            if charge is None:
                if order.payment_provider == "pushinpay":
                    data.update(state="pix_pending", message="Preparando seu Pix.")
                    return data
                if self.settings.payment_provider == "pushinpay":
                    data.update(
                        state="legacy_draft",
                        message="Você tem uma tentativa anterior sem Pix. "
                        "Cancele essa tentativa para iniciar uma compra.",
                    )
                    return data
                data.update(
                    state="buyer_required",
                    message="📩 Informe seu e-mail para gerar o pagamento Pix.",
                )
            elif charge.expires_at and charge.expires_at <= int(self.clock()):
                data.update(
                    state="unavailable", message="Este Pix expirou. Não efetue o pagamento."
                )
            elif charge.state == "ready":
                if not charge.copy_paste:
                    data.update(
                        state="pix_pending",
                        message="Estamos verificando a geração do Pix. "
                        "Verifique esta tentativa antes de iniciar outra compra.",
                    )
                    return data
                data["copy_paste"] = charge.copy_paste
                data["expires_at"] = min(order.expires_at, charge.expires_at or order.expires_at)
                if hashlib.sha256(charge.copy_paste.encode()).hexdigest() != current.pix_code_hash:
                    data["pix_message_id"] = data["pix_file_id"] = None
        return data

    def status(self, user_id, order_id=None, *, product_code=None):
        with self.db.transaction() as s:
            return self._view(s, user_id, order_id, product_code=product_code)

    def wants_buyer(self, user_id):
        try:
            return self.status(user_id)["state"] == "buyer_required"
        except DomainError:
            return False

    def submit(self, user_id, text, profile_name=None):
        if self.settings.payment_provider == "pushinpay":
            return self.status(user_id)
        self.require_enabled()
        try:
            if len(text) > 500:
                raise ValueError
            if profile_name is None:
                # Retain the internal adapter's earlier explicit-data contract.
                if text.count("|") != 1:
                    raise ValueError
                name, email = (part.strip() for part in text.split("|"))
            else:
                # The declared profile name is not a verified civil identity.
                name, email = profile_name, text.strip()
            client = Buyer(name=name, email=email)
        except (ValidationError, ValueError):
            raise DomainError("invalid_buyer_details") from None
        with self.db.transaction() as s:
            lock_buyer(s, user_id)
            current, order = self._current(s, user_id)
            data = self._view(s, user_id)
            if data["state"] != "buyer_required":
                return data
            current.stage = "payment"
            oid = order.id
        # Payer name/email only exist in this request, never in the database or logs.
        self.payments.create_charge(oid, client)
        return self.status(user_id, oid)

    def refresh(self, user_id, order_id=None):
        data = self.status(user_id, order_id)
        if data["state"] == "validity_unverified":
            return self._resume_existing(user_id, data)
        if data["state"] in {"awaiting_payment", "pix_pending"} and data["charge_state"]:
            try:
                self._service(data["payment_provider"]).reconcile(data["order_id"])
            except IntegrationUnavailable:
                if data["payment_provider"] != self.settings.payment_provider:
                    raise DomainError("legacy_gateway_unavailable") from None
                raise
        return self.status(user_id, order_id)

    def cancel(self, user_id, order_id):
        with self.db.transaction() as s:
            self._current(s, user_id, order_id)
            # Serialize with charge reservation before checking whether a Pix exists.
            lock_order(s, order_id)
            lock_buyer(s, user_id)
            current, order = self._current(s, user_id, order_id)
            if self._charge(s, order) is not None:
                raise DomainError("pix_already_created")
            if order.status != "pending":
                raise DomainError("order_not_payable")
            order.status = "cancelled"
            s.get(Fulfillment, order.id).status = "cancelled"
            s.delete(current)

    def remember_message(self, user_id, order_id, message_id, file_id, code):
        with self.db.transaction() as s:
            current, _ = self._current(s, user_id, order_id)
            current.pix_message_id, current.pix_file_id = message_id, file_id
            current.pix_code_hash = hashlib.sha256(code.encode()).hexdigest()
