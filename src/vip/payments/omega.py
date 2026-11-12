import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from vip.config import Settings
from vip.errors import GatewayError, IntegrationUnavailable
from vip.payments.contracts import (
    ChargeRequest,
    PixCharge,
    TransactionSnapshot,
    WebhookNotification,
)
from vip.payments.validation import FINANCIAL_STATES, decode_object, money, text, timestamp

LOG = logging.getLogger(__name__)
# Exact error literals recovered from the official documentation asset. Unknown
# values may contain payer data or credentials and must never be logged verbatim.
DOCUMENTED_ERROR_CODES = frozenset(
    {
        "ABORTED",
        "GATEWAY_ABORTED",
        "GATEWAY_INVALID_ARGUMENT",
        "GATEWAY_INVALID_CREDENTIALS",
        "GATEWAY_INVALID_DATA",
        "GATEWAY_NO_CREDENTIALS",
        "GATEWAY_TRANSACTION_NOT_FOUND",
        "GENERIC_NOT_FOUND",
        "UNAUTHENTICATED",
    }
)
DIAGNOSTIC_FIELDS = frozenset(
    {
        "identifier",
        "amount",
        "client",
        "client.name",
        "client.email",
        "client.phone",
        "client.document",
        "metadata",
        "metadata.provider",
        "metadata.orderId",
        "callbackUrl",
    }
)
CREATE_STATUSES = frozenset({"OK", "FAILED", "PENDING", "REJECTED", "CANCELED"})


def _log_failure(operation, stage, code, *, response=None, exception=None):
    """Only allowlisted diagnostic labels cross the provider/log boundary."""
    provider_code, provider_status, fields = "none", "none", []
    if response is not None:
        try:
            body = decode_object(response.content)
        except GatewayError:
            body = {}
        status = body.get("status")
        if isinstance(status, str) and status in CREATE_STATUSES:
            provider_status = status
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        value = error.get("errorCode")
        if value is not None:
            provider_code = (
                value
                if isinstance(value, str) and value in DOCUMENTED_ERROR_CODES
                else "unrecognized"
            )
        details = error.get("details")
        if isinstance(details, list):
            fields = sorted(
                {
                    item["path"]
                    for item in details
                    if isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and item["path"] in DIAGNOSTIC_FIELDS
                }
            )
    LOG.warning(
        "omega_request_failed operation=%s stage=%s http_status=%s code=%s "
        "provider_code=%s provider_status=%s invalid_fields=%s exception=%s",
        operation,
        stage,
        response.status_code if response is not None else "none",
        code,
        provider_code,
        provider_status,
        ",".join(fields) or "none",
        type(exception).__name__ if exception is not None else "none",
    )


class OmegaGateway:
    """Official Pix client; financial operations require explicit environment opt-in."""

    def __init__(
        self, settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None
    ):
        self._settings = settings or Settings()
        self._transport = transport

    def _request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Internal HTTP building block, not a public API for marking payments paid.

        Authentication headers come from the official authentication section supplied
        by the user. No transaction endpoint or response shape is inferred here.
        Redirects and automatic retries are disabled; no request/response body is logged.
        """
        parsed = urlsplit(path)
        if (
            not path
            or parsed.scheme
            or parsed.netloc
            or path.startswith(("/", "\\"))
            or "\\" in path
            or ".." in path.split("/")
            or parsed.query
            or parsed.fragment
        ):
            raise GatewayError("omega_invalid_request_path")
        public = self._settings.omega_client_id
        private = self._settings.omega_client_secret
        if public is None or private is None:
            raise IntegrationUnavailable
        operation = {
            ("POST", "gateway/pix/receive"): "create_pix",
            ("GET", "gateway/transactions"): "query_transaction",
        }.get((method, path), "transport")
        try:
            with httpx.Client(
                base_url=str(self._settings.omega_base_url).rstrip("/") + "/",
                headers={
                    "x-public-key": public.get_secret_value(),
                    "x-secret-key": private.get_secret_value(),
                },
                timeout=httpx.Timeout(10),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = client.request(
                    method,
                    path,
                    content=content,
                    params=params,
                    headers={"Content-Type": "application/json"} if content is not None else None,
                )
        except httpx.TimeoutException as error:
            _log_failure(operation, "transport", "omega_timeout", exception=error)
            raise GatewayError("omega_timeout") from None
        except httpx.TransportError as error:
            _log_failure(operation, "transport", "omega_api_unavailable", exception=error)
            raise GatewayError("omega_api_unavailable") from None
        status = response.status_code
        code = None
        if status in (401, 403):
            code = "omega_authentication_failed"
        elif status == 404:
            code = "omega_resource_not_found"
        elif status == 429:
            code = "omega_rate_limited"
        elif status >= 500:
            code = "omega_api_unavailable"
        elif 400 <= status < 500:
            code = "omega_request_rejected"
        elif not response.is_success:
            code = "omega_unexpected_response"
        if code:
            _log_failure(operation, "http", code, response=response)
            raise GatewayError(code)
        return response

    def create_pix_charge(self, charge: ChargeRequest) -> PixCharge:
        self.require_enabled(creation=True)
        if (
            charge.client is None
            or type(charge.amount_minor) is not int
            or charge.amount_minor <= 0
            or charge.currency != "BRL"
        ):
            raise GatewayError("omega_invalid_charge")
        identifier = text(charge.order_id)
        # Encode Decimal as a JSON number without any floating point calculation.
        # All other values go through json.dumps; client data cannot alter the JSON structure.
        amount = Decimal(charge.amount_minor) / Decimal(100)
        remainder = json.dumps(
            {
                "identifier": identifier,
                "client": charge.client.model_dump(exclude_none=True),
                "metadata": {"provider": "telegram-vip", "orderId": identifier},
                "callbackUrl": str(self._settings.omega_callback_url),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        content = ('{"amount":' + format(amount, ".2f") + "," + remainder[1:]).encode()
        response = self._request("POST", "gateway/pix/receive", content=content)
        try:
            return self._pix_result(response.content)
        except GatewayError as error:
            _log_failure("create_pix", "response", error.code, response=response)
            raise

    @staticmethod
    def _pix_result(content: bytes) -> PixCharge:
        data = decode_object(content)
        if data.get("status") != "OK":
            raise GatewayError("omega_charge_rejected")
        tx_id = text(data.get("transactionId"))
        pix = data.get("pix")
        if not isinstance(pix, dict):
            raise GatewayError("omega_unexpected_response")
        code = text(pix.get("code"), maximum=20000)
        token = data.get("webhookToken")
        if token is not None:
            token = text(token, maximum=1000)
        state = data.get("transactionStatus")
        if state is not None and (not isinstance(state, str) or state not in FINANCIAL_STATES):
            raise GatewayError("omega_unknown_financial_status")
        money(data.get("fee"))
        if not isinstance(data.get("order"), dict):
            raise GatewayError("omega_unexpected_response")
        text(data["order"].get("id"))
        # Remote images are neither fetched nor forwarded; checkout renders QR from code.
        return PixCharge(tx_id, code, None, timestamp(pix.get("expiresAt")), token, state)

    def require_enabled(self, *, creation: bool = False):
        if (
            not self._settings.omega_pix_enabled
            or self._settings.omega_client_id is None
            or self._settings.omega_client_secret is None
        ):
            raise IntegrationUnavailable
        if creation and self._settings.omega_callback_url is None:
            raise IntegrationUnavailable

    def query_payment(
        self, transaction_id: str | None = None, *, client_identifier: str | None = None
    ) -> TransactionSnapshot:
        self.require_enabled()
        # Each documented filter is optional. Use exactly one; combined semantics are unverified.
        if bool(transaction_id) == bool(client_identifier):
            raise GatewayError("omega_invalid_query")
        params = (
            {"id": text(transaction_id)}
            if transaction_id
            else {
                "clientIdentifier": text(client_identifier),
            }
        )
        data = decode_object(self._request("GET", "gateway/transactions", params=params).content)
        state = data.get("status")
        if not isinstance(state, str) or state not in FINANCIAL_STATES:
            raise GatewayError("omega_unknown_financial_status")
        pix = data.get("pixInformation")
        code = None
        if pix is not None:
            if not isinstance(pix, dict):
                raise GatewayError("omega_unexpected_response")
            code = text(pix.get("qrCode"), maximum=20000)
        return TransactionSnapshot(
            transaction_id=text(data.get("id")),
            client_identifier=text(data.get("clientIdentifier")),
            amount=money(data.get("amount")),
            charge_amount=money(data.get("chargeAmount")),
            currency=text(data.get("currency")),
            status=state,
            payment_method=text(data.get("paymentMethod")),
            purchase_type=text(data.get("purchaseType")),
            exchange_rate=money(data.get("exchangeRate")),
            producer_extra_amount=money(data.get("producerExtraAmount")),
            paid_at=timestamp(data.get("payedAt")),
            refunded_at=timestamp(data.get("refundedAt")),
            copy_paste=code,
        )

    def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str], *, expected_token_hash: str | None = None
    ) -> WebhookNotification:
        self.require_enabled()
        if expected_token_hash is None:
            raise IntegrationUnavailable
        data = decode_object(raw_body)
        transaction = data.get("transaction")
        if not isinstance(transaction, dict):
            raise GatewayError("omega_invalid_webhook")
        token = text(data.get("token"), maximum=1000)
        digest = hashlib.sha256(token.encode()).hexdigest()
        # Body token compared to the hash of the token returned for this charge.
        # No invented signing algorithm/header; a separate authenticated query is mandatory.
        if not hmac.compare_digest(digest, expected_token_hash):
            raise GatewayError("omega_invalid_webhook_token")
        return WebhookNotification(text(data.get("event")), text(transaction.get("id")))
