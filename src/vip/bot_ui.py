"""Telegram presentation only. Authorization remains in the existing domain services."""

from collections import OrderedDict
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)

from vip.bot_media import BannerPhoto, BannerVideo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
START_TEXT = "<b>Bem-vindo ao catálogo de exemplo.</b>\nEscolha um produto ou consulte seu acesso."


def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Vídeo Exemplo", callback_data="menu:sample"),
                InlineKeyboardButton(text="💎 VIP Permanente", callback_data="menu:vip"),
            ],
            [
                InlineKeyboardButton(text="🎟️ Acessar VIP", callback_data="menu:access"),
                InlineKeyboardButton(text="💬 Suporte", callback_data="menu:support"),
            ],
        ]
    )


def context_keyboard(
    *, link=False, access=False, support=False, video=False, discover_vip=False, buy=None
):
    rows = []
    if buy:
        rows.append([InlineKeyboardButton(text="Comprar agora", callback_data="pay:buy:" + buy)])
    if video:
        rows.append([InlineKeyboardButton(text="▶️ Receber vídeo", callback_data="menu:video")])
    if link:
        rows.append([InlineKeyboardButton(text="Recuperar acesso", callback_data="menu:link")])
    if access:
        rows.append([InlineKeyboardButton(text="🎟️ Acessar VIP", callback_data="menu:access")])
    if discover_vip:
        rows.append(
            [InlineKeyboardButton(text="💎 Conhecer VIP Permanente", callback_data="menu:vip")]
        )
    if support:
        rows.append([InlineKeyboardButton(text="💬 Suporte", callback_data="menu:support")])
    rows.append([InlineKeyboardButton(text="← Início", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def configure_public_commands(bot):
    commands = [
        BotCommand(command="start", description="Início"),
        BotCommand(command="sample", description="Vídeo Exemplo"),
        BotCommand(command="vip", description="VIP Permanente"),
    ]
    for scope in (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats()):
        for language in ("", "pt"):
            await bot.set_my_commands(commands, scope=scope, language_code=language)


class MenuView:
    def __init__(self, banner_path, *, database=None, video_path=None):
        self.banner_path = banner_path
        path = Path(banner_path) if banner_path else None
        if path is not None and not path.is_absolute():
            path = PROJECT_ROOT / path
        self.banner = BannerPhoto(path, database) if path is not None else None
        video = Path(video_path) if video_path else None
        if video is not None and not video.is_absolute():
            video = PROJECT_ROOT / video
        self.video_path = video
        self.video = BannerVideo(video, database) if video is not None else None
        # UI cache only: no payment, permission or binding token state lives here.
        self.recent = OrderedDict()
        self.presentations = {}
        self.modes = {}

    def _remember(self, user_id, message_id, photo, text=None, keyboard=None, *, media="brand"):
        self.recent[user_id] = (message_id, photo)
        self.presentations[user_id] = (
            message_id,
            text,
            keyboard.model_dump_json() if keyboard else None,
        )
        self.recent.move_to_end(user_id)
        self.modes[user_id] = media
        if len(self.recent) > 1024:
            old_user, _ = self.recent.popitem(last=False)
            self.presentations.pop(old_user, None)
            self.modes.pop(old_user, None)

    async def show(self, bot, user_id, text, keyboard, *, source=None, fresh=False, media="brand"):
        if (
            source
            and self.modes.get(user_id) == media
            and self.presentations.get(user_id)
            == (
                source.message_id,
                text,
                keyboard.model_dump_json(),
            )
        ):
            return
        target = (
            (source.message_id, bool(source.photo or source.video))
            if source
            else (None if fresh else self.recent.get(user_id))
        )
        path = Path(self.banner_path) if self.banner_path else None
        if path is not None and not path.is_absolute():
            path = PROJECT_ROOT / path
        wants_media = media == "brand" and (
            self.video_path is not None
            and self.video_path.is_file()
            or path is not None
            and path.is_file()
        )
        owned_target = target and self.recent.get(user_id) == target
        compatible = target and (
            not wants_media
            and not target[1]
            or media == "brand"
            and owned_target
            and self.modes.get(user_id) == "brand"
        )
        if compatible:
            message_id, photo = target
            try:
                if photo:
                    await bot.edit_message_caption(
                        chat_id=user_id,
                        message_id=message_id,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                else:
                    await bot.edit_message_text(
                        text,
                        chat_id=user_id,
                        message_id=message_id,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    # A definite rejection (deleted/uneditable message) permits a new screen.
                    pass
                else:
                    self._remember(user_id, message_id, photo, text, keyboard, media=media)
                    return
            else:
                self._remember(user_id, message_id, photo, text, keyboard, media=media)
                return

        # Deliver inline buttons atomically; a later edit can fail after the photo is sent.
        message = None
        if media == "brand" and self.video_path is not None and self.video_path.is_file():
            try:
                message = await self.video.send(
                    bot,
                    user_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except (OSError, TelegramBadRequest):
                # A definite rejection/read error permits the existing photo fallback.
                pass
        if media == "brand" and message is None and path is not None and path.is_file():
            try:
                message = await self.banner.send(
                    bot,
                    user_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except (OSError, TelegramBadRequest):
                # No photo was delivered on a definite rejection/read failure.
                pass
        if message is None:
            message = await bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        self._remember(
            user_id,
            message.message_id,
            bool(message.photo or message.video),
            text,
            keyboard,
            media=media,
        )
