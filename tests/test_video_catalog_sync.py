from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from tests.test_bot_ui import UI
from tests.test_domain import NOW
from tests.test_sample_delivery import Sender, purchased, registered
from vip.catalog import create_product_order
from vip.config import Settings
from vip.errors import DomainError
from vip.models import AccessGrant, BotMediaCache, Order, Payment, Product
from vip.users import register_user
from vip.video_delivery import deliver_video, update_video_offer


def test_empty_catalog_sync_creates_only_canonical_video_and_is_idempotent(db):
    for _ in range(2):
        with db.transaction() as session:
            update_video_offer(session)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        product = session.get(Product, "sample_video")
        assert (product.name, product.amount_minor) == ("Vídeo Exemplo", 1000)
        assert product.telegram_group_id is None
        assert product.rights_reference is None
        assert product.recovery_only is False
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        assert session.scalar(select(func.count()).select_from(AccessGrant)) == 0


async def test_admin_command_initializes_empty_catalog_and_repeated_calls_succeed(db):
    ui = UI(db, settings=Settings(_env_file=None, admin_telegram_ids="123"))
    await ui.text("/atualizarvideo", user_id=456)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 0
    for _ in range(2):
        await ui.text("/atualizarvideo")
        assert "R$ 10,00" in ui.screen()
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 1


def test_legacy_catalog_sync_keeps_orders_permissions_media_and_other_products(db):
    oid = purchased(db, old=True)
    registered(db)
    with db.transaction() as session:
        video = session.get(Product, "sample_video")
        video.name, video.amount_minor, video.active = "Vídeo Anterior", 900, False
        before = {
            p.code: (p.name, p.amount_minor, p.telegram_group_id, p.rights_reference, p.active)
            for p in session.scalars(select(Product))
            if p.code != "sample_video"
        }
        review = video.rights_reference
        update_video_offer(session)
        assert video.name == "Vídeo Exemplo"
        assert video.amount_minor == 1000
        assert video.active is False
        assert video.rights_reference == review
        assert before == {
            p.code: (p.name, p.amount_minor, p.telegram_group_id, p.rights_reference, p.active)
            for p in session.scalars(select(Product))
            if p.code != "sample_video"
        }
        order = session.get(Order, oid)
        assert (order.product_name, order.amount_minor) == ("Vídeo Anterior", 900)
        assert session.scalar(select(func.count()).select_from(BotMediaCache)) == 1
    assert deliver_video(db, 999, 123, Sender()) == "sent"


def test_parallel_empty_catalog_sync_does_not_duplicate_products(db):
    def synchronize(_):
        with db.transaction() as session:
            update_video_offer(session)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(synchronize, range(2)))
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 1


def test_sync_does_not_bypass_existing_distribution_review(db):
    with db.transaction() as session:
        update_video_offer(session)
        register_user(session, 123)
    with pytest.raises(DomainError, match="distribution_review_required"):
        with db.transaction() as session:
            create_product_order(session, "sample_video", 123, "request-1234567890")
    with db.transaction() as session:
        session.get(Product, "sample_video").rights_reference = "synthetic-reviewed-material"
        order = create_product_order(
            session, "sample_video", 123, "request-1234567890", now=NOW + 1201
        )
        oid = order.id
        assert order.amount_minor == 1000
        assert order.product_id == "sample_video"
        assert session.scalar(select(AccessGrant.permission)) == "sample_video_access"
    from tests.test_product_access import bind, settle

    bind(db, settle(db, oid))
    registered(db)
    assert deliver_video(db, 999, 123, Sender()) == "sent"


def test_alternative_identifier_is_not_silently_duplicated(db):
    with db.transaction() as session:
        session.add(
            Product(
                code="external-video-code",
                name="Vídeo Exemplo",
                amount_minor=1000,
                telegram_group_id=None,
            )
        )
    with pytest.raises(DomainError, match="video_product_code_conflict"):
        with db.transaction() as session:
            update_video_offer(session)
    with db.transaction() as session:
        assert session.get(Product, "sample_video") is None
        assert session.get(Product, "external-video-code") is not None
