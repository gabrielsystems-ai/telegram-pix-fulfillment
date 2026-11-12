"""Read-only Telegram readiness check; never starts polling or changes a group."""

import argparse
import json

from aiogram.methods import GetMe, GetWebhookInfo

from vip.config import Settings
from vip.db import Database
from vip.groups import VIPAccess
from vip.telegram_gateway import AiogramGroupGateway, GroupError


def inspect_telegram(settings, *, gateway=None, check_group=False):
    if any(
        (
            settings.omega_pix_enabled,
            settings.pushinpay_enabled,
            settings.vip_group_enabled,
            settings.recovery_enabled,
            settings.recovery_promotions_approved,
        )
    ):
        raise GroupError("preflight_disable_automation")
    if not settings.telegram_bot_token or not settings.telegram_bot_token.get_secret_value():
        raise GroupError("preflight_token_required")
    if check_group and settings.vip_chat_id is None:
        raise GroupError("preflight_chat_id_required")
    gateway = gateway or AiogramGroupGateway(settings.telegram_bot_token)
    bot = gateway.call(GetMe())
    if (
        not bot.is_bot
        or not settings.telegram_bot_username
        or (bot.username or "").casefold() != settings.telegram_bot_username.casefold()
    ):
        raise GroupError("preflight_unexpected_bot")
    webhook = gateway.call(GetWebhookInfo())
    if webhook.url:
        raise GroupError("preflight_webhook_active")
    result = {"bot": bot.username, "bot_id": bot.id, "webhook": "absent", "group_verified": False}
    if check_group:
        # Reuse the existing permission validator. No DB connection/query is made;
        # the production DATABASE_URL is never opened. No worker is instantiated.
        db = Database("sqlite://")
        try:
            probe = settings.model_copy(update={"vip_group_enabled": True})
            VIPAccess(probe, db, gateway).check_permissions()
        finally:
            db.engine.dispose()
        result.update(group_verified=True, chat_id=settings.vip_chat_id)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Read-only configured-bot preflight; all automation flags must be false"
    )
    parser.add_argument(
        "--group", action="store_true", help="Validate VIP_CHAT_ID and admin permissions"
    )
    args = parser.parse_args()
    try:
        result = inspect_telegram(Settings(), check_group=args.group)
    except GroupError as error:
        parser.exit(1, f"Preflight rejected: {error.code}\n")
    except ValueError:
        # Never echo validation inputs, bot tokens or private webhook addresses.
        parser.exit(1, "Preflight rejected: invalid_configuration\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
