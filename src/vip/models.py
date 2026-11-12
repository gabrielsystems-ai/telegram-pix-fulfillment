from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class BotMediaCache(Base):
    """Disposable Telegram media references; never an entitlement or payment."""

    __tablename__ = "bot_media_cache"
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[str] = mapped_column(Text)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("telegram_id > 0"),)

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[int] = mapped_column(BigInteger)


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("duration_days > 0"),
        CheckConstraint("currency = 'BRL'"),
        CheckConstraint("telegram_group_id < 0"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="BRL")
    duration_days: Mapped[int] = mapped_column(Integer)
    telegram_group_id: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("duration_days > 0"),
        CheckConstraint("currency = 'BRL'"),
        CheckConstraint("status IN ('pending', 'paid', 'cancelled', 'expired')"),
        CheckConstraint(
            "(plan_id IS NOT NULL AND product_id IS NULL) OR "
            "(plan_id IS NULL AND product_id IS NOT NULL AND duration_days IS NULL)",
            name="ck_orders_kind",
        ),
        UniqueConstraint("sales_request_id", name="uq_orders_sales_request"),
        UniqueConstraint("combo_buyer_id", name="uq_orders_combo_buyer"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    payment_provider: Mapped[str] = mapped_column(
        String(16), default="omega", server_default="omega"
    )
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("plans.id"))
    product_id: Mapped[str | None] = mapped_column(
        ForeignKey("products.code", name="fk_orders_product")
    )
    product_name: Mapped[str | None] = mapped_column(String(100))
    buyer_telegram_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.telegram_id", name="fk_orders_buyer"), index=True
    )
    sales_request_id: Mapped[str | None] = mapped_column(String(64))
    combo_buyer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.telegram_id", name="fk_orders_combo_buyer")
    )
    origin: Mapped[str] = mapped_column(String(16), default="standard", server_default="standard")
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    duration_days: Mapped[int | None] = mapped_column(Integer)
    telegram_group_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    paid_at: Mapped[int | None] = mapped_column(BigInteger)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    linked_telegram_id: Mapped[int | None] = mapped_column(ForeignKey("users.telegram_id"))


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount_minor > 0"),)

    transaction_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    confirmed_at: Mapped[int] = mapped_column(BigInteger)


class PaymentWindowRenewal(Base):
    """Append-only evidence for resuming the same authenticated, unpaid Pix."""

    __tablename__ = "payment_window_renewals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    transaction_id: Mapped[str] = mapped_column(String(36))
    previous_expires_at: Mapped[int] = mapped_column(BigInteger)
    new_expires_at: Mapped[int] = mapped_column(BigInteger)
    checked_at: Mapped[int] = mapped_column(BigInteger)
    provider_expiration: Mapped[str] = mapped_column(String(100))
    provider_expires_at: Mapped[int] = mapped_column(BigInteger)


class PixReplacement(Base):
    """Explicit attempt replacement, never a provider cancellation."""

    __tablename__ = "pix_replacements"
    old_order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    new_order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    root_order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    settled_order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    replaced_at: Mapped[int] = mapped_column(BigInteger)


class BotPurchase(Base):
    """One resumable purchase per buyer/product; selection is presentation state."""

    __tablename__ = "bot_purchases"
    __table_args__ = (
        CheckConstraint("stage IN ('buyer', 'payment')", name="bot_purchases_stage_check"),
    )
    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), primary_key=True)
    product_code: Mapped[str] = mapped_column(
        ForeignKey("products.code", name="fk_bot_purchases_product"), primary_key=True
    )
    selection_order: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    stage: Mapped[str] = mapped_column(String(16), default="buyer")
    pix_message_id: Mapped[int | None] = mapped_column(BigInteger)
    pix_file_id: Mapped[str | None] = mapped_column(Text)
    pix_code_hash: Mapped[str | None] = mapped_column(String(64))


class WebPurchase(Base):
    """One-use Login nonce bound to a product/request before a buyer is known."""

    __tablename__ = "web_purchases"
    nonce_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_code: Mapped[str] = mapped_column(ForeignKey("products.code"))
    sales_request_id: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), unique=True)


class Fulfillment(Base):
    """Only purchases with a server-verified Telegram identity enter this outbox."""

    __tablename__ = "fulfillments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting_payment', 'queued', 'processing', "
            "'complete', 'review', 'cancelled')"
        ),
        CheckConstraint("video_state IN ('not_required', 'pending', 'sent', 'retry', 'uncertain')"),
        CheckConstraint(
            "vip_state IN ('not_required', 'pending', 'sending', 'sent', 'retry', 'uncertain')"
        ),
    )
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    authenticated_user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    verified_at: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="waiting_payment")
    video_state: Mapped[str] = mapped_column(String(16))
    vip_state: Mapped[str] = mapped_column(String(16))
    next_check_at: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(64))


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("payments.transaction_id"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    received_at: Mapped[int] = mapped_column(BigInteger)


class LinkToken(Base):
    __tablename__ = "link_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    consumed_at: Mapped[int | None] = mapped_column(BigInteger)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("expires_at > starts_at"),
        CheckConstraint("status IN ('active', 'expired')"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    telegram_group_id: Mapped[int] = mapped_column(BigInteger)
    starts_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    action: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[int] = mapped_column(BigInteger)


class OmegaCharge(Base):
    __tablename__ = "omega_charges"
    __table_args__ = (CheckConstraint("state IN ('creating', 'uncertain', 'ready')"),)

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    identifier: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[int] = mapped_column(BigInteger)
    financial_status: Mapped[str | None] = mapped_column(String(32))
    copy_paste: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[int | None] = mapped_column(BigInteger)
    webhook_token_hash: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[int | None] = mapped_column(BigInteger)
    next_reconcile_at: Mapped[int] = mapped_column(BigInteger, default=0)


class PushinCharge(Base):
    __tablename__ = "pushin_charges"
    __table_args__ = (CheckConstraint("state IN ('creating', 'uncertain', 'ready')"),)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    transaction_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[int] = mapped_column(BigInteger)
    copy_paste: Mapped[str | None] = mapped_column(Text)
    financial_status: Mapped[str | None] = mapped_column(String(16))
    expires_at: Mapped[int | None] = mapped_column(BigInteger)


class PushinNotification(Base):
    """Authenticated signals only; unassociated IDs can arrive before POST commits."""

    __tablename__ = "pushin_notifications"
    transaction_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    processed_version: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[int] = mapped_column(BigInteger, default=0)
    payment_hint: Mapped[bool] = mapped_column(Boolean, default=False)


class PushinQueryGate(Base):
    __tablename__ = "pushin_query_gates"
    account_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_query_at: Mapped[int] = mapped_column(BigInteger, default=0)


class OmegaEvent(Base):
    __tablename__ = "omega_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("omega_charges.order_id"))
    name: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[int] = mapped_column(BigInteger)


class VIPInvite(Base):
    __tablename__ = "vip_invites"
    __table_args__ = (
        CheckConstraint(
            "state IN ('creating', 'uncertain', 'active', 'approving', "
            "'used', 'expired', 'revoked')"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    marker: Mapped[str] = mapped_column(String(32), unique=True)
    creator_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16))
    url: Mapped[str | None] = mapped_column(Text)
    url_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    consumed_at: Mapped[int | None] = mapped_column(BigInteger)
    revoke_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(String(64))


class VIPMember(Base):
    __tablename__ = "vip_members"
    __table_args__ = (UniqueConstraint("chat_id", "telegram_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), default="unknown")
    pending_action: Mapped[str | None] = mapped_column(String(16))
    invite_id: Mapped[str | None] = mapped_column(ForeignKey("vip_invites.id"))
    request_at: Mapped[int] = mapped_column(BigInteger, default=0)
    removal_intent: Mapped[bool] = mapped_column(Boolean, default=False)
    next_check_at: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(64))


class VIPSyncLease(Base):
    __tablename__ = "vip_sync_leases"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[int] = mapped_column(BigInteger)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("amount_minor > 0"),)

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    amount_minor: Mapped[int] = mapped_column(Integer)
    telegram_group_id: Mapped[int | None] = mapped_column(BigInteger)
    recovery_only: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    rights_reference: Mapped[str | None] = mapped_column(String(100))


class BuyerAssurance(Base):
    """Historical metadata retained for compatibility; never used for authorization."""

    __tablename__ = "buyer_assurances"

    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), primary_key=True)
    declared_at: Mapped[int | None] = mapped_column(BigInteger)
    verified_at: Mapped[int | None] = mapped_column(BigInteger)
    verification_reference: Mapped[str | None] = mapped_column(String(100))


class AccessGrant(Base):
    __tablename__ = "access_grants"
    __table_args__ = (
        UniqueConstraint("order_id", "permission"),
        CheckConstraint("permission IN ('sample_video_access', 'vip_permanent_access')"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    permission: Mapped[str] = mapped_column(String(32))
    telegram_group_id: Mapped[int | None] = mapped_column(BigInteger)
    granted_at: Mapped[int | None] = mapped_column(BigInteger)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger)


class RecoveryTask(Base):
    __tablename__ = "recovery_tasks"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'eligible', 'sending', 'sent', 'skipped', "
            "'canceled', 'failed', 'uncertain')"
        ),
    )

    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), primary_key=True)
    first_started_at: Mapped[int] = mapped_column(BigInteger)
    due_at: Mapped[int] = mapped_column(BigInteger)
    next_check_at: Mapped[int] = mapped_column(BigInteger, index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    eligible_at: Mapped[int | None] = mapped_column(BigInteger)
    opted_in_at: Mapped[int | None] = mapped_column(BigInteger)
    opted_out_at: Mapped[int | None] = mapped_column(BigInteger)
    blocked_at: Mapped[int | None] = mapped_column(BigInteger)
    send_attempt_at: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(64))
