import pytest
from aiogram.methods import GetChat, GetChatMember, GetMe, GetWebhookInfo
from aiogram.types import WebhookInfo

from scripts.telegram_preflight import inspect_telegram
from tests.test_groups import CHAT, TelegramDouble
from vip.config import Settings
from vip.telegram_gateway import GroupError


class ReadOnlyTelegram(TelegramDouble):
    def __init__(self):
        super().__init__()
        self.bot_user = self.bot_user.model_copy(update={"username": "example_fulfillment_bot"})
        self.webhook_url = ""

    def call(self, method):
        assert isinstance(method, (GetMe, GetWebhookInfo, GetChat, GetChatMember))
        if isinstance(method, GetWebhookInfo):
            self.calls.append(method)
            return WebhookInfo(
                url=self.webhook_url, has_custom_certificate=False, pending_update_count=0
            )
        return super().call(method)


def configuration(**updates):
    return Settings(
        _env_file=None,
        telegram_bot_token="999:synthetic-test-token",
        database_url="postgresql+psycopg://must-not-open.example/test",
        **updates,
    )


def test_identity_preflight_needs_no_database_or_group_and_performs_only_reads():
    telegram = ReadOnlyTelegram()
    result = inspect_telegram(configuration(), gateway=telegram)
    assert result == {
        "bot": "example_fulfillment_bot",
        "bot_id": 999,
        "webhook": "absent",
        "group_verified": False,
    }
    assert [type(call) for call in telegram.calls] == [GetMe, GetWebhookInfo]


def test_group_preflight_checks_permissions_while_all_flags_stay_disabled():
    telegram = ReadOnlyTelegram()
    settings = configuration(vip_chat_id=CHAT)
    result = inspect_telegram(settings, gateway=telegram, check_group=True)
    assert result["group_verified"] and result["chat_id"] == CHAT
    assert settings.vip_group_enabled is False and settings.recovery_enabled is False
    assert not settings.omega_pix_enabled and not settings.recovery_promotions_approved


@pytest.mark.parametrize(
    "flag",
    ["omega_pix_enabled", "vip_group_enabled", "recovery_enabled", "recovery_promotions_approved"],
)
def test_preflight_refuses_enabled_automation_without_network_calls(flag):
    telegram = ReadOnlyTelegram()
    with pytest.raises(GroupError, match="preflight_disable_automation"):
        inspect_telegram(configuration(**{flag: True}), gateway=telegram)
    assert telegram.calls == []


def test_preflight_detects_another_bot_without_printing_its_token():
    telegram = ReadOnlyTelegram()
    telegram.bot_user = telegram.bot_user.model_copy(update={"username": "another_bot"})
    with pytest.raises(GroupError, match="preflight_unexpected_bot"):
        inspect_telegram(configuration(), gateway=telegram)
    assert len(telegram.calls) == 1


def test_active_webhook_blocks_preflight_without_exposing_or_deleting_it():
    telegram = ReadOnlyTelegram()
    telegram.webhook_url = "https://private.example/synthetic-secret"
    with pytest.raises(GroupError) as error:
        inspect_telegram(configuration(vip_chat_id=CHAT), gateway=telegram, check_group=True)
    assert error.value.code == "preflight_webhook_active"
    assert "private.example" not in str(error.value)
    assert [type(call) for call in telegram.calls] == [GetMe, GetWebhookInfo]


@pytest.mark.parametrize("permission", ["invite_right", "restrict_right"])
def test_missing_group_permissions_are_rejected_before_automation(permission):
    telegram = ReadOnlyTelegram()
    setattr(telegram, permission, False)
    with pytest.raises(GroupError, match="vip_admin_permissions_required"):
        inspect_telegram(configuration(vip_chat_id=CHAT), gateway=telegram, check_group=True)


def test_public_group_is_rejected_without_any_group_mutation():
    telegram = ReadOnlyTelegram()
    telegram.private = False
    with pytest.raises(GroupError, match="vip_private_supergroup_required"):
        inspect_telegram(configuration(vip_chat_id=CHAT), gateway=telegram, check_group=True)


def test_missing_token_or_group_id_fails_before_network():
    telegram = ReadOnlyTelegram()
    with pytest.raises(GroupError, match="preflight_token_required"):
        inspect_telegram(Settings(_env_file=None), gateway=telegram)
    with pytest.raises(GroupError, match="preflight_chat_id_required"):
        inspect_telegram(configuration(), gateway=telegram, check_group=True)
    assert telegram.calls == []
