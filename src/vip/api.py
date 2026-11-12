import secrets
from contextlib import asynccontextmanager
from time import time
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from vip.commerce_api import build_commerce_router
from vip.config import Settings
from vip.db import Database
from vip.errors import DomainError, GatewayError, IntegrationUnavailable
from vip.linking import issue_link_token
from vip.models import Order
from vip.orders import create_order
from vip.payments.contracts import Buyer
from vip.payments.omega import OmegaGateway
from vip.payments.operations import OmegaPayments
from vip.plans import create_plan, list_plans
from vip.webhooks import build_webhook_router


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    amount_minor: int = Field(gt=0, le=2_000_000_000)
    duration_days: int = Field(gt=0, le=3650)
    telegram_group_id: int = Field(lt=0, ge=-(2**63))


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class PixInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client: Buyer


def order_view(order: Order) -> dict:
    effective_status = order.status
    if effective_status == "pending" and order.expires_at <= int(time()):
        effective_status = "expired"
    result = {
        "id": order.id,
        "plan_id": order.plan_id,
        "amount_minor": order.amount_minor,
        "currency": order.currency,
        "status": effective_status,
        "expires_at": order.expires_at,
        "paid_at": order.paid_at,
        "linked": order.linked_telegram_id is not None,
    }
    if order.product_id is not None:
        result.update(
            product_id=order.product_id, product_name=order.product_name, origin=order.origin
        )
    return result


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    omega_gateway: OmegaGateway | None = None,
    pushin_gateway=None,
) -> FastAPI:
    settings = settings or Settings()
    db = database or Database(settings.database_url)
    payments = OmegaPayments(db, omega_gateway or OmegaGateway(settings))
    from vip.payments.pushinpay import PushinGateway, PushinPayments
    from vip.pushin_webhooks import build_pushin_webhook_router

    pushin = PushinPayments(db, pushin_gateway or PushinGateway(settings))

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if database is None:
            db.engine.dispose()

    app = FastAPI(
        title="VIP Sales Backend",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def authorize(x_sales_api_key: Annotated[str | None, Header()] = None):
        # This is our own server-to-server auth, unrelated to Omega authentication.
        expected = settings.sales_api_key
        if expected is None:
            raise HTTPException(status_code=503, detail="sales_api_not_configured")
        if not x_sales_api_key or not secrets.compare_digest(
            x_sales_api_key.encode(),
            expected.get_secret_value().encode(),
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    protected = [Depends(authorize)]

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_request, error: DomainError):
        status = {
            "order_not_found": 404,
            "charge_not_found": 404,
            "invalid_webhook": 400,
            "invalid_webhook_token": 401,
            "unknown_webhook_transaction": 404,
            "reconciliation_rate_limited": 429,
        }.get(error.code, 409)
        return JSONResponse(status_code=status, content={"detail": error.code})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, error):
        # FastAPI's default includes input values; Pix buyer data must not be echoed.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [{"loc": item["loc"], "type": item["type"]} for item in error.errors()]
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def persistence_error(_request, _error):
        return JSONResponse(status_code=503, content={"detail": "database_operation_failed"})

    @app.exception_handler(GatewayError)
    async def gateway_error(_request, error):
        status = 504 if error.code in {"omega_timeout", "pushinpay_timeout"} else 502
        return JSONResponse(status_code=status, content={"detail": error.code})

    @app.exception_handler(IntegrationUnavailable)
    async def unavailable(_request, _error):
        provider = "pushinpay" if _request.url.path == "/webhooks/pushinpay" else "omega"
        return JSONResponse(
            status_code=503, content={"detail": provider + "_integration_unavailable"}
        )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "omega": "configured" if settings.omega_pix_enabled else "unavailable",
            "group_automation": False,
        }

    @app.get("/ready")
    def ready():
        from vip.deployment import schema_ready

        try:
            if schema_ready(db.engine):
                return {"status": "ready"}
        except Exception:
            pass
        return JSONResponse(status_code=503, content={"status": "unavailable"})

    @app.get("/plans", dependencies=protected)
    def get_plans():
        with db.transaction() as session:
            return [
                {
                    "id": p.id,
                    "name": p.name,
                    "amount_minor": p.amount_minor,
                    "currency": p.currency,
                    "duration_days": p.duration_days,
                }
                for p in list_plans(session)
            ]

    @app.post("/plans", dependencies=protected, status_code=201)
    def add_plan(data: PlanInput):
        with db.transaction() as session:
            plan = create_plan(session, **data.model_dump())
            return {"id": plan.id}

    @app.post("/orders", dependencies=protected, status_code=201)
    def add_order(data: OrderInput):
        with db.transaction() as session:
            order = create_order(session, data.plan_id, ttl_seconds=settings.order_ttl_seconds)
            return order_view(order)

    @app.get("/orders/{order_id}", dependencies=protected)
    def get_order(order_id: str):
        with db.transaction() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise DomainError("order_not_found")
            return order_view(order)

    @app.post("/orders/{order_id}/pix", dependencies=protected)
    def create_pix(order_id: str, data: PixInput | None = None):
        with db.transaction() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise DomainError("order_not_found")
            provider = order.payment_provider
        if provider == "pushinpay":
            pushin.create_charge(order_id)
            return {"order_id": order_id, "status": "pending"}
        payments.enabled()
        if data is None:
            raise HTTPException(status_code=422, detail="client_required")
        return payments.create_charge(order_id, data.client)

    @app.post("/orders/{order_id}/reconcile", dependencies=protected)
    def reconcile(order_id: str):
        with db.transaction() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise DomainError("order_not_found")
            provider = order.payment_provider
        if provider == "pushinpay":
            pushin.reconcile(order_id)
            with db.transaction() as session:
                return order_view(session.get(Order, order_id))
        return payments.reconcile(order_id)

    @app.post("/orders/{order_id}/link-token", dependencies=protected)
    def link_token(order_id: str):
        with db.transaction() as session:
            raw = issue_link_token(session, order_id, ttl_seconds=settings.link_token_ttl_seconds)
            return {"token": raw, "expires_in": settings.link_token_ttl_seconds}

    app.include_router(build_webhook_router(payments))
    app.include_router(build_pushin_webhook_router(pushin))
    app.include_router(build_commerce_router(settings, db, protected, order_view))
    return app
