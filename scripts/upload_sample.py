"""Manual private upload to a configured administrator. Never starts polling."""

import argparse
import asyncio
import hashlib
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from vip.config import Settings
from vip.video_delivery import profile


async def upload(settings, path, admin_id):
    if admin_id not in settings.admin_ids:
        raise ValueError("administrator_required")
    expected = profile()
    if (
        path.stat().st_size != expected["size"]
        or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]
    ):
        raise ValueError("definitive_video_required")
    if not settings.telegram_bot_token:
        raise ValueError("telegram_token_required")
    async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
        identity = await bot.get_me()
        if (
            not settings.telegram_bot_username
            or identity.username != settings.telegram_bot_username
        ):
            raise ValueError("unexpected_bot")
        await bot.send_video(
            admin_id,
            FSInputFile(path, filename="sample.mp4"),
            supports_streaming=True,
            protect_content=True,
            caption="Cadastro administrativo: VÃ­deo Exemplo. "
            "Responda a esta mensagem com /cadastrarvideo.",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--admin-id", type=int, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(upload(Settings(), args.video, args.admin_id))
    except Exception as error:
        parser.exit(
            1,
            f"Upload rejected or unconfirmed: {type(error).__name__}. "
            "Check settings and the admin conversation before retrying.\n",
        )
    print(
        "Upload completed in the administrator's private conversation. Reply /cadastrarvideo there."
    )


if __name__ == "__main__":
    main()
