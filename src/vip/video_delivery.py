"""Private delivery using existing entitlements, media cache and durable audit claims."""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from time import time
from uuid import uuid4

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vip.config import Settings
from vip.entitlements import product_accesses
from vip.errors import DomainError
from vip.models import AuditEvent, BotMediaCache, User
from vip.users import lock_buyer

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger(__name__)


def profile():
    path = Settings().video_profile_path
    if path is None:
        raise DomainError("video_profile_required")
    return json.loads(path.read_text(encoding="utf-8"))


def update_video_offer(session):
    from vip.catalog import sync_video_offer

    return sync_video_offer(session)


def register_video(db, bot_id, video):
    expected = profile()
    if (
        video.file_size != expected["size"]
        or video.width != expected["width"]
        or video.height != expected["height"]
        or abs(video.duration - expected["duration"]) > 1
    ):
        raise DomainError("video_metadata_mismatch")
    with db.transaction() as session:
        insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
        session.execute(
            insert(BotMediaCache)
            .values(bot_id=bot_id, content_hash=expected["sha256"], file_id=video.file_id)
            .on_conflict_do_update(
                index_elements=[BotMediaCache.bot_id, BotMediaCache.content_hash],
                set_={"file_id": video.file_id},
            )
        )


def configured_video(db, bot_id):
    if Settings().video_profile_path is None:
        return False
    with db.transaction() as session:
        return session.get(BotMediaCache, (bot_id, profile()["sha256"])) is not None


class VideoSendError(Exception):
    pass


class TelegramVideoSender:
    def __init__(self, token, *, caption="Vídeo Exemplo"):
        self.token = token
        self.caption = caption
        self.retry_after = 0

    def send(self, user_id, file_id):
        self.retry_after = 0

        async def invoke():
            # An HTTP client on the worker, never getUpdates or another polling process.
            async with Bot(
                self.token.get_secret_value(), session=AiohttpSession(timeout=10)
            ) as bot:
                return await bot.send_video(
                    user_id,
                    file_id,
                    caption=self.caption,
                    protect_content=True,
                    supports_streaming=True,
                )

        try:
            return asyncio.run(invoke())
        except TelegramRetryAfter as error:
            self.retry_after = int(error.retry_after)
            raise VideoSendError("rejected") from None
        except (TelegramBadRequest, TelegramForbiddenError):
            raise VideoSendError("rejected") from None
        except Exception:
            raise VideoSendError("uncertain") from None


def _terminal(claim_id, result):
    return hashlib.sha256(f"sample/{claim_id}/{result}".encode()).hexdigest()[:32]


def _authorized(session, user_id):
    return any(a.permission == "sample_video_access" for a in product_accesses(session, user_id))


def deliver_video(db, bot_id, user_id, sender):
    expected = profile()
    key = hashlib.sha256(f"sample/{bot_id}/{user_id}/{expected['sha256']}".encode()).hexdigest()[
        :32
    ]
    with db.transaction() as session:
        if session.get(User, user_id) is None:
            return "denied"
        lock_buyer(session, user_id)
        if not _authorized(session, user_id):
            return "denied"
        media = session.get(BotMediaCache, (bot_id, expected["sha256"]))
        if media is None:
            return "unavailable"
        file_id = media.file_id
        claims = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "video_delivery_claimed",
                    AuditEvent.entity_id == key,
                )
            )
        )
        for claim in claims:
            if session.get(AuditEvent, _terminal(claim.id, "sent")):
                return "already_sent"
            if session.get(AuditEvent, _terminal(claim.id, "failed")) is None:
                return "uncertain"
        claim_id = uuid4().hex
        session.add(
            AuditEvent(
                id=claim_id, action="video_delivery_claimed", entity_id=key, occurred_at=int(time())
            )
        )
    # Claim commits BEFORE the external call. A crash/ambiguous result never auto-resends.
    try:
        with db.transaction() as session:
            lock_buyer(session, user_id)
            if not _authorized(session, user_id):
                result = "failed"
                status = "denied"
            else:
                try:
                    # The same buyer lock used by refunds remains held across the bounded send.
                    sender.send(user_id, file_id)
                    result, status = "sent", "sent"
                except VideoSendError as error:
                    result = "failed" if str(error) == "rejected" else "uncertain"
                    status = "unavailable" if result == "failed" else "uncertain"
                except Exception:
                    result, status = "uncertain", "uncertain"
            session.add(
                AuditEvent(
                    id=_terminal(claim_id, result),
                    action="video_delivery_" + result,
                    entity_id=key,
                    occurred_at=int(time()),
                )
            )
        return status
    except Exception:
        LOG.warning("Video delivery outcome unconfirmed; durable claim retained")
        return "uncertain"
