import pytest
from sqlalchemy import select

from tests.test_domain import NOW
from vip import catalog
from vip.errors import DomainError
from vip.models import AccessGrant, Order, Product, RecoveryTask, Subscription
from vip.users import register_user


def setup_catalog(db):
    with db.transaction() as session:
        catalog.install_catalog(session, -100123)
        for product in session.scalars(select(Product)):
            product.rights_reference = "synthetic-rights-review"
        register_user(session, 123)


def product_order(db, code="sample_video", request="request-00000001", user_id=123):
    with db.transaction() as session:
        return catalog.create_product_order(session, code, user_id, request, now=NOW + 1201).id


def test_exact_catalog_and_idempotent_installation(db):
    setup_catalog(db)
    with db.transaction() as session:
        catalog.install_catalog(session, -100123)
        products = list(session.scalars(select(Product).order_by(Product.code)))
        assert [(p.code, p.amount_minor) for p in products] == [
            ("recovery_bundle", 2500),
            ("sample_video", 1000),
            ("vip_permanent", 2000),
        ]
        assert all(type(p.amount_minor) is int for p in products)
        assert [p.code for p in catalog.list_products(session)] == ["sample_video", "vip_permanent"]


def test_product_order_has_no_artificial_subscription_and_snapshots_price(db):
    setup_catalog(db)
    oid = product_order(db)
    with db.transaction() as session:
        session.get(Product, "sample_video").amount_minor = 999
        order = session.get(Order, oid)
        assert order.amount_minor == 1000
        assert order.duration_days is None and order.plan_id is None
        assert order.telegram_group_id is None
        assert order.buyer_telegram_id == 123 and order.origin == "standard"
        assert session.scalar(select(Subscription)) is None
        grants = list(session.scalars(select(AccessGrant).where(AccessGrant.order_id == oid)))
        assert [g.permission for g in grants] == ["sample_video_access"]
        assert grants[0].granted_at is None


def test_same_request_reuses_order_but_mismatched_identity_or_product_fails(db):
    setup_catalog(db)
    oid = product_order(db)
    assert product_order(db) == oid
    with pytest.raises(DomainError):
        product_order(db, "vip_permanent")
    with pytest.raises(DomainError):
        product_order(db, user_id=456)


def test_combo_is_not_public_and_requires_qualified_buyer(db):
    setup_catalog(db)
    with pytest.raises(DomainError, match="recovery_not_eligible"):
        product_order(db, "recovery_bundle")
    with db.transaction() as session:
        session.add(
            RecoveryTask(
                telegram_id=123,
                first_started_at=NOW,
                due_at=NOW + 1200,
                next_check_at=NOW + 1200,
                state="eligible",
                eligible_at=NOW + 1200,
            )
        )
    oid = product_order(db, "recovery_bundle")
    assert product_order(db, "recovery_bundle") == oid
    with pytest.raises(DomainError):
        product_order(db, "recovery_bundle", "request-00000002")
    with db.transaction() as session:
        order = session.get(Order, oid)
        assert order.amount_minor == 2500 and order.origin == "recovery"
        assert order.duration_days is None
        assert {g.permission for g in session.scalars(select(AccessGrant))} == {
            "sample_video_access",
            "vip_permanent_access",
        }


def test_material_review_is_required_without_any_age_requirement(db):
    with db.transaction() as session:
        catalog.install_catalog(session, -100123)
        register_user(session, 123)
    with pytest.raises(DomainError, match="distribution_review_required"):
        product_order(db)
    with db.transaction() as session:
        session.get(Product, "sample_video").rights_reference = "synthetic-rights-review"
    assert product_order(db)
