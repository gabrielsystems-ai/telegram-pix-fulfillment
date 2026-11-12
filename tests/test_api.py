import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from vip.api import create_app
from vip.config import Settings
from vip.errors import IntegrationUnavailable
from vip.models import Order, Payment, Subscription
from vip.payments.contracts import ChargeRequest
from vip.payments.omega import OmegaGateway

KEY = "test-only-server-api-key-32-characters-long"


@pytest.fixture
async def client(db):
    settings = Settings(_env_file=None, sales_api_key=KEY)
    transport = ASGITransport(app=create_app(settings=settings, database=db))
    async with AsyncClient(
        transport=transport, base_url="http://testserver", headers={"X-Sales-API-Key": KEY}
    ) as test_client:
        yield test_client


async def plan(client):
    response = await client.post(
        "/plans",
        json={
            "name": "Test VIP",
            "amount_minor": 2500,
            "duration_days": 30,
            "telegram_group_id": -100123,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


async def test_auth_is_required_and_unconfigured_server_fails_closed(db):
    transport = ASGITransport(app=create_app(settings=Settings(_env_file=None), database=db))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.post("/orders", json={"plan_id": "a" * 32})).status_code == 503
    transport = ASGITransport(
        app=create_app(
            settings=Settings(_env_file=None, sales_api_key=KEY),
            database=db,
        )
    )
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/plans")).status_code == 401
        response = await client.get("/plans", headers={"X-Sales-API-Key": "wrong"})
        assert response.status_code == 401


async def test_external_order_is_pending_and_client_cannot_set_price_or_status(client, db):
    plan_id = await plan(client)
    response = await client.post("/orders", json={"plan_id": plan_id})
    assert response.status_code == 201
    body = response.json()
    assert body["amount_minor"] == 2500
    assert body["status"] == "pending"
    assert response.headers["cache-control"] == "no-store"
    response = await client.get(f"/orders/{body['id']}")
    assert response.json()["status"] == "pending"
    response = await client.post(
        "/orders", json={"plan_id": plan_id, "status": "paid", "amount_minor": 1}
    )
    assert response.status_code == 422
    assert (await client.post(f"/orders/{body['id']}/link-token")).status_code == 409
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1


async def test_gateway_and_webhook_cannot_settle_or_create_real_charge(client, db):
    response = await client.post("/orders", json={"plan_id": await plan(client)})
    order_id = response.json()["id"]
    assert (await client.post(f"/orders/{order_id}/pix")).status_code == 503
    response = await client.post(
        "/webhooks/omega", json={"order_id": order_id, "status": "paid", "amount_minor": 2500}
    )
    assert response.status_code == 503
    response = await client.post(f"/orders/{order_id}/confirm", json={"status": "paid"})
    assert response.status_code == 404
    with db.transaction() as session:
        assert session.get(Order, order_id).status == "pending"
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        assert session.scalar(select(func.count()).select_from(Subscription)) == 0
    gateway = OmegaGateway()
    operations = [
        lambda: gateway.create_pix_charge(ChargeRequest(order_id, 2500, "BRL", order_id)),
        lambda: gateway.query_payment("tx"),
        lambda: gateway.verify_webhook(b'{"status":"paid"}', {}),
    ]
    for operation in operations:
        with pytest.raises(IntegrationUnavailable):
            operation()


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_minor": 0},
        {"amount_minor": -1},
        {"duration_days": 0},
        {"telegram_group_id": 1},
        {"name": " "},
        {"amount_minor": True},
    ],
)
async def test_invalid_plan_is_rejected(client, changes):
    data = {"name": "Test", "amount_minor": 2500, "duration_days": 30, "telegram_group_id": -100123}
    assert (await client.post("/plans", json=data | changes)).status_code == 422


async def test_configuring_omega_credentials_does_not_enable_unverified_operations(db):
    settings = Settings(
        _env_file=None,
        sales_api_key=KEY,
        omega_client_id="synthetic-client-id",
        omega_client_secret="synthetic-client-secret",
    )
    transport = ASGITransport(app=create_app(settings=settings, database=db))
    async with AsyncClient(
        transport=transport, base_url="http://testserver", headers={"X-Sales-API-Key": KEY}
    ) as client:
        response = await client.post("/orders", json={"plan_id": await plan(client)})
        order_id = response.json()["id"]
        assert (await client.post(f"/orders/{order_id}/pix")).status_code == 503
        response = await client.post("/webhooks/omega", json={"status": "paid"})
        assert response.status_code == 503
    with db.transaction() as session:
        assert session.get(Order, order_id).status == "pending"
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
