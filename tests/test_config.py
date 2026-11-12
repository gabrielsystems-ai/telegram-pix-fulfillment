import pytest
from pydantic import ValidationError

from vip.config import Settings


def test_secrets_are_hidden_and_empty_admin_list_grants_nobody():
    secret = "synthetic-secret-with-at-least-32-characters"
    settings = Settings(
        _env_file=None,
        sales_api_key=secret,
        telegram_bot_token=secret,
        database_url="postgresql+psycopg://user:secret-password@localhost/vip",
    )
    assert secret not in repr(settings)
    assert "secret-password" not in repr(settings)
    assert settings.admin_ids == frozenset()
    assert Settings(_env_file=None, sales_api_key="").sales_api_key is None


def test_invalid_configuration_does_not_print_supplied_secret():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, sales_api_key="private-short-key")
    assert "private-short-key" not in str(error.value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_telegram_ids="123,not-an-id")


def test_omega_credentials_load_from_environment_without_exposing_them(monkeypatch):
    client_id = "synthetic-public-client-id"
    client_secret = "synthetic-private-client-secret"
    monkeypatch.setenv("OMEGA_CLIENT_ID", client_id)
    monkeypatch.setenv("OMEGA_CLIENT_SECRET", client_secret)
    settings = Settings(_env_file=None)
    assert settings.omega_client_id.get_secret_value() == client_id
    assert settings.omega_client_secret.get_secret_value() == client_secret
    assert client_id not in repr(settings)
    assert client_secret not in repr(settings)
    assert str(settings.omega_base_url) == "https://app.omegapayments.com.br/api/v1"


def test_blank_omega_credentials_remain_unconfigured(monkeypatch):
    monkeypatch.setenv("OMEGA_CLIENT_ID", "")
    monkeypatch.setenv("OMEGA_CLIENT_SECRET", "")
    settings = Settings(_env_file=None)
    assert settings.omega_client_id is None
    assert settings.omega_client_secret is None


def test_omega_base_url_requires_https_without_embedded_credentials():
    for url in [
        "http://app.omegapayments.com.br/api/v1",
        "https://user:private-password@app.omegapayments.com.br/api/v1",
        "https://app.omegapayments.com.br/api/v1?secret=private-key",
    ]:
        with pytest.raises(ValidationError) as error:
            Settings(_env_file=None, omega_base_url=url)
        assert "private-password" not in str(error.value)
        assert "private-key" not in str(error.value)


def test_empty_callback_in_env_example_is_optional_when_integration_is_disabled(monkeypatch):
    monkeypatch.setenv("OMEGA_CALLBACK_URL", "")
    assert Settings(_env_file=None).omega_callback_url is None
