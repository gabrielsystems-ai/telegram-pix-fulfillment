import pytest

from tests.media_fixtures import MEDIA_ROOT, PROFILE
from vip.db import Database
from vip.models import Base


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    # Tests must never activate credentials or financial flags from a developer's .env.
    for name in [
        "OMEGA_CLIENT_ID",
        "OMEGA_CLIENT_SECRET",
        "OMEGA_CALLBACK_URL",
        "TELEGRAM_BOT_TOKEN",
        "SALES_API_KEY",
        "ADMIN_TELEGRAM_IDS",
    ]:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("OMEGA_PIX_ENABLED", "false")
    monkeypatch.setenv("VIP_GROUP_ENABLED", "false")
    monkeypatch.setenv("VIP_CHAT_ID", "")
    monkeypatch.setenv("RECOVERY_ENABLED", "false")
    monkeypatch.setenv("RECOVERY_PROMOTIONS_APPROVED", "false")
    monkeypatch.setenv("BOT_TIMING_ENABLED", "false")
    monkeypatch.setenv("VIDEO_PROFILE_PATH", str(PROFILE))
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "example_fulfillment_bot")
    monkeypatch.setenv("CATALOG_PATH", "")
    monkeypatch.setenv("PUSHINPAY_BASE_URL", "https://api.pushinpay.com.br/api")
    monkeypatch.setenv("RECOVERY_DELAY_SECONDS", "1200")
    monkeypatch.setenv("START_BANNER_PATH", "")
    monkeypatch.setenv("START_VIDEO_PATH", "")
    monkeypatch.setenv("PAYMENT_PROVIDER", "omega")
    monkeypatch.setenv("PUSHINPAY_ENABLED", "false")
    for name in ("PUSHINPAY_TOKEN", "PUSHINPAY_WEBHOOK_TOKEN", "PUSHINPAY_CALLBACK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RECOVERY_TEMPLATE", raising=False)
    monkeypatch.setenv("OMEGA_BASE_URL", "https://app.omegapayments.com.br/api/v1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'isolated.db').as_posix()}")


def pytest_sessionfinish(session, exitstatus):
    import shutil

    if (
        MEDIA_ROOT.parent.resolve()
        != __import__("pathlib").Path(__import__("tempfile").gettempdir()).resolve()
    ):
        raise RuntimeError("Unexpected test media directory")
    shutil.rmtree(MEDIA_ROOT)


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(database.engine)
    yield database
    database.engine.dispose()
