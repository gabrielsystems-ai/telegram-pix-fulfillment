import json

import pytest

from vip.catalog import OFFERS, install_catalog
from vip.config import Settings
from vip.models import Product
from vip.video_delivery import configured_video


def test_custom_catalog_updates_offer_without_changing_permission(db, monkeypatch, tmp_path):
    path = tmp_path / "catalog.json"
    data = {
        code: {"name": "Configured " + code, "amount_minor": 3400 + index}
        for index, code in enumerate(OFFERS)
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CATALOG_PATH", str(path))
    assert OFFERS["sample_video"] == ("Configured sample_video", 3400, ("sample_video_access",))
    with db.transaction() as session:
        install_catalog(session, -100123)
        product = session.get(Product, "sample_video")
        assert (product.name, product.amount_minor) == ("Configured sample_video", 3400)
        assert product.rights_reference is None


def test_custom_catalog_rejects_boolean_amount(monkeypatch, tmp_path):
    path = tmp_path / "catalog.json"
    data = {code: {"name": "Sample", "amount_minor": True} for code in OFFERS}
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CATALOG_PATH", str(path))
    with pytest.raises(ValueError, match="amount_minor"):
        OFFERS["sample_video"]


def test_video_without_profile_is_unavailable_without_reading_private_files(db, monkeypatch):
    monkeypatch.setenv("VIDEO_PROFILE_PATH", "")
    assert configured_video(db, 999) is False
    assert Settings(_env_file=None).start_banner_path is None
    assert Settings(_env_file=None).start_video_path is None


def test_primary_provider_defaults_to_disabled_pushin_sandbox(monkeypatch):
    for name in ("PAYMENT_PROVIDER", "PUSHINPAY_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.payment_provider == "pushinpay"
    assert not settings.pushinpay_enabled
    assert "api-sandbox.pushinpay.com.br" in str(settings.pushinpay_base_url)
