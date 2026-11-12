"""One banner upload, then file_id reuse. Persistent data is only a UI cache."""

import asyncio
import hashlib
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vip.models import BotMediaCache

LOG = logging.getLogger(__name__)


class BannerPhoto:
    namespace = b""

    def __init__(self, path, database=None):
        self.path, self.database = path, database
        self.lock = asyncio.Lock()
        self.digest = None
        self.ids = {}

    async def _send(self, bot, chat_id, media, **options):
        return await bot.send_photo(chat_id, media, **options)

    def _file_id(self, message):
        return message.photo[-1].file_id if message.photo else None

    def _read(self, bot_id):
        if self.database is None:
            return None
        with self.database.transaction() as session:
            entry = session.get(BotMediaCache, (bot_id, self.digest))
            return entry.file_id if entry else None

    def _write(self, bot_id, file_id):
        if self.database is None:
            return
        with self.database.transaction() as session:
            if file_id is None:
                session.execute(
                    delete(BotMediaCache).where(
                        BotMediaCache.bot_id == bot_id, BotMediaCache.content_hash == self.digest
                    )
                )
            else:
                insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
                session.execute(
                    insert(BotMediaCache)
                    .values(bot_id=bot_id, content_hash=self.digest, file_id=file_id)
                    .on_conflict_do_update(
                        index_elements=[BotMediaCache.bot_id, BotMediaCache.content_hash],
                        set_={"file_id": file_id},
                    )
                )

    async def _persist(self, bot_id, file_id):
        try:
            await asyncio.to_thread(self._write, bot_id, file_id)
        except Exception:
            LOG.warning("Banner cache persistence unavailable; using process cache")

    async def send(self, bot, chat_id, **options):
        if bot.id not in self.ids:
            async with self.lock:
                if bot.id not in self.ids:
                    # Hash once per process; replacing the asset takes effect on restart.
                    self.digest = await asyncio.to_thread(
                        lambda: hashlib.sha256(self.namespace + self.path.read_bytes()).hexdigest()
                    )
                    try:
                        self.ids[bot.id] = await asyncio.to_thread(self._read, bot.id)
                    except Exception:
                        self.ids[bot.id] = None
                        LOG.warning("Banner cache read unavailable; using local photo")
        rejected = self.ids[bot.id]
        if rejected:
            try:
                # Warm sends run concurrently; never serialize users behind Telegram I/O.
                return await self._send(bot, chat_id, rejected, **options)
            except TelegramBadRequest as error:
                if not any(
                    term in error.message.lower()
                    for term in ("file identifier", "file_id", "file reference", "file_reference")
                ):
                    raise
        async with self.lock:
            if self.ids[bot.id] == rejected:
                if rejected:
                    self.ids[bot.id] = None
                    await self._persist(bot.id, None)
                message = await self._send(bot, chat_id, FSInputFile(self.path), **options)
                file_id = self._file_id(message)
                if file_id:
                    self.ids[bot.id] = file_id
                    await self._persist(bot.id, self.ids[bot.id])
                return message
            current = self.ids[bot.id]
        # A concurrent first upload already supplied the ID.
        return await self._send(bot, chat_id, current, **options)


class BannerVideo(BannerPhoto):
    # Telegram file IDs are media-type specific; never reuse a photo ID as video.
    namespace = b"example-store-start-video\0"

    async def _send(self, bot, chat_id, media, **options):
        return await bot.send_video(chat_id, media, supports_streaming=True, **options)

    def _file_id(self, message):
        return message.video.file_id if message.video else None
