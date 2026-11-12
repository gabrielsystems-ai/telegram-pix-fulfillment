import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from vip.api import create_app
from vip.config import Settings
from vip.models import Fulfillment, Order, Payment, WebPurchase


@pytest.mark.parametrize("provider", ["pushinpay", "omega"])
def test_legacy_web_surface_cannot_be_enabled_by_old_configuration(db, monkeypatch, provider):
    monkeypatch.setenv("CHECKOUT_BASE_URL", "https://shop.example.test")
    monkeypatch.setenv("TELEGRAM_LOGIN_CLIENT_ID", "999")
    settings = Settings(
        _env_file=None,
        payment_provider=provider,
        sales_api_key="synthetic-sales-key-at-least-32-characters",
        omega_pix_enabled=True,
        omega_client_id="synthetic-client",
        omega_client_secret="synthetic-secret",
    )
    app = create_app(settings=settings, database=db)
    assert not any(
        path.startswith(("/checkout", "/web/purchases")) for path in app.openapi()["paths"]
    )
    with TestClient(app) as client:
        client.headers["X-Sales-Api-Key"] = settings.sales_api_key.get_secret_value()
        for path in (
            "/checkout",
            "/checkout/catalog",
            "/checkout/status",
            "/checkout/qr",
            "/checkout/assets/checkout.js",
            "/checkout/assets/checkout.css",
            "/checkout/session",
            "/checkout/authenticate",
            "/checkout/pix",
            "/checkout/refresh",
            "/checkout/new",
            "/web/purchases",
            "/web/purchases/synthetic",
            "/web/purchases/synthetic/authenticate",
        ):
            assert client.get(path).status_code == 404
            assert client.post(path, json={}).status_code == 404
        assert client.get("/health").status_code == 200
        assert client.get("/products").status_code == 200
    with db.transaction() as session:
        for model in (Order, Payment, Fulfillment, WebPurchase):
            assert session.scalar(select(func.count()).select_from(model)) == 0
