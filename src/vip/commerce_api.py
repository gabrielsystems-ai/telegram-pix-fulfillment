from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from vip.catalog import create_product_order, list_products
from vip.entitlements import product_accesses
from vip.models import Product, RecoveryTask
from vip.recovery_rules import recovery_reason


class ProductOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_code: str = Field(pattern=r"^(sample_video|vip_permanent|recovery_bundle)$")
    buyer_telegram_id: int = Field(gt=0, le=2**63 - 1)
    sales_request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


def build_commerce_router(settings, db, protected, order_view):
    router = APIRouter(dependencies=protected)

    @router.get("/products")
    def products():
        with db.transaction() as session:
            return [
                {
                    "code": p.code,
                    "name": p.name,
                    "amount_minor": p.amount_minor,
                    "currency": "BRL",
                    "purchase_type": "one_time",
                }
                for p in list_products(session)
            ]

    @router.post("/product-orders", status_code=201)
    def product_order(data: ProductOrderInput):
        # This endpoint is server-to-server. The sales server MUST authenticate
        # ownership of this identity; no browser-supplied Telegram ID is trusted.
        with db.transaction() as session:
            order = create_product_order(
                session,
                data.product_code,
                data.buyer_telegram_id,
                data.sales_request_id,
                ttl_seconds=settings.order_ttl_seconds,
                payment_provider=settings.payment_provider,
            )
            return order_view(order)

    @router.get("/buyers/{telegram_id}/access")
    def access(telegram_id: int):
        with db.transaction() as session:
            return {
                "permissions": sorted(
                    {a.permission for a in product_accesses(session, telegram_id)}
                ),
                "video_delivery": "not_configured",
            }

    @router.get("/buyers/{telegram_id}/video-previous")
    def video(telegram_id: int):
        with db.transaction() as session:
            if not any(
                a.permission == "sample_video_access"
                for a in product_accesses(session, telegram_id)
            ):
                raise HTTPException(status_code=403, detail="video_access_denied")
        raise HTTPException(status_code=503, detail="video_delivery_not_configured")

    @router.get("/recovery/{telegram_id}")
    def recovery(telegram_id: int):
        from time import time

        with db.transaction() as session:
            task = session.get(RecoveryTask, telegram_id)
            offer = session.get(Product, "recovery_bundle")
            eligible = bool(
                not recovery_reason(session, task, int(time()))
                and offer
                and offer.active
                and offer.rights_reference
            )
            return {
                "eligible": eligible,
                "state": task.state if task else "not_started",
                "product_code": "recovery_bundle" if eligible else None,
            }

    return router
