import re
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = Field(default="sqlite:///./vip.db", repr=False)
    telegram_bot_token: SecretStr | None = None
    sales_api_key: SecretStr | None = None
    admin_telegram_ids: str = ""
    support_contact: str = "Suporte ainda não configurado."
    start_banner_path: Path | None = Field(default=None, repr=False)
    start_video_path: Path | None = Field(default=None, repr=False)
    video_profile_path: Path | None = Field(default=None, repr=False)
    telegram_bot_username: str | None = None
    catalog_path: Path | None = Field(default=None, repr=False)
    bot_timing_enabled: bool = False

    @field_validator(
        "start_banner_path", "start_video_path", "video_profile_path", "catalog_path", mode="before"
    )
    @classmethod
    def normalize_start_banner_path(cls, value):
        return None if value == "" else value

    link_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    order_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    # Official base and auth headers verified; transaction/webhook operations remain blocked.
    omega_base_url: HttpUrl = Field(default="https://app.omegapayments.com.br/api/v1", repr=False)
    omega_client_id: SecretStr | None = None
    omega_client_secret: SecretStr | None = None
    omega_pix_enabled: bool = False
    omega_callback_url: HttpUrl | None = Field(default=None, repr=False)
    payment_provider: Literal["omega", "pushinpay"] = "pushinpay"
    pushinpay_enabled: bool = False
    pushinpay_token: SecretStr | None = None
    pushinpay_webhook_token: SecretStr | None = Field(default=None, min_length=32)
    pushinpay_base_url: HttpUrl = Field(
        default="https://api-sandbox.pushinpay.com.br/api", repr=False
    )
    pushinpay_callback_url: HttpUrl | None = Field(default=None, repr=False)

    @field_validator("pushinpay_base_url")
    @classmethod
    def validate_pushinpay_host(cls, value):
        if str(value).rstrip("/") not in {
            "https://api.pushinpay.com.br/api",
            "https://api-sandbox.pushinpay.com.br/api",
        }:
            raise ValueError("Use an official Pushin Pay API origin")
        return value

    vip_group_enabled: bool = False
    vip_chat_id: int | None = Field(default=None, lt=0)
    vip_invite_ttl_seconds: int = Field(default=300, ge=30, le=900)
    vip_sync_interval_seconds: int = Field(default=60, ge=5, le=3600)
    recovery_enabled: bool = False
    recovery_delay_seconds: int = Field(default=1200, ge=1200, le=86400)
    recovery_promotions_approved: bool = False
    recovery_template: str = Field(
        default="Retome sua compra pelo menu. Consulte os produtos e valores antes de confirmar.",
        min_length=1,
        max_length=3500,
    )

    @field_validator("recovery_template")
    @classmethod
    def validate_recovery_template(cls, value):
        if re.search(r"https?://|tg://|t\.me/|www\.", value, re.IGNORECASE):
            raise ValueError("Recovery templates cannot include links")
        return value

    @field_validator("vip_chat_id", mode="before")
    @classmethod
    def normalize_vip_chat_id(cls, value):
        return None if value == "" else value

    @field_validator(
        "omega_callback_url", "pushinpay_callback_url", "pushinpay_webhook_token", mode="before"
    )
    @classmethod
    def normalize_callback_url(cls, value):
        return None if value == "" else value

    @field_validator("omega_client_id", "omega_client_secret", "pushinpay_token")
    @classmethod
    def normalize_omega_credentials(cls, value: SecretStr | None) -> SecretStr | None:
        return value if value is not None and value.get_secret_value() else None

    @field_validator("omega_base_url", "omega_callback_url", "pushinpay_callback_url")
    @classmethod
    def validate_omega_base_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return value
        if (
            value.scheme != "https"
            or value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError("Omega URLs must use HTTPS without credentials, query or fragment")
        return value

    @field_validator("sales_api_key")
    @classmethod
    def validate_sales_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        if len(value.get_secret_value()) < 32:
            raise ValueError("SALES_API_KEY must contain at least 32 characters")
        return value

    @field_validator("admin_telegram_ids")
    @classmethod
    def validate_admin_ids(cls, value: str) -> str:
        if any(
            not item.strip().isdigit() or int(item) <= 0
            for item in value.split(",")
            if item.strip()
        ):
            raise ValueError("ADMIN_TELEGRAM_IDS must be positive numeric IDs separated by commas")
        return value

    @property
    def admin_ids(self) -> frozenset[int]:
        return frozenset(int(item) for item in self.admin_telegram_ids.split(",") if item.strip())
