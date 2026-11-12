import json
import re
from collections.abc import Mapping
from time import time

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vip.config import Settings
from vip.errors import DomainError
from vip.models import AccessGrant, AuditEvent, Order, Product, RecoveryTask
from vip.recovery_rules import recovery_reason
from vip.users import lock_buyer

DEFAULT_OFFERS = {
    "sample_video": ("Vídeo Exemplo", 1000, ("sample_video_access",)),
    "vip_permanent": ("VIP Permanente", 2000, ("vip_permanent_access",)),
    "recovery_bundle": (
        "Combo Especial Example Store",
        2500,
        ("sample_video_access", "vip_permanent_access"),
    ),
}


class ConfiguredOffers(Mapping):
    """Local overrides preserve stable product codes and permission boundaries."""

    def __iter__(self):
        return iter(DEFAULT_OFFERS)

    def __len__(self):
        return len(DEFAULT_OFFERS)

    def __getitem__(self, code):
        default = DEFAULT_OFFERS[code]
        path = Settings().catalog_path
        if path is None:
            return default
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(DEFAULT_OFFERS):
            raise ValueError("Catalog must define the three documented product codes")
        for entry in data.values():
            if (
                not isinstance(entry, dict)
                or set(entry) != {"name", "amount_minor"}
                or not isinstance(entry["name"], str)
                or not 1 <= len(entry["name"].strip()) <= 100
                or type(entry["amount_minor"]) is not int
                or not 50 <= entry["amount_minor"] <= 100000000
            ):
                raise ValueError("Invalid catalog name or amount_minor")
        return data[code]["name"], data[code]["amount_minor"], default[2]


OFFERS = ConfiguredOffers()


def sync_video_offer(session):
    """Synchronize just the stable video code, including a never-seeded catalog."""
    code = "sample_video"
    if session.get(Product, code) is None:
        conflicting = session.scalar(
            select(Product.code)
            .where(
                Product.code != code,
                func.lower(Product.name).in_([OFFERS[code][0].casefold(), "vídeo anterior"]),
            )
            .limit(1)
        )
        if conflicting:
            # Do not guess how to rewrite foreign keys, historical purchases or external IDs.
            raise DomainError("video_product_code_conflict")
    name, amount, _ = OFFERS[code]
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    statement = (
        insert(Product)
        .values(
            code=code,
            name=name,
            amount_minor=amount,
            telegram_group_id=None,
            recovery_only=False,
            active=True,
            rights_reference=None,
        )
        .on_conflict_do_update(
            index_elements=[Product.code],
            set_={"name": name, "amount_minor": amount},
        )
    )
    # Upsert handles simultaneous first installs; refresh an already-loaded ORM instance.
    return session.scalars(
        statement.returning(Product), execution_options={"populate_existing": True}
    ).one()


def install_catalog(session, chat_id):
    if type(chat_id) is not int or chat_id >= 0:
        raise DomainError("vip_group_required")
    for code, (name, amount, permissions) in OFFERS.items():
        product = session.get(Product, code)
        if product is None:
            product = Product(code=code)
            session.add(product)
        product.name, product.amount_minor = name, amount
        product.telegram_group_id = chat_id if "vip_permanent_access" in permissions else None
        product.recovery_only = code == "recovery_bundle"
    session.flush()


def list_products(session):
    return list(
        session.scalars(
            select(Product)
            .where(Product.active.is_(True), Product.recovery_only.is_(False))
            .order_by(Product.code)
        )
    )


def create_product_order(
    session, code, buyer_id, request_id, *, ttl_seconds=1800, now=None, payment_provider="omega"
):
    now = int(time()) if now is None else now
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", request_id) or ttl_seconds <= 0:
        raise DomainError("invalid_sales_request")
    lock_buyer(session, buyer_id)
    existing = session.scalar(select(Order).where(Order.sales_request_id == request_id))
    if existing:
        if existing.product_id != code or existing.buyer_telegram_id != buyer_id:
            raise DomainError("sales_request_conflict")
        return existing
    product = session.get(Product, code)
    if product is None or not product.active or code not in OFFERS:
        raise DomainError("unavailable_product")
    if not product.rights_reference:
        raise DomainError("distribution_review_required")
    if product.recovery_only:
        task = session.get(RecoveryTask, buyer_id)
        if recovery_reason(session, task, now):
            raise DomainError("recovery_not_eligible")
        if session.scalar(select(Order.id).where(Order.combo_buyer_id == buyer_id)):
            raise DomainError("combo_already_reserved")
    order = Order(
        product_id=code,
        product_name=product.name,
        buyer_telegram_id=buyer_id,
        sales_request_id=request_id,
        combo_buyer_id=buyer_id if product.recovery_only else None,
        amount_minor=product.amount_minor,
        currency="BRL",
        duration_days=None,
        telegram_group_id=product.telegram_group_id,
        origin="recovery" if product.recovery_only else "standard",
        created_at=now,
        expires_at=now + ttl_seconds,
        payment_provider=payment_provider,
    )
    session.add(order)
    session.flush()
    for permission in OFFERS[code][2]:
        session.add(
            AccessGrant(
                order_id=order.id,
                permission=permission,
                telegram_group_id=product.telegram_group_id
                if permission == "vip_permanent_access"
                else None,
            )
        )
    session.add(AuditEvent(action="product_order_created", entity_id=order.id, occurred_at=now))
    session.flush()
    return order
