"""Official producer API shapes, with fictitious data and no external network."""

import json
from decimal import Decimal

import httpx
import pytest

from vip.config import Settings
from vip.errors import GatewayError
from vip.payments.contracts import Buyer, ChargeRequest
from vip.payments.omega import OmegaGateway

CALLBACK = "https://sales.example/webhooks/omega"


def configured():
    return Settings(
        _env_file=None,
        omega_pix_enabled=True,
        omega_callback_url=CALLBACK,
        omega_client_id="synthetic-public",
        omega_client_secret="synthetic-private",
    )


def charge_response(**updates):
    # Official /endpoint/pix/receive 200 response shape; only fixture values differ.
    body = {
        "transactionId": "omega-tx-1",
        "status": "OK",
        "fee": 5,
        "webhookToken": "synthetic-webhook-token",
        "order": {
            "id": "provider-order-1",
            "url": "https://checkout.example/order/1",
            "receiptUrl": "https://checkout.example/order/1/receipt",
        },
        "pix": {
            "code": "000201synthetic-pix-code",
            "image": "https://qr.example/1",
            "base64": "",
            "expiresAt": "2030-01-01T12:00:00Z",
        },
    }
    return body | updates


def transaction_response(**updates):
    # Official query schema: bare object, amount/chargeAmount in currency units,
    # and payedAt (not paidAt). Fields not needed for decisions remain in the fixture.
    body = {
        "id": "omega-tx-1",
        "clientIdentifier": "order-1",
        "currency": "BRL",
        "amount": 29.9,
        "chargeAmount": 29.9,
        "exchangeRate": 1,
        "producerExtraAmount": 0,
        "status": "PENDING",
        "statusDescription": None,
        "purchaseType": "ONCE",
        "paymentMethod": "PIX",
        "details": None,
        "errorDescription": None,
        "webhookUrl": CALLBACK,
        "availableAt": "2030-01-01T00:00:00Z",
        "createdAt": "2030-01-01T00:00:00Z",
        "refundedAt": None,
        "payedAt": None,
        "boletoInformation": None,
        "pixInformation": {"qrCode": "000201synthetic-pix-code", "image": None, "base64": ""},
        "pixMetadata": {
            "payerBankName": "Fictitious bank",
            "payerDocument": "00000000000",
            "payerName": "Synthetic Buyer",
            "receiverBankName": "Fictitious bank",
            "receiverDocument": "00000000000",
            "receiverName": "Synthetic Seller",
            "receiverPixKey": "synthetic@example.test",
        },
    }
    return body | updates


def buyer():
    return Buyer(name="Synthetic Buyer", email="buyer@example.test")


def gateway(handler):
    return OmegaGateway(configured(), transport=httpx.MockTransport(handler))


def test_create_sends_exact_decimal_price_official_path_and_server_callback():
    def handle(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/gateway/pix/receive"
        assert request.headers["x-public-key"] == "synthetic-public"
        assert request.headers["x-secret-key"] == "synthetic-private"
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content, parse_float=Decimal)
        assert body["amount"] == Decimal("29.90")
        assert body["identifier"] == "order-1"
        assert body["client"] == {"name": "Synthetic Buyer", "email": "buyer@example.test"}
        assert body["callbackUrl"] == CALLBACK
        assert body["metadata"] == {"provider": "telegram-vip", "orderId": "order-1"}
        assert not {"shippingFee", "extraFee", "discount", "splits"} & body.keys()
        return httpx.Response(200, json=charge_response())

    result = gateway(handle).create_pix_charge(
        ChargeRequest("order-1", 2990, "BRL", "order-1", buyer())
    )
    assert result.transaction_id == "omega-tx-1"
    assert result.copy_paste == "000201synthetic-pix-code"
    assert result.financial_status is None  # operation OK does not mean paid or even PENDING
    assert "synthetic-webhook-token" not in repr(result)
    assert result.qr_code is None  # no remote image is trusted or fetched


def test_multiple_charges_reuse_one_callback_without_registering_panel_webhooks():
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        return httpx.Response(200, json=charge_response(transactionId="tx-" + body["identifier"]))

    client = gateway(handle)
    for number, price in enumerate([1000, 2000, 2500]):
        client.create_pix_charge(ChargeRequest(f"order-{number}", price, "BRL", "unused", buyer()))
    assert len(requests) == 3
    assert {(method, path) for method, path, _ in requests} == {
        ("POST", "/api/v1/gateway/pix/receive")
    }
    assert {body["callbackUrl"] for _, _, body in requests} == {CALLBACK}
    assert all("?" not in body["callbackUrl"] for _, _, body in requests)


@pytest.mark.parametrize("pix", [None, {}, {"image": "https://qr.example/1"}, {"code": ""}])
def test_missing_copy_paste_code_is_rejected_even_if_an_image_exists(pix):
    g = gateway(lambda request: httpx.Response(200, json=charge_response(pix=pix)))
    with pytest.raises(GatewayError):
        g.create_pix_charge(ChargeRequest("order-1", 2990, "BRL", "order-1", buyer()))


def test_optional_expiry_image_and_base64_are_not_required():
    g = gateway(
        lambda request: httpx.Response(
            200,
            json=charge_response(
                pix={"code": "synthetic", "image": "javascript:unsafe", "base64": ""},
            ),
        )
    )
    result = g.create_pix_charge(ChargeRequest("order-1", 2990, "BRL", "order-1", buyer()))
    assert result.copy_paste == "synthetic"
    assert result.expires_at is None
    assert result.qr_code is None


@pytest.mark.parametrize("status", ["FAILED", "REJECTED", "CANCELED", "SOMETHING_NEW"])
def test_unsuccessful_or_unknown_create_status_does_not_become_a_payment(status):
    g = gateway(lambda request: httpx.Response(200, json=charge_response(status=status)))
    with pytest.raises(GatewayError):
        g.create_pix_charge(ChargeRequest("order-1", 2990, "BRL", "order-1", buyer()))


def test_query_uses_one_documented_filter_and_normalizes_decimal_without_approval():
    def handle(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/gateway/transactions"
        assert dict(request.url.params) == {"id": "omega-tx-1"}
        return httpx.Response(200, json=transaction_response())

    result = gateway(handle).query_payment("omega-tx-1")
    assert result.client_identifier == "order-1"
    assert result.amount == Decimal("29.90")
    assert result.charge_amount == Decimal("29.90")
    assert result.status == "PENDING"


def test_query_can_reconcile_by_client_identifier_without_combining_filters():
    def handle(request):
        assert dict(request.url.params) == {"clientIdentifier": "order-1"}
        return httpx.Response(200, json=transaction_response())

    assert gateway(handle).query_payment(client_identifier="order-1").transaction_id == "omega-tx-1"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "OK"},
        {"status": "UNKNOWN"},
        {"status": {}},
        {"status": []},
        {"amount": "29.90"},
        {"amount": True},
        {"chargeAmount": 29.901},
        {"amount": -1},
        {"currency": None},
        {"id": ""},
        {"payedAt": "not-a-date"},
    ],
)
def test_unexpected_financial_response_is_rejected(changes):
    g = gateway(lambda request: httpx.Response(200, json=transaction_response(**changes)))
    with pytest.raises(GatewayError):
        g.query_payment("omega-tx-1")


def test_create_timeout_is_not_retried():
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic", request=request)

    with pytest.raises(GatewayError, match="omega_timeout"):
        gateway(handle).create_pix_charge(ChargeRequest("order-1", 2990, "BRL", "order-1", buyer()))
    assert calls == 1


@pytest.mark.parametrize("status", [{}, []])
def test_nonstring_create_financial_status_has_a_safe_error(status):
    g = gateway(lambda request: httpx.Response(200, json=charge_response(transactionStatus=status)))
    with pytest.raises(GatewayError):
        g.create_pix_charge(ChargeRequest("order-1", 2990, "BRL", "order-1", buyer()))
