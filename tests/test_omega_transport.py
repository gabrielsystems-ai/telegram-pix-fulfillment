"""Transport tests only; no invented transaction/webhook payload fixtures.

The documented public /ping path is used as a known URL in MockTransport.
Sending headers to this mock does not prove that /ping authenticates credentials.
"""

import httpx
import pytest

from vip.config import Settings
from vip.errors import GatewayError, IntegrationUnavailable
from vip.payments.contracts import ChargeRequest
from vip.payments.omega import OmegaGateway


def settings():
    return Settings(
        _env_file=None,
        omega_client_id="synthetic-client-id",
        omega_client_secret="synthetic-client-secret",
    )


def test_authenticated_transport_uses_documented_headers_and_preserves_base_path():
    def handle(request):
        assert str(request.url) == "https://app.omegapayments.com.br/api/v1/ping"
        assert request.headers["x-public-key"] == "synthetic-client-id"
        assert request.headers["x-secret-key"] == "synthetic-client-secret"
        assert "authorization" not in request.headers
        return httpx.Response(200)

    gateway = OmegaGateway(settings(), transport=httpx.MockTransport(handle))
    assert gateway._request("GET", "ping").status_code == 200


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "omega_authentication_failed"),
        (403, "omega_authentication_failed"),
        (404, "omega_resource_not_found"),
        (400, "omega_request_rejected"),
        (429, "omega_rate_limited"),
        (503, "omega_api_unavailable"),
        (302, "omega_unexpected_response"),
    ],
)
def test_transport_errors_do_not_disclose_request_headers_or_response_body(status, code, caplog):
    def handle(request):
        return httpx.Response(
            status,
            text="synthetic-client-secret: sensitive upstream body",
            headers={"Location": "https://unrelated.example/"},
        )

    gateway = OmegaGateway(settings(), transport=httpx.MockTransport(handle))
    with pytest.raises(GatewayError) as error:
        gateway._request("GET", "ping")
    assert error.value.code == code
    assert "synthetic-client-secret" not in str(error.value)
    assert "sensitive upstream body" not in str(error.value)
    assert "synthetic-client-secret" not in caplog.text


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_communication_failure_is_safe_and_bounded(failure):
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        raise failure("synthetic-client-secret", request=request)

    gateway = OmegaGateway(settings(), transport=httpx.MockTransport(handle))
    with pytest.raises(GatewayError) as error:
        gateway._request("GET", "ping")
    assert calls == 1  # automatic retries are deferred until operation contracts are verified
    assert error.value.code in {"omega_timeout", "omega_api_unavailable"}
    assert "synthetic-client-secret" not in str(error.value)


def test_missing_credentials_do_not_issue_any_request():
    def handle(_request):
        pytest.fail("Missing credentials must prevent all authenticated requests")

    gateway = OmegaGateway(
        Settings(_env_file=None, omega_client_id="", omega_client_secret=""),
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(IntegrationUnavailable):
        gateway._request("GET", "ping")


def test_transport_rejects_absolute_urls_instead_of_sending_credentials_elsewhere():
    def handle(_request):
        pytest.fail("Absolute URLs must be rejected before issuing a request")

    gateway = OmegaGateway(settings(), transport=httpx.MockTransport(handle))
    with pytest.raises(GatewayError, match="omega_invalid_request_path"):
        gateway._request("GET", "https://unrelated.example/")


def test_undocumented_operations_remain_disabled_with_credentials():
    def handle(_request):
        pytest.fail("Undocumented operations must never reach HTTP transport")

    gateway = OmegaGateway(settings(), transport=httpx.MockTransport(handle))
    operations = [
        lambda: gateway.create_pix_charge(
            ChargeRequest("internal-order", 2500, "BRL", "internal-key")
        ),
        lambda: gateway.query_payment("internal-transaction"),
        lambda: gateway.verify_webhook(b"untrusted-content", {}),
    ]
    for operation in operations:
        with pytest.raises(IntegrationUnavailable):
            operation()
