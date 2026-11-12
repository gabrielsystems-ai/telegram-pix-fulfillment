import hashlib
from dataclasses import dataclass, field
from time import time
from uuid import uuid4

from aiogram.methods import (
    ApproveChatJoinRequest,
    BanChatMember,
    CreateChatInviteLink,
    DeclineChatJoinRequest,
    GetChat,
    GetChatMember,
    GetMe,
    RevokeChatInviteLink,
    UnbanChatMember,
)
from aiogram.types import ChatJoinRequest
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vip.config import Settings
from vip.db import Database
from vip.models import (
    AccessGrant,
    AuditEvent,
    Order,
    Subscription,
    User,
    VIPInvite,
    VIPMember,
    VIPSyncLease,
)
from vip.subscriptions import expire_subscriptions, vip_entitlements
from vip.telegram_gateway import GroupError, GroupGateway
from vip.users import register_user


@dataclass(frozen=True)
class InviteTicket:
    url: str | None = field(repr=False)
    expires_at: int
    status: str = "issued"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _present(remote) -> bool:
    return remote.status == "member" or remote.status == "restricted" and remote.is_member


class VIPAccess:
    """Single private supergroup. Public methods are run in a worker thread, not the bot loop."""

    def __init__(self, settings: Settings, db: Database, gateway: GroupGateway, *, clock=time):
        self.settings, self.db, self.gateway, self.clock = settings, db, gateway, clock

    def _now(self):
        return int(self.clock())

    def _enabled(self):
        if not self.settings.vip_group_enabled or self.settings.vip_chat_id is None:
            raise GroupError("vip_group_disabled")

    @property
    def chat_id(self):
        return self.settings.vip_chat_id

    def _permissions(self):
        self._enabled()
        bot = self.gateway.call(GetMe())
        chat = self.gateway.call(GetChat(chat_id=self.chat_id))
        if chat.id != self.chat_id or chat.type != "supergroup" or getattr(chat, "username", None):
            raise GroupError("vip_private_supergroup_required")
        member = self.gateway.call(GetChatMember(chat_id=self.chat_id, user_id=bot.id))
        if (
            member.status != "administrator"
            or not getattr(member, "can_invite_users", False)
            or not getattr(member, "can_restrict_members", False)
        ):
            raise GroupError("vip_admin_permissions_required")
        return bot.id

    def _entitlements(self, session, user_id):
        return vip_entitlements(session, user_id, self.chat_id, now=self._now())

    def _member(self, session, user_id):
        register_user(session, user_id)
        # Shared with binding and financial revocation; held until the Telegram decision commits.
        session.execute(
            update(User).where(User.telegram_id == user_id).values(telegram_id=User.telegram_id)
        )
        member = session.scalar(
            select(VIPMember).where(
                VIPMember.telegram_id == user_id, VIPMember.chat_id == self.chat_id
            )
        )
        if member is None:
            member = VIPMember(telegram_id=user_id, chat_id=self.chat_id, updated_at=self._now())
            session.add(member)
            session.flush()
        return member

    def _audit(self, session, action, entity_id):
        session.add(AuditEvent(action=action, entity_id=entity_id, occurred_at=self._now()))

    def _failure(self, member, error):
        member.state = "retry"
        member.attempts += 1
        member.last_error = error.code
        member.next_check_at = self._now() + max(
            error.retry_after, min(30 * 2 ** min(member.attempts, 6), 1800)
        )
        member.updated_at = self._now()

    def issue_invite(self, user_id: int) -> InviteTicket:
        self._enabled()
        with self.db.transaction() as session:
            if not self._entitlements(session, user_id):
                raise GroupError("vip_not_eligible")
            member = session.scalar(
                select(VIPMember).where(
                    VIPMember.telegram_id == user_id, VIPMember.chat_id == self.chat_id
                )
            )
            if member and member.last_error and member.next_check_at > self._now():
                raise GroupError(
                    "vip_retry_pending", retry_after=member.next_check_at - self._now()
                )
        try:
            bot_id = self._permissions()
        except GroupError as error:
            with self.db.transaction() as session:
                member = self._member(session, user_id)
                self._failure(member, error)
                self._audit(session, "vip_invite_failed", member.id)
            raise
        with self.db.transaction() as session:
            self._member(session, user_id)
            rights = self._entitlements(session, user_id)
            if not rights:
                raise GroupError("vip_not_eligible")
            now = self._now()
            previous = session.scalars(
                select(VIPInvite)
                .where(
                    VIPInvite.telegram_id == user_id,
                    VIPInvite.chat_id == self.chat_id,
                    VIPInvite.state.in_(["creating", "uncertain", "active", "approving"]),
                )
                .order_by(VIPInvite.created_at.desc())
            ).all()
            for invite in previous:
                if invite.expires_at <= now:
                    invite.state, invite.revoke_pending = "expired", invite.url is not None
                elif invite.url:
                    return InviteTicket(invite.url, invite.expires_at)
                else:
                    raise GroupError("invite_creation_pending")
            invite_id = uuid4().hex
            expires = min(
                now + self.settings.vip_invite_ttl_seconds,
                now + self.settings.vip_invite_ttl_seconds
                if any(right.expires_at is None for right in rights)
                else max(right.expires_at for right in rights),
            )
            invite = VIPInvite(
                id=invite_id,
                telegram_id=user_id,
                chat_id=self.chat_id,
                marker="vip_" + invite_id[:24],
                creator_id=bot_id,
                state="creating",
                created_at=now,
                expires_at=expires,
            )
            session.add(invite)
        # The reservation is durable before creating a remote link.
        error = None
        with self.db.transaction() as session:
            member = self._member(session, user_id)
            invite = session.get(VIPInvite, invite_id)
            try:
                rights = self._entitlements(session, user_id)
                if not rights or invite.expires_at <= self._now():
                    raise GroupError("vip_not_eligible")
                remote = self.gateway.call(GetChatMember(chat_id=self.chat_id, user_id=user_id))
                if remote.status in {"creator", "administrator"} or _present(remote):
                    invite.state = "revoked"
                    member.state = (
                        "protected" if remote.status in {"creator", "administrator"} else "active"
                    )
                    ticket = InviteTicket(None, invite.expires_at, "already_member")
                else:
                    if remote.status == "kicked":
                        if not member.removal_intent:
                            raise GroupError("vip_manual_ban_review")
                        self._true(
                            self.gateway.call(
                                UnbanChatMember(
                                    chat_id=self.chat_id, user_id=user_id, only_if_banned=True
                                )
                            )
                        )
                        member.removal_intent = False
                    if not self._entitlements(session, user_id) or invite.expires_at <= self._now():
                        raise GroupError("vip_not_eligible")
                    link = self.gateway.call(
                        CreateChatInviteLink(
                            chat_id=self.chat_id,
                            name=invite.marker,
                            expire_date=invite.expires_at,
                            creates_join_request=True,
                        )
                    )
                    if (
                        link.creator.id != bot_id
                        or not link.creates_join_request
                        or link.is_primary
                        or link.is_revoked
                        or not isinstance(link.invite_link, str)
                    ):
                        raise GroupError("telegram_invite_response_invalid")
                    invite.url, invite.url_hash = link.invite_link, _hash(link.invite_link)
                    if not self._entitlements(session, user_id) or invite.expires_at <= self._now():
                        invite.state, invite.revoke_pending = "expired", True
                        ticket = InviteTicket(None, invite.expires_at, "expired")
                    else:
                        invite.state = "active"
                        ticket = InviteTicket(invite.url, invite.expires_at)
                        self._audit(session, "vip_invite_issued", invite.id)
                member.next_check_at = self._now()
                cleanup_error = self._cleanup_invites(session, user_id)
                if cleanup_error:
                    self._failure(member, cleanup_error)
            except GroupError as caught:
                error = caught
                invite.state = "uncertain" if invite.url is None else "expired"
                invite.revoke_pending = invite.url is not None
                invite.last_error = caught.code
                self._failure(member, caught)
                self._audit(session, "vip_invite_failed", invite.id)
        if error:
            raise error
        return ticket

    @staticmethod
    def _true(result):
        if result is not True:
            raise GroupError("telegram_unexpected_response")

    def join_request(self, request: ChatJoinRequest) -> str:
        self._enabled()
        if request.chat.id != self.chat_id or request.from_user.is_bot:
            return "ignored"
        user_id, link = request.from_user.id, request.invite_link
        with self.db.transaction() as session:
            member = self._member(session, user_id)
            request_at = int(request.date.timestamp())
            if request_at < member.request_at:
                return "ignored"
            retry_at = (
                member.next_check_at
                if member.last_error and member.next_check_at > self._now()
                else None
            )
            invite = None
            if link is not None:
                invite = session.scalar(
                    select(VIPInvite).where(
                        VIPInvite.chat_id == self.chat_id,
                        VIPInvite.url_hash == _hash(link.invite_link),
                    )
                )
                if invite is None and link.name:
                    candidate = session.scalar(
                        select(VIPInvite).where(
                            VIPInvite.chat_id == self.chat_id, VIPInvite.marker == link.name
                        )
                    )
                    if (
                        candidate
                        and candidate.url is None
                        and candidate.telegram_id == user_id
                        and candidate.state in {"creating", "uncertain"}
                        and candidate.expires_at > self._now()
                        and candidate.creator_id == link.creator.id
                        and link.creates_join_request
                        and not link.is_primary
                    ):
                        # Recover a link whose create response/DB commit was lost.
                        candidate.url, candidate.url_hash = (
                            link.invite_link,
                            _hash(link.invite_link),
                        )
                        candidate.state = "active"
                        invite = candidate
            allowed = bool(
                invite
                and link
                and invite.telegram_id == user_id
                and invite.creator_id == link.creator.id
                and link.creates_join_request
                and not link.is_revoked
                and not link.is_primary
                and invite.state in {"active", "approving"}
                and invite.expires_at > self._now()
                and self._entitlements(session, user_id)
            )
            member.pending_action = "approve" if allowed else "decline"
            member.invite_id = invite.id if allowed else None
            member.request_at, member.next_check_at = request_at, retry_at or self._now()
            if allowed:
                invite.state = "approving"
            self._audit(session, "vip_join_requested", member.id)
        return "pending" if retry_at else self._work(user_id)

    def observe_member(self, user_id: int, chat_id: int):
        self._enabled()
        if chat_id != self.chat_id:
            return
        with self.db.transaction() as session:
            member = self._member(session, user_id)
            if not member.last_error or member.next_check_at <= self._now():
                member.next_check_at = self._now()

    def _prepare(self, user_id, owner=None):
        with self.db.transaction() as session:
            self._lease_guard(session, owner)
            member = self._member(session, user_id)
            if member.pending_action is None:
                member.pending_action = (
                    "check" if self._entitlements(session, user_id) else "remove"
                )

    def _work(self, user_id, owner=None):
        result = "pending"
        for _ in range(3):
            self._prepare(user_id, owner)
            again, result = self._execute(user_id, owner)
            if not again:
                break
        return result

    def _execute(self, user_id, owner=None):
        with self.db.transaction() as session:
            self._lease_guard(session, owner)
            member = self._member(session, user_id)
            try:
                bot_id = self._permissions()
                remote = self.gateway.call(GetChatMember(chat_id=self.chat_id, user_id=user_id))
                if user_id == bot_id or remote.status in {"administrator", "creator"}:
                    member.state, member.pending_action = "protected", None
                    member.removal_intent = False
                    member.next_check_at = self._now() + self.settings.vip_sync_interval_seconds
                    member.last_error, member.attempts = None, 0
                    for owned in session.scalars(
                        select(VIPInvite).where(
                            VIPInvite.telegram_id == user_id,
                            VIPInvite.chat_id == self.chat_id,
                            VIPInvite.state != "revoked",
                        )
                    ):
                        owned.revoke_pending = owned.url is not None
                    cleanup_error = self._cleanup_invites(session, user_id)
                    if cleanup_error:
                        self._failure(member, cleanup_error)
                    return False, "protected"
                rights = self._entitlements(session, user_id)
                present = _present(remote)
                invite = session.get(VIPInvite, member.invite_id) if member.invite_id else None
                if member.pending_action == "approve":
                    valid = bool(
                        rights
                        and invite
                        and invite.telegram_id == user_id
                        and invite.creator_id == bot_id
                        and invite.chat_id == self.chat_id
                        and invite.state == "approving"
                        and invite.expires_at > self._now()
                    )
                    if present and rights:
                        self._approved(session, member, invite, "vip_entry_recovered")
                    elif not valid:
                        member.pending_action = "remove" if present else "decline"
                        member.next_check_at = self._now()
                        return True, "pending"
                    else:
                        if remote.status == "kicked":
                            if not member.removal_intent:
                                raise GroupError("vip_manual_ban_review")
                            self._true(
                                self.gateway.call(
                                    UnbanChatMember(
                                        chat_id=self.chat_id, user_id=user_id, only_if_banned=True
                                    )
                                )
                            )
                        # Re-read the clock and financial proof immediately before approval.
                        if (
                            not self._entitlements(session, user_id)
                            or invite.expires_at <= self._now()
                        ):
                            member.pending_action = "decline"
                            return True, "pending"
                        self._true(
                            self.gateway.call(
                                ApproveChatJoinRequest(chat_id=self.chat_id, user_id=user_id)
                            )
                        )
                        self._approved(session, member, invite, "vip_join_approved")
                        if not self._entitlements(session, user_id):
                            member.pending_action = "remove"
                            member.next_check_at = self._now()
                            return True, "pending"
                    result = "approved"
                elif member.pending_action == "decline":
                    if present and not rights:
                        member.pending_action = "remove"
                        return True, "pending"
                    if not present:
                        self._true(
                            self.gateway.call(
                                DeclineChatJoinRequest(chat_id=self.chat_id, user_id=user_id)
                            )
                        )
                    member.state = "active" if present else "absent"
                    member.pending_action = None
                    self._audit(session, "vip_join_declined", member.id)
                    result = "declined"
                else:
                    if rights:
                        if remote.status == "kicked" and member.removal_intent:
                            self._true(
                                self.gateway.call(
                                    UnbanChatMember(
                                        chat_id=self.chat_id, user_id=user_id, only_if_banned=True
                                    )
                                )
                            )
                            present = False
                        member.state = "active" if present else "absent"
                        member.pending_action, member.removal_intent = None, False
                        result = member.state
                    elif present:
                        if member.pending_action != "remove":
                            member.pending_action = "remove"
                            return True, "pending"
                        if not member.removal_intent:
                            # Arm removal only after observing a non-admin member. A pre-existing
                            # moderator ban must not be mistaken for an interrupted bot removal.
                            member.removal_intent = True
                            return True, "pending"
                        # Renewal commits use the same user lock and cannot race this decision.
                        if self._entitlements(session, user_id):
                            return True, "pending"
                        self._true(
                            self.gateway.call(BanChatMember(chat_id=self.chat_id, user_id=user_id))
                        )
                        self._true(
                            self.gateway.call(
                                UnbanChatMember(
                                    chat_id=self.chat_id, user_id=user_id, only_if_banned=True
                                )
                            )
                        )
                        member.state, member.pending_action = "removed", None
                        member.removal_intent = False
                        self._audit(session, "vip_member_removed", member.id)
                        result = "removed"
                    else:
                        if remote.status == "kicked" and member.removal_intent:
                            self._true(
                                self.gateway.call(
                                    UnbanChatMember(
                                        chat_id=self.chat_id, user_id=user_id, only_if_banned=True
                                    )
                                )
                            )
                        member.state, member.pending_action = "absent", None
                        member.removal_intent = False
                        self._audit(session, "vip_already_absent", member.id)
                        result = "absent"
                cleanup_error = self._cleanup_invites(session, user_id)
                member.last_error, member.attempts = None, 0
                member.updated_at = self._now()
                next_expiry = min(
                    (right.expires_at for right in rights if right.expires_at is not None),
                    default=self._now() + 3600,
                )
                member.next_check_at = (
                    min(self._now() + self.settings.vip_sync_interval_seconds, next_expiry)
                    if rights
                    else self._now() + 3600
                )
                if cleanup_error:
                    self._failure(member, cleanup_error)
                return False, result
            except GroupError as error:
                self._failure(member, error)
                self._audit(session, "vip_sync_failed", member.id)
                return False, "pending"

    def _approved(self, session, member, invite, action):
        member.state, member.pending_action, member.removal_intent = "active", None, False
        if invite:
            invite.state, invite.consumed_at, invite.revoke_pending = "used", self._now(), True
        self._audit(session, action, member.id)

    def _cleanup_invites(self, session, user_id):
        invites = session.scalars(
            select(VIPInvite)
            .where(
                VIPInvite.chat_id == self.chat_id,
                VIPInvite.telegram_id == user_id,
                or_(
                    VIPInvite.state.not_in(["revoked", "expired"]),
                    VIPInvite.revoke_pending.is_(True),
                ),
            )
            .order_by(VIPInvite.created_at)
            .limit(10)
        ).all()
        rights = self._entitlements(session, user_id)
        failure = None
        for invite in invites:
            if invite.expires_at <= self._now():
                if invite.state != "expired":
                    self._audit(session, "vip_invite_expired", invite.id)
                invite.state, invite.revoke_pending, invite.last_error = "expired", False, None
                continue
            if not rights:
                invite.revoke_pending = invite.url is not None
                if invite.state != "used":
                    invite.state = "expired" if invite.url else "revoked"
            if invite.revoke_pending and invite.url:
                try:
                    revoked = self.gateway.call(
                        RevokeChatInviteLink(chat_id=self.chat_id, invite_link=invite.url)
                    )
                    if not revoked.is_revoked:
                        raise GroupError("telegram_unexpected_response")
                    invite.state, invite.revoke_pending, invite.last_error = "revoked", False, None
                    self._audit(session, "vip_invite_revoked", invite.id)
                except GroupError as error:
                    invite.last_error = error.code
                    failure = error
        return failure

    def _lease_guard(self, session, owner):
        if owner is None:
            return
        result = session.execute(
            update(VIPSyncLease)
            .where(VIPSyncLease.key == "vip-sync", VIPSyncLease.owner == owner)
            .values(expires_at=self._now() + 300)
        )
        if result.rowcount != 1:
            raise GroupError("vip_sync_lease_lost")

    def sync_once(self, stop=None):
        self._enabled()
        owner, now = uuid4().hex, self._now()
        with self.db.transaction() as session:
            insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
            session.execute(
                insert(VIPSyncLease)
                .values(key="vip-sync", owner="", expires_at=0)
                .on_conflict_do_nothing(index_elements=[VIPSyncLease.key])
            )
            claimed = session.execute(
                update(VIPSyncLease)
                .where(VIPSyncLease.key == "vip-sync", VIPSyncLease.expires_at <= now)
                .values(owner=owner, expires_at=now + 300)
            )
            if claimed.rowcount != 1:
                return {"busy": True, "processed": 0, "pending": 0}
            expire_subscriptions(session, now=now, chat_id=self.chat_id)
        processed, pending = 0, 0
        try:
            with self.db.transaction() as session:
                tracked = {
                    m.telegram_id: m
                    for m in session.scalars(
                        select(VIPMember).where(VIPMember.chat_id == self.chat_id)
                    )
                }
                due = {user_id: member.next_check_at for user_id, member in tracked.items()}
                for user_id in session.scalars(
                    select(Subscription.telegram_id)
                    .where(Subscription.telegram_group_id == self.chat_id)
                    .distinct()
                ):
                    due.setdefault(user_id, 0)
                for user_id in session.scalars(
                    select(Order.linked_telegram_id)
                    .join(AccessGrant, AccessGrant.order_id == Order.id)
                    .where(
                        AccessGrant.telegram_group_id == self.chat_id,
                        Order.linked_telegram_id.is_not(None),
                    )
                    .distinct()
                ):
                    due.setdefault(user_id, 0)
                for invite in session.scalars(
                    select(VIPInvite).where(
                        VIPInvite.chat_id == self.chat_id, VIPInvite.state != "revoked"
                    )
                ):
                    member = tracked.get(invite.telegram_id)
                    if member and member.last_error and member.next_check_at > now:
                        continue
                    if (
                        invite.revoke_pending
                        or invite.state != "expired"
                        and invite.expires_at <= now
                        or invite.state == "approving"
                    ):
                        due[invite.telegram_id] = min(due.get(invite.telegram_id, 0), now)
                candidates = [
                    user_id
                    for user_id, when in sorted(due.items(), key=lambda item: item[1])
                    if when <= now
                ][:100]
            for user_id in candidates:
                if stop is not None and stop.is_set():
                    break
                result = self._work(user_id, owner)
                processed += 1
                pending += result == "pending"
            return {"busy": False, "processed": processed, "pending": pending}
        finally:
            with self.db.transaction() as session:
                session.execute(
                    update(VIPSyncLease)
                    .where(VIPSyncLease.key == "vip-sync", VIPSyncLease.owner == owner)
                    .values(expires_at=0)
                )

    def operational_status(self):
        with self.db.transaction() as session:
            members = list(
                session.scalars(select(VIPMember).where(VIPMember.chat_id == self.chat_id))
            )
            return {
                "enabled": self.settings.vip_group_enabled,
                "tracked": len(members),
                "pending": sum(m.pending_action is not None for m in members),
                "failures": sum(m.last_error is not None for m in members),
            }

    def check_permissions(self):
        self._permissions()
        return True
