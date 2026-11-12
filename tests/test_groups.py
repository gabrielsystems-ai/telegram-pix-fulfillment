from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
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
from aiogram.types import Chat, ChatInviteLink, ChatJoinRequest, User
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from tests.test_domain import NOW, pay, purchase, redeem, token
from vip.config import Settings
from vip.groups import GroupError, VIPAccess
from vip.models import VIPInvite, VIPMember, VIPSyncLease

CHAT = -100123


class TelegramDouble:
    def __init__(self):
        self.bot_user = User(id=999, first_name="Synthetic Bot", is_bot=True)
        self.calls = []
        self.members = {}
        self.invites = {}
        self.invite_right = True
        self.restrict_right = True
        self.bot_status = "administrator"
        self.fail = None
        self.hook = None
        self.private = True
        self.lose_response = None

    def result(self, method, value):
        if self.lose_response is not None and isinstance(method, self.lose_response):
            self.lose_response = None
            raise GroupError("telegram_unavailable")
        return value

    def call(self, method):
        self.calls.append(method)
        if self.hook:
            self.hook(method)
        if self.fail is not None and isinstance(method, self.fail):
            raise GroupError("telegram_unavailable")
        if isinstance(method, GetMe):
            return self.bot_user
        if isinstance(method, GetChat):
            return SimpleNamespace(
                id=CHAT, type="supergroup", username=None if self.private else "public"
            )
        if isinstance(method, GetChatMember):
            if method.user_id == 999:
                return SimpleNamespace(
                    status=self.bot_status,
                    can_invite_users=self.invite_right,
                    can_restrict_members=self.restrict_right,
                )
            return SimpleNamespace(status=self.members.get(method.user_id, "left"), is_member=True)
        if isinstance(method, CreateChatInviteLink):
            assert method.creates_join_request is True
            assert method.member_limit is None
            link = ChatInviteLink(
                invite_link=f"https://t.me/+synthetic-{method.name}",
                creator=self.bot_user,
                creates_join_request=True,
                is_primary=False,
                is_revoked=False,
                name=method.name,
                expire_date=method.expire_date,
            )
            self.invites[link.invite_link] = link
            return self.result(method, link)
        if isinstance(method, RevokeChatInviteLink):
            link = self.invites[method.invite_link].model_copy(update={"is_revoked": True})
            self.invites[link.invite_link] = link
            return link
        if isinstance(method, ApproveChatJoinRequest):
            self.members[method.user_id] = "member"
            return self.result(method, True)
        if isinstance(method, DeclineChatJoinRequest):
            return True
        if isinstance(method, BanChatMember):
            assert self.members.get(method.user_id) not in {"administrator", "creator"}
            self.members[method.user_id] = "kicked"
            return self.result(method, True)
        if isinstance(method, UnbanChatMember):
            assert method.only_if_banned is True
            if self.members.get(method.user_id) == "kicked":
                self.members[method.user_id] = "left"
            return self.result(method, True)
        raise AssertionError(type(method).__name__)

    def count(self, method_type):
        return sum(isinstance(method, method_type) for method in self.calls)


class Scenario:
    def __init__(self, db):
        self.db = db
        self.now = NOW + 10
        self.telegram = TelegramDouble()
        self.settings = Settings(
            _env_file=None,
            vip_group_enabled=True,
            vip_chat_id=CHAT,
            vip_sync_interval_seconds=10,
            vip_invite_ttl_seconds=120,
        )
        self.service = self.restarted()

    def restarted(self):
        return VIPAccess(self.settings, self.db, self.telegram, clock=lambda: self.now)

    def paid(self, user_id=123):
        order_id, _ = purchase(self.db)
        pay(self.db, order_id, event_id="event-" + order_id)
        redeem(self.db, token(self.db, order_id), telegram_id=user_id)
        return order_id

    def request(self, user_id, link):
        return ChatJoinRequest(
            chat=Chat(id=CHAT, type="supergroup"),
            from_user=User(id=user_id, first_name="Synthetic", is_bot=False),
            user_chat_id=user_id,
            date=datetime.fromtimestamp(self.now, UTC),
            invite_link=self.telegram.invites.get(link) if link else None,
        )


@pytest.fixture
def s(db):
    return Scenario(db)


def test_unpaid_or_unlinked_user_cannot_receive_invite(s):
    purchase(s.db)
    with pytest.raises(GroupError, match="vip_not_eligible"):
        s.service.issue_invite(123)
    assert s.telegram.count(CreateChatInviteLink) == 0


def test_controlled_invite_is_short_and_reused_only_by_its_owner(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    assert ticket.url.startswith("https://t.me/")
    assert ticket.expires_at == NOW + 130
    assert s.service.issue_invite(123).url == ticket.url
    assert s.telegram.count(CreateChatInviteLink) == 1
    with s.db.transaction() as session:
        invite = session.scalar(select(VIPInvite))
        assert invite.telegram_id == 123
        assert ticket.url not in repr(ticket)


def test_shared_invite_is_declined_for_a_third_party_but_owner_can_join(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    assert s.service.join_request(s.request(456, ticket.url)) == "declined"
    assert s.telegram.count(ApproveChatJoinRequest) == 0
    assert s.service.join_request(s.request(123, ticket.url)) == "approved"
    assert s.telegram.members[123] == "member"
    assert s.telegram.count(ApproveChatJoinRequest) == 1
    assert s.telegram.count(RevokeChatInviteLink) == 1


def test_join_without_identifiable_invite_is_declined_even_for_paid_user(s):
    s.paid()
    assert s.service.join_request(s.request(123, None)) == "declined"


def test_duplicate_join_never_approves_twice(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    request = s.request(123, ticket.url)
    s.service.join_request(request)
    s.service.join_request(request)
    assert s.telegram.count(ApproveChatJoinRequest) == 1


def test_invite_expiry_does_not_expire_the_paid_subscription(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    s.now = ticket.expires_at
    assert s.service.join_request(s.request(123, ticket.url)) == "declined"
    assert s.service.issue_invite(123).url != ticket.url


@pytest.mark.parametrize("status", ["administrator", "creator"])
def test_administrators_and_owner_are_never_removed(s, status):
    s.paid()
    s.telegram.members[123] = status
    s.now = NOW + 31 * 86400
    s.service.sync_once()
    assert s.telegram.count(BanChatMember) == 0
    assert s.telegram.count(UnbanChatMember) == 0


def test_expiration_removes_member_then_unbans_for_future_renewal(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    s.service.join_request(s.request(123, ticket.url))
    s.now = NOW + 31 * 86400
    s.service.sync_once()
    assert s.telegram.members[123] == "left"
    assert s.telegram.count(BanChatMember) == 1
    assert s.telegram.count(UnbanChatMember) == 1


def test_member_that_already_left_is_not_banned(s):
    s.paid()
    s.now = NOW + 31 * 86400
    s.service.sync_once()
    assert s.telegram.count(BanChatMember) == 0


def test_removal_failure_is_persisted_and_retried_after_restart(s):
    s.paid()
    s.telegram.members[123] = "member"
    s.telegram.fail = BanChatMember
    s.now = NOW + 31 * 86400
    s.service.sync_once()
    with s.db.transaction() as session:
        member = session.scalar(select(VIPMember).where(VIPMember.telegram_id == 123))
        assert member.pending_action == "remove"
        assert member.last_error == "telegram_unavailable"
    s.telegram.fail = None
    s.now += 3600
    s.restarted().sync_once()
    assert s.telegram.members[123] == "left"


def test_renewal_keeps_member_when_an_older_subscription_expires(s):
    s.paid()
    s.paid()
    s.telegram.members[123] = "member"
    s.now = NOW + 31 * 86400
    s.service.sync_once()
    assert s.telegram.count(BanChatMember) == 0


def test_two_simultaneous_invite_requests_do_not_create_two_links(s):
    s.paid()

    def issue(_):
        try:
            return s.service.issue_invite(123)
        except GroupError as error:
            assert error.code == "invite_creation_pending"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(issue, range(2)))
    assert s.telegram.count(CreateChatInviteLink) == 1


def test_expiration_during_permission_or_member_lookup_cannot_be_approved(s):
    s.paid()
    ticket = s.service.issue_invite(123)

    def delay(method):
        if isinstance(method, GetChatMember) and method.user_id == 123:
            s.now = NOW + 31 * 86400

    s.telegram.hook = delay
    s.service.join_request(s.request(123, ticket.url))
    assert s.telegram.count(ApproveChatJoinRequest) == 0


def test_missing_admin_permissions_prevent_any_invite_or_approval(s):
    s.paid()
    s.telegram.invite_right = False
    with pytest.raises(GroupError):
        s.service.issue_invite(123)
    assert s.telegram.count(CreateChatInviteLink) == 0


def test_disabled_or_wrong_chat_never_changes_a_telegram_group(s):
    s.paid()
    disabled = VIPAccess(Settings(_env_file=None), s.db, s.telegram, clock=lambda: s.now)
    with pytest.raises(GroupError, match="vip_group_disabled"):
        disabled.issue_invite(123)
    request = s.request(123, None).model_copy(update={"chat": Chat(id=-999, type="supergroup")})
    assert s.service.join_request(request) == "ignored"
    assert s.telegram.calls == []


def test_live_worker_lease_prevents_another_cycle(s):
    with s.db.transaction() as session:
        session.add(VIPSyncLease(key="vip-sync", owner="other", expires_at=s.now + 60))
    assert s.service.sync_once()["busy"] is True
    assert s.telegram.calls == []


def test_existing_moderator_ban_is_not_undone_by_expiration_sync(s):
    s.paid()
    s.telegram.members[123] = "kicked"
    s.now = NOW + 31 * 86400
    s.service.sync_once()
    assert s.telegram.count(UnbanChatMember) == 0


def test_pending_join_retry_respects_persisted_backoff(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    s.telegram.fail = ApproveChatJoinRequest
    assert s.service.join_request(s.request(123, ticket.url)) == "pending"
    calls = s.telegram.count(ApproveChatJoinRequest)
    s.now += 1
    s.service.sync_once()
    assert s.telegram.count(ApproveChatJoinRequest) == calls


def test_successful_approval_with_lost_response_is_recovered_without_approving_twice(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    s.telegram.lose_response = ApproveChatJoinRequest
    assert s.service.join_request(s.request(123, ticket.url)) == "pending"
    assert s.telegram.members[123] == "member"
    s.now += 61
    s.restarted().sync_once()
    assert s.telegram.count(ApproveChatJoinRequest) == 1
    with s.db.transaction() as session:
        member = session.scalar(select(VIPMember).where(VIPMember.telegram_id == 123))
        assert member.pending_action is None
        assert member.state == "active"


def test_create_link_lost_response_can_be_recovered_from_own_join_request(s):
    s.paid()
    s.telegram.lose_response = CreateChatInviteLink
    with pytest.raises(GroupError):
        s.service.issue_invite(123)
    url = next(iter(s.telegram.invites))
    assert s.restarted().join_request(s.request(123, url)) == "pending"
    s.now += 61
    s.restarted().sync_once()
    assert s.telegram.members[123] == "member"
    assert s.telegram.count(CreateChatInviteLink) == 1


def test_lost_ban_response_is_recovered_by_unban_without_second_ban(s):
    s.paid()
    s.telegram.members[123] = "member"
    s.telegram.lose_response = BanChatMember
    s.now = NOW + 31 * 86400
    s.service.sync_once()
    assert s.telegram.members[123] == "kicked"
    s.now += 61
    s.restarted().sync_once()
    assert s.telegram.members[123] == "left"
    assert s.telegram.count(BanChatMember) == 1


def test_another_paid_user_still_cannot_use_someone_elses_link(s):
    s.paid(123)
    s.paid(456)
    ticket = s.service.issue_invite(123)
    assert s.service.join_request(s.request(456, ticket.url)) == "declined"
    assert s.telegram.count(ApproveChatJoinRequest) == 0


def test_eligibility_loss_during_approval_is_compensated_by_removal(s):
    s.paid()
    ticket = s.service.issue_invite(123)

    def expire(method):
        if isinstance(method, ApproveChatJoinRequest):
            s.now = NOW + 31 * 86400

    s.telegram.hook = expire
    s.service.join_request(s.request(123, ticket.url))
    assert s.telegram.members[123] == "left"
    assert s.telegram.count(BanChatMember) == 1


def test_public_group_is_rejected_before_invite_creation(s):
    s.paid()
    s.telegram.private = False
    with pytest.raises(GroupError, match="vip_private_supergroup_required"):
        s.service.issue_invite(123)
    assert s.telegram.count(CreateChatInviteLink) == 0


def test_expired_worker_lease_is_reclaimed_after_restart(s):
    s.paid()
    with s.db.transaction() as session:
        session.add(VIPSyncLease(key="vip-sync", owner="crashed", expires_at=s.now - 1))
    assert s.restarted().sync_once()["busy"] is False


def test_membership_events_do_not_bypass_retry_after_a_telegram_failure(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    s.telegram.fail = ApproveChatJoinRequest
    s.service.join_request(s.request(123, ticket.url))
    count = s.telegram.count(ApproveChatJoinRequest)
    s.now += 1
    s.service.observe_member(123, CHAT)
    s.service.sync_once()
    assert s.telegram.count(ApproveChatJoinRequest) == count


def test_repeated_join_requests_do_not_bypass_persisted_backoff(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    s.telegram.fail = ApproveChatJoinRequest
    request = s.request(123, ticket.url)
    s.service.join_request(request)
    count = s.telegram.count(ApproveChatJoinRequest)
    s.now += 1
    s.service.join_request(request)
    assert s.telegram.count(ApproveChatJoinRequest) == count


def test_expired_links_finish_cleanup_without_repeated_revoke_errors(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    s.telegram.fail = RevokeChatInviteLink
    s.service.join_request(s.request(123, ticket.url))
    count = s.telegram.count(RevokeChatInviteLink)
    s.now = ticket.expires_at + 1
    s.restarted().sync_once()
    assert s.telegram.count(RevokeChatInviteLink) == count
    with s.db.transaction() as session:
        invite = session.scalar(select(VIPInvite))
        assert invite.revoke_pending is False
        assert invite.last_error is None


def test_concurrent_join_updates_approve_only_once(s):
    s.paid()
    ticket = s.service.issue_invite(123)
    request = s.request(123, ticket.url)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: s.service.join_request(request), range(2)))
    assert s.telegram.count(ApproveChatJoinRequest) == 1
    assert s.telegram.members[123] == "member"


def test_remote_approval_survives_local_transaction_rollback(s, monkeypatch):
    s.paid()
    ticket = s.service.issue_invite(123)
    transaction = s.db.transaction
    failed = False

    @contextmanager
    def fail_after_approval():
        nonlocal failed
        with transaction() as session:
            yield session
            if not failed and s.telegram.count(ApproveChatJoinRequest):
                failed = True
                raise OperationalError("synthetic commit failure", None, None)

    monkeypatch.setattr(s.db, "transaction", fail_after_approval)
    with pytest.raises(OperationalError):
        s.service.join_request(s.request(123, ticket.url))
    assert s.telegram.members[123] == "member"
    monkeypatch.setattr(s.db, "transaction", transaction)
    s.restarted().sync_once()
    assert s.telegram.count(ApproveChatJoinRequest) == 1
    with s.db.transaction() as session:
        member = session.scalar(select(VIPMember).where(VIPMember.telegram_id == 123))
        assert member.pending_action is None
        assert member.state == "active"


def test_repeated_invite_commands_respect_persisted_failure_backoff(s):
    s.paid()
    s.telegram.invite_right = False
    with pytest.raises(GroupError):
        s.service.issue_invite(123)
    calls = len(s.telegram.calls)
    s.telegram.invite_right = True
    s.now += 1
    with pytest.raises(GroupError, match="vip_retry_pending"):
        s.restarted().issue_invite(123)
    assert len(s.telegram.calls) == calls
    s.now += 61
    assert s.restarted().issue_invite(123).url
