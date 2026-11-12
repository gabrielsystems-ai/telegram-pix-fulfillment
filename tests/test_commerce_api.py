import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.test_api import KEY
from tests.test_catalog import setup_catalog
from vip.api import create_app
from vip.config import Settings
from vip.models import BuyerAssurance, Order


@pytest.fixture
async def commerce(db):
    setup_catalog(db)
    transport = ASGITransport(
        app=create_app(settings=Settings(_env_file=None, sales_api_key=KEY), database=db)
    )
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Sales-API-Key": KEY}
    ) as client:
        yield client


async def test_catalog_orders_are_protected_and_price_is_not_client_controlled(commerce, db):
    response = await commerce.get("/products")
    assert response.status_code == 200
    assert [(p["code"], p["amount_minor"]) for p in response.json()] == [
        ("sample_video", 1000),
        ("vip_permanent", 2000),
    ]
    payload = {
        "product_code": "sample_video",
        "buyer_telegram_id": 123,
        "sales_request_id": "request-00000001",
    }
    assert (
        await commerce.post("/product-orders", json=payload | {"amount_minor": 1})
    ).status_code == 422
    first = await commerce.post("/product-orders", json=payload)
    assert first.status_code == 201 and first.json()["amount_minor"] == 1000
    assert first.json()["product_id"] == "sample_video"
    assert (await commerce.post("/product-orders", json=payload)).json()["id"] == first.json()["id"]
    assert (
        await commerce.post(
            "/product-orders",
            json=payload
            | {"product_code": "recovery_bundle", "sales_request_id": "request-00000002"},
        )
    ).status_code == 409
    assert (
        await commerce.get("/products", headers={"X-Sales-API-Key": "invalid"})
    ).status_code == 401
    with db.transaction() as session:
        assert len(list(session.scalars(select(Order)))) == 1
    assert (
        await commerce.post(
            f"/orders/{first.json()['id']}/pix",
            json={"client": {"name": "Synthetic", "email": "test@example.test"}},
        )
    ).status_code == 503


async def test_access_checks_do_not_deliver_unconfigured_video_or_private_urls(commerce):
    response = await commerce.get("/buyers/123/access")
    assert response.status_code == 200 and response.json()["permissions"] == []
    assert (await commerce.get("/buyers/123/video-previous")).status_code == 403
    assert (await commerce.get("/recovery/123")).json()["eligible"] is False


async def test_obsolete_age_endpoint_does_not_collect_data(commerce, db):
    response = await commerce.post(
        "/buyers/123/age-assurance",
        json={"reference": "synthetic-external-check", "verified": True},
    )
    assert response.status_code == 404
    with db.transaction() as session:
        assert session.get(BuyerAssurance, 123) is None


async def test_qualified_combo_checkout_does_not_require_age_verification(commerce, db):
    from time import time

    from vip.recovery import RecoveryService, record_start

    now = int(time())
    with db.transaction() as session:
        record_start(session, 123, now=now - 1200)
    RecoveryService(
        Settings(_env_file=None, recovery_enabled=True), db, clock=lambda: now
    ).sync_once()
    assert (await commerce.get("/recovery/123")).json()["eligible"] is True
    response = await commerce.post(
        "/product-orders",
        json={
            "product_code": "recovery_bundle",
            "buyer_telegram_id": 123,
            "sales_request_id": "qualified-combo-request",
        },
    )
    assert response.status_code == 201 and response.json()["amount_minor"] == 2500
    assert response.json()["status"] == "pending"
    assert (await commerce.get("/buyers/123/access")).json()["permissions"] == []


async def test_paid_video_buyer_reaches_delivery_boundary_without_age_verification(commerce, db):
    from tests.test_catalog import product_order
    from tests.test_product_access import bind, settle

    bind(db, settle(db, product_order(db)))
    assert (await commerce.get("/buyers/123/access")).json()["permissions"] == [
        "sample_video_access"
    ]
    response = await commerce.get("/buyers/123/video-previous")
    assert response.status_code == 503
    assert response.json()["detail"] == "video_delivery_not_configured"
