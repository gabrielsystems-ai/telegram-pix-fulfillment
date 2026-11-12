from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class Buyer(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=True, hide_input_in_errors=True
    )
    name: str = Field(min_length=1, max_length=200, repr=False)
    email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", repr=False
    )
    # Official schema marks phone required but explicitly describes it as optional.
    # Account/acquirer requirements still apply at Omega; do not invent CPF/phone rules.
    phone: str | None = Field(default=None, min_length=1, max_length=40, repr=False)
    document: str | None = Field(default=None, min_length=1, max_length=40, repr=False)


@dataclass(frozen=True)
class ChargeRequest:
    order_id: str
    amount_minor: int
    currency: str
    idempotency_key: str
    client: Buyer | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PixCharge:
    transaction_id: str
    copy_paste: str
    qr_code: str | None
    expires_at: int | None
    webhook_token: str | None = field(default=None, repr=False)
    financial_status: str | None = None


@dataclass(frozen=True)
class TransactionSnapshot:
    transaction_id: str
    client_identifier: str
    amount: Decimal
    charge_amount: Decimal
    currency: str
    status: str
    payment_method: str
    purchase_type: str
    exchange_rate: Decimal
    producer_extra_amount: Decimal
    paid_at: int | None
    refunded_at: int | None
    copy_paste: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WebhookNotification:
    event: str
    transaction_id: str


@dataclass(frozen=True)
class VerifiedPayment:
    """Internal normalized result, never a client request or invented Omega payload.

    Only trusted orchestration after an authenticated provider query and order/value
    checks may produce this object. Webhook-triggered queries additionally require
    validation of the charge's stored notification-token hash.
    """

    event_id: str
    transaction_id: str
    order_id: str
    amount_minor: int
    currency: str
    status: str


class PaymentGateway(Protocol):
    def create_pix_charge(self, charge: ChargeRequest) -> PixCharge: ...

    def query_payment(
        self, transaction_id: str | None = None, *, client_identifier: str | None = None
    ) -> TransactionSnapshot: ...

    def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str], *, expected_token_hash: str | None = None
    ) -> WebhookNotification: ...

    # Refund/cancellation signatures are intentionally unspecified until documented.
