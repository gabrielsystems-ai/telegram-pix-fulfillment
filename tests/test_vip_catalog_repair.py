import pytest
from sqlalchemy import func, select

from vip.catalog import list_products, sync_video_offer
from vip.models import AccessGrant, Order, Payment, Product


def test_repair_missing_vip_only_is_idempotent_and_visible_in_catalog(db):
    from scripts.repair_vip_catalog import ensure_vip_product

    with db.transaction() as s:
        sync_video_offer(s)
    for _ in range(2):
        with db.transaction() as s:
            ensure_vip_product(s, -100123)
    with db.transaction() as s:
        assert [(p.code, p.amount_minor) for p in list_products(s)] == [
            ("sample_video", 1000),
            ("vip_permanent", 2000),
        ]
        vip = s.get(Product, "vip_permanent")
        assert vip.telegram_group_id == -100123
        assert vip.active and not vip.recovery_only
        assert vip.rights_reference is None
        assert s.scalar(select(func.count()).select_from(Product)) == 2
        for model in [Order, Payment, AccessGrant]:
            assert s.scalar(select(func.count()).select_from(model)) == 0


def snapshot(s):
    from vip.models import Base

    return {
        table.name: list(s.execute(select(table)).tuples()) for table in Base.metadata.sorted_tables
    }


def test_repair_keeps_existing_purchases_media_video_and_combo_byte_for_byte(db):
    from scripts.repair_vip_catalog import ensure_vip_product
    from tests.test_sample_delivery import purchased, registered

    purchased(db)
    registered(db)
    with db.transaction() as s:
        vip = s.get(Product, "vip_permanent")
        vip.active, vip.recovery_only, vip.rights_reference = False, True, "synthetic-reviewed"
        vip.amount_minor = 900  # Existing offers are never silently repriced.
    with db.transaction() as s:
        before = snapshot(s)
        ensure_vip_product(s, -100999)
        assert snapshot(s) == before


def test_concurrent_repair_does_not_duplicate_or_touch_other_products(db):
    from concurrent.futures import ThreadPoolExecutor

    from scripts.repair_vip_catalog import ensure_vip_product

    with db.transaction() as s:
        sync_video_offer(s)

    def attempt(_):
        with db.transaction() as s:
            ensure_vip_product(s, -100123)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(attempt, range(2)))
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Product)) == 2


@pytest.mark.parametrize("chat_id", [None, 0, 123, True])
def test_invalid_group_is_rejected_without_writes(db, chat_id):
    from scripts.repair_vip_catalog import ensure_vip_product
    from vip.errors import DomainError

    with pytest.raises(DomainError, match="vip_group_required"):
        with db.transaction() as s:
            ensure_vip_product(s, chat_id)
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Product)) == 0


def test_existing_alternate_code_is_not_duplicated_or_renamed(db):
    from scripts.repair_vip_catalog import ensure_vip_product
    from vip.errors import DomainError

    with db.transaction() as s:
        s.add(
            Product(
                code="existing-vip-id",
                name="VIP Permanente",
                amount_minor=2000,
                telegram_group_id=-100123,
            )
        )
    with pytest.raises(DomainError, match="vip_product_code_conflict"):
        with db.transaction() as s:
            ensure_vip_product(s, -100123)
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Product)) == 1


def test_missing_vip_repair_is_reflected_by_authenticated_api(db):
    from fastapi.testclient import TestClient

    from scripts.repair_vip_catalog import ensure_vip_product
    from vip.api import create_app
    from vip.config import Settings

    with db.transaction() as s:
        sync_video_offer(s)
        ensure_vip_product(s, -100123)
    settings = Settings(_env_file=None, sales_api_key="synthetic-key-with-at-least-32-characters")
    with TestClient(create_app(settings=settings, database=db)) as c:
        r = c.get(
            "/products", headers={"X-Sales-API-Key": settings.sales_api_key.get_secret_value()}
        )
        assert r.status_code == 200
        assert {p["code"]: p["amount_minor"] for p in r.json()} == {
            "sample_video": 1000,
            "vip_permanent": 2000,
        }


def test_cli_is_read_only_by_default_and_apply_is_explicit(db, monkeypatch, capsys):
    import json

    from scripts.repair_vip_catalog import main

    monkeypatch.setenv("DATABASE_URL", str(db.engine.url))
    monkeypatch.setenv("VIP_CHAT_ID", "-100123")
    monkeypatch.setattr("sys.argv", ["repair_vip_catalog.py"])
    main()
    assert json.loads(capsys.readouterr().out)["after"]["cause"] == "missing_product"
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Product)) == 0
    monkeypatch.setattr("sys.argv", ["repair_vip_catalog.py", "--apply"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["after"]["visible"] is True
    assert result["after"]["group_matches_configuration"] is True
    assert result["after"]["distribution_reviewed"] is False


@pytest.mark.parametrize(
    "flag",
    ["OMEGA_PIX_ENABLED", "VIP_GROUP_ENABLED", "RECOVERY_ENABLED", "RECOVERY_PROMOTIONS_APPROVED"],
)
def test_cli_apply_refuses_enabled_automation_before_any_write(db, monkeypatch, capsys, flag):
    from scripts.repair_vip_catalog import main

    monkeypatch.setenv("DATABASE_URL", str(db.engine.url))
    monkeypatch.setenv("VIP_CHAT_ID", "-100123")
    monkeypatch.setenv(flag, "true")
    monkeypatch.setattr("sys.argv", ["repair_vip_catalog.py", "--apply"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "repair_disable_automation_first" in capsys.readouterr().err
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Product)) == 0


def test_repair_does_not_approve_distribution_or_allow_unpaid_access(db):
    from scripts.repair_vip_catalog import ensure_vip_product
    from vip.catalog import create_product_order
    from vip.entitlements import product_accesses
    from vip.errors import DomainError
    from vip.users import register_user

    with db.transaction() as s:
        ensure_vip_product(s, -100123)
        register_user(s, 123)
    with pytest.raises(DomainError, match="distribution_review_required"):
        with db.transaction() as s:
            create_product_order(s, "vip_permanent", 123, "request-vip-test-0001")
    with db.transaction() as s:
        assert product_accesses(s, 123) == []
        assert s.scalar(select(func.count()).select_from(Order)) == 0
