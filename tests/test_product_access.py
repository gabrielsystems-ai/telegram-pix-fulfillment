import pytest
from aiogram.methods import BanChatMember
from sqlalchemy import select

from tests.test_catalog import product_order, setup_catalog
from tests.test_domain import NOW
from tests.test_groups import Scenario
from vip.entitlements import product_accesses
from vip.errors import DomainError
from vip.linking import issue_link_token, redeem_link_token
from vip.models import AccessGrant, BuyerAssurance, Order, RecoveryTask, Subscription
from vip.payments.contracts import VerifiedPayment
from vip.payments.operations import OmegaPayments
from vip.payments.service import record_verified_payment
from vip.users import register_user


def settle(db, oid):
    with db.transaction() as session:
        order = session.get(Order, oid)
        order.provider_transaction_id = "synthetic-" + oid
        event = VerifiedPayment(
            "event-" + oid, order.provider_transaction_id, oid, order.amount_minor, "BRL", "paid"
        )
        assert record_verified_payment(session, event, now=NOW + 1202)
        assert not record_verified_payment(session, event, now=NOW + 1202)
        return issue_link_token(session, oid, ttl_seconds=900, now=NOW + 1202)


def bind(db, raw, user_id=123):
    with db.transaction() as session:
        register_user(session, user_id)
        return redeem_link_token(session, raw, user_id, now=NOW + 1203)


@pytest.mark.parametrize(
    "code,permissions",
    [
        ("sample_video", {"sample_video_access"}),
        ("vip_permanent", {"vip_permanent_access"}),
        ("recovery_bundle", {"sample_video_access", "vip_permanent_access"}),
    ],
)
def test_one_payment_grants_only_purchased_permissions_permanently(db, code, permissions):
    setup_catalog(db)
    if code == "recovery_bundle":
        with db.transaction() as session:
            session.add(
                RecoveryTask(
                    telegram_id=123,
                    first_started_at=NOW,
                    due_at=NOW + 1200,
                    next_check_at=NOW + 1200,
                    eligible_at=NOW + 1200,
                    state="eligible",
                )
            )
    oid = product_order(db, code)
    with db.transaction() as session:
        assert product_accesses(session, 123) == []
    raw = settle(db, oid)
    with db.transaction() as session:
        assert product_accesses(session, 123) == []  # Must still bind the actual buyer.
    bind(db, raw)
    with db.transaction() as session:
        assert {a.permission for a in product_accesses(session, 123)} == permissions
        assert product_accesses(session, 456) == []
        assert session.scalar(select(Subscription)) is None
        assert len(list(session.scalars(select(AccessGrant)))) == len(permissions)
    s = Scenario(db)
    s.now = NOW + 1000 * 86400
    if "vip_permanent_access" in permissions:
        ticket = s.service.issue_invite(123)
        assert ticket.expires_at == s.now + 120
        s.service.join_request(s.request(123, ticket.url))
        s.now += 1000 * 86400
        s.service.sync_once()
        assert s.telegram.count(BanChatMember) == 0
    else:
        with pytest.raises(Exception, match="vip_not_eligible"):
            s.service.issue_invite(123)


def test_product_token_cannot_be_redeemed_by_a_different_telegram_user(db):
    setup_catalog(db)
    raw = settle(db, product_order(db))
    with pytest.raises(DomainError):
        bind(db, raw, 456)
    bind(db, raw)


def test_revoking_one_order_keeps_other_independently_purchased_access(db):
    setup_catalog(db)
    first = product_order(db, "vip_permanent")
    second = product_order(db, "vip_permanent", "request-00000002")
    bind(db, settle(db, first))
    bind(db, settle(db, second))
    with db.transaction() as session:
        order = session.get(Order, first)
        order.status = "cancelled"
        OmegaPayments._revoke_internal(session, order, NOW + 1204)
        assert [a.order_id for a in product_accesses(session, 123)] == [second]


@pytest.mark.parametrize("historical_status", ["absent", "unverified", "verified"])
def test_historical_age_records_do_not_affect_legitimate_product_access(db, historical_status):
    setup_catalog(db)
    if historical_status != "absent":
        with db.transaction() as session:
            session.add(
                BuyerAssurance(
                    telegram_id=123,
                    declared_at=NOW,
                    verified_at=NOW if historical_status == "verified" else None,
                    verification_reference="historical-record",
                )
            )
    bind(db, settle(db, product_order(db)))
    with db.transaction() as session:
        assert {access.permission for access in product_accesses(session, 123)} == {
            "sample_video_access"
        }
        assert product_accesses(session, 456) == []
        if historical_status != "absent":
            assert session.get(BuyerAssurance, 123).verification_reference == "historical-record"


def test_catalog_installation_preserves_legacy_vip_without_age_assurance(db):
    s = Scenario(db)
    s.paid()
    s.telegram.members[123] = "member"
    with db.transaction() as session:
        subscription = session.scalar(select(Subscription))
        before = (subscription.id, subscription.status, subscription.expires_at)
    setup_catalog(db)
    s.service.sync_once()
    assert s.telegram.count(BanChatMember) == 0
    assert s.service.issue_invite(123).status == "already_member"
    with db.transaction() as session:
        subscription = session.scalar(select(Subscription))
        assert (subscription.id, subscription.status, subscription.expires_at) == before
