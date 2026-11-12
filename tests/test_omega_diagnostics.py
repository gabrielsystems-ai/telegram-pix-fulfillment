"""Safe provider diagnostics with official response shapes and fictitious data."""

import httpx
import pytest

from tests.test_omega_pix import buyer, charge_response, configured
from vip.errors import GatewayError
from vip.payments.contracts import ChargeRequest
from vip.payments.omega import OmegaGateway


def create(gateway):
    return gateway.create_pix_charge(ChargeRequest("order-1", 1000, "BRL", "order-1", buyer()))


@pytest.mark.parametrize("nested", [False, True])
def test_validation_failure_logs_http_code_and_only_known_field_names(nested, caplog):
    error = {
        "statusCode": 400,
        "errorCode": "GATEWAY_INVALID_DATA",
        "message": "buyer@example.test synthetic-private",
        "details": [
            {"path": "client.phone", "error": {"message": "private buyer value"}},
            {"path": "buyer@example.test", "error": {"message": "private buyer value"}},
        ],
    }
    gateway = OmegaGateway(
        configured(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": error} if nested else error)
        ),
    )
    with pytest.raises(GatewayError, match="omega_request_rejected"):
        create(gateway)
    assert "operation=create_pix" in caplog.text
    assert "stage=http" in caplog.text and "http_status=400" in caplog.text
    assert "provider_code=GATEWAY_INVALID_DATA" in caplog.text
    assert "invalid_fields=client.phone" in caplog.text
    for private in ("buyer@example.test", "synthetic-private", "private buyer value"):
        assert private not in caplog.text


def test_query_not_found_is_distinct_from_creation_failure(caplog):
    gateway = OmegaGateway(
        configured(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(404, json={"errorCode": "GATEWAY_TRANSACTION_NOT_FOUND"})
        ),
    )
    with pytest.raises(GatewayError, match="omega_resource_not_found"):
        gateway.query_payment(client_identifier="private-order-reference")
    assert "operation=query_transaction" in caplog.text
    assert "http_status=404" in caplog.text
    assert "provider_code=GATEWAY_TRANSACTION_NOT_FOUND" in caplog.text
    assert "private-order-reference" not in caplog.text


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failure_logs_stage_without_exception_message_or_retry(failure, caplog):
    calls = []

    def fail(request):
        calls.append(request.method)
        raise failure("synthetic-private buyer@example.test", request=request)

    gateway = OmegaGateway(configured(), transport=httpx.MockTransport(fail))
    with pytest.raises(GatewayError):
        create(gateway)
    assert calls == ["POST"]
    assert "operation=create_pix" in caplog.text and "stage=transport" in caplog.text
    assert "http_status=none" in caplog.text
    assert "exception=" + failure.__name__ in caplog.text
    assert "synthetic-private" not in caplog.text and "buyer@example.test" not in caplog.text


@pytest.mark.parametrize("malformed", [False, True])
def test_http_200_business_rejection_or_invalid_json_is_response_failure(malformed, caplog):
    response = (
        httpx.Response(200, text="private sensitive non-json")
        if malformed
        else httpx.Response(200, json=charge_response(status="REJECTED"))
    )
    gateway = OmegaGateway(configured(), transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(GatewayError):
        create(gateway)
    assert "stage=response" in caplog.text and "http_status=200" in caplog.text
    assert "private sensitive" not in caplog.text
    if not malformed:
        assert "provider_status=REJECTED" in caplog.text


def test_untrusted_codes_fields_and_status_are_never_logged(caplog):
    gateway = OmegaGateway(
        configured(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                422,
                json={
                    "errorCode": "SECRET_TOKEN_ABC",
                    "status": "secret value",
                    "details": [{"path": "client.email=private@example.test"}],
                },
            )
        ),
    )
    with pytest.raises(GatewayError):
        create(gateway)
    assert "provider_code=unrecognized" in caplog.text
    for value in ("SECRET_TOKEN_ABC", "secret value", "private@example.test"):
        assert value not in caplog.text


def test_success_does_not_log_credentials_payer_qr_or_webhook_token(caplog):
    gateway = OmegaGateway(
        configured(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=charge_response())),
    )
    assert create(gateway).copy_paste
    assert not caplog.text
