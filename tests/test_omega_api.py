from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from tests.test_omega_operations import Harness
from tests.test_omega_pix import configured
from vip.api import create_app

KEY = "synthetic-server-key-at-least-32-characters"


@pytest.fixture
async def api(db):
    h = Harness(db)
    settings = configured()
    settings.sales_api_key = SecretStr(KEY)
    app = create_app(settings=settings, database=db, omega_gateway=h.service.gateway)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Sales-API-Key": KEY},
    ) as client:
        response = await client.post("/orders", json={"plan_id": h.plan_id})
        yield h, client, response.json()["id"]


async def test_external_api_creates_pix_and_confirms_via_authenticated_webhook(api):
    h, client, order_id = api
    body = {"client": {"name": "Synthetic Buyer", "email": "buyer@example.test"}}
    result = await client.post(f"/orders/{order_id}/pix", json=body)
    assert result.status_code == 200
    assert result.json()["eligible_for_linking"] is False
    assert "synthetic-webhook-token" not in result.text
    assert h.post_count == 1
    result = await client.post(f"/orders/{order_id}/pix", json=body)
    assert result.status_code == 200
    assert h.post_count == 1
    h.changes = {"status": "COMPLETED", "payedAt": datetime.now(UTC).isoformat()}
    event = {
        "event": "TRANSACTION_PAID",
        "token": "synthetic-webhook-token",
        "transaction": {"id": "tx-" + order_id},
        "client": {},
    }
    result = await client.post("/webhooks/omega", json=event)
    assert result.status_code == 200
    assert (await client.get(f"/orders/{order_id}")).json()["status"] == "paid"
    assert (await client.post(f"/orders/{order_id}/link-token")).status_code == 200
    assert (await client.post("/webhooks/omega", json=event)).json()["duplicate"] is True


@pytest.mark.parametrize(
    "extra",
    [{"amount": 0.01}, {"callbackUrl": "https://attacker.example"}, {"transactionId": "arbitrary"}],
)
async def test_buyer_cannot_override_financial_fields_or_callback(api, extra):
    h, client, order_id = api
    response = await client.post(
        f"/orders/{order_id}/pix",
        json={
            "client": {"name": "Synthetic Buyer", "email": "buyer@example.test"},
            **extra,
        },
    )
    assert response.status_code == 422
    assert h.post_count == 0


async def test_missing_buyer_fields_are_rejected_without_echoing_pii(api):
    h, client, order_id = api
    response = await client.post(
        f"/orders/{order_id}/pix",
        json={
            "client": {"email": "sensitive-email@example.test", "document": "12345678900"},
        },
    )
    assert response.status_code == 422
    assert "sensitive-email" not in response.text
    assert "12345678900" not in response.text
    assert h.post_count == 0


async def test_invalid_webhook_is_rejected_and_large_request_is_bounded(api):
    _, client, _ = api
    assert (await client.post("/webhooks/omega", content=b"not json")).status_code == 400
    assert (await client.post("/webhooks/omega", content=b"x" * 256001)).status_code == 413


async def test_reconciliation_is_explicit_and_rate_limited(api):
    h, client, order_id = api
    await client.post(
        f"/orders/{order_id}/pix",
        json={
            "client": {"name": "Synthetic Buyer", "email": "buyer@example.test"},
        },
    )
    assert (await client.post(f"/orders/{order_id}/reconcile")).status_code == 200
    assert (await client.post(f"/orders/{order_id}/reconcile")).status_code == 429
    assert h.get_count == 1


@pytest.mark.parametrize(
    "failure,expected_http",
    [
        ("timeout", 504),
        ("server_error", 502),
        ("rate_limited", 502),
        ("authentication", 502),
    ],
)
async def test_webhook_failure_is_not_acknowledged_or_persisted_and_replay_recovers(
    api, failure, expected_http
):
    from sqlalchemy import func, select

    from vip.models import OmegaEvent, Order, Payment

    h, client, oid = api
    await client.post(
        f"/orders/{oid}/pix",
        json={"client": {"name": "Synthetic Buyer", "email": "buyer@example.test"}},
    )
    h.complete()
    original = h.service.gateway._transport

    def fail_query(request):
        assert request.method == "GET"
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-private-error", request=request)
        status = {"server_error": 503, "rate_limited": 429, "authentication": 401}[failure]
        return httpx.Response(status, json={"private": "must-not-escape"})

    h.service.gateway._transport = httpx.MockTransport(fail_query)
    event = {
        "event": "TRANSACTION_PAID",
        "token": "synthetic-webhook-token",
        "transaction": {"id": "tx-" + oid},
    }
    # Omega authenticates by the per-charge body token; never our store's API key.
    client.headers.pop("X-Sales-API-Key")
    response = await client.post("/webhooks/omega", json=event)
    assert response.status_code == expected_http
    assert "private" not in response.text and "must-not-escape" not in response.text
    with h.db.transaction() as s:
        assert s.get(Order, oid).status == "pending"
        assert s.scalar(select(func.count()).select_from(Payment)) == 0
        assert s.scalar(select(func.count()).select_from(OmegaEvent)) == 0
    h.service.gateway._transport = original
    assert (await client.post("/webhooks/omega", json=event)).status_code == 200
    duplicate = await client.post("/webhooks/omega", json=event)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    with h.db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 1
    assert h.post_count == 1
