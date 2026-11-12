"""Persist verified web purchase identity and durable delivery progress.

Additive only: no backfill, no automatic association of historical orders.
"""

import sqlalchemy as sa

from alembic import op

revision = "e28f104a6209"
down_revision = "d15ab28c701e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "web_purchases",
        sa.Column("nonce_hash", sa.String(64), primary_key=True),
        sa.Column("product_code", sa.String(32), sa.ForeignKey("products.code"), nullable=False),
        sa.Column("sales_request_id", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.String(32), sa.ForeignKey("orders.id"), unique=True),
    )
    op.create_table(
        "fulfillments",
        sa.Column("order_id", sa.String(32), sa.ForeignKey("orders.id"), primary_key=True),
        sa.Column(
            "authenticated_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("video_state", sa.String(16), nullable=False),
        sa.Column("vip_state", sa.String(16), nullable=False),
        sa.Column("next_check_at", sa.BigInteger(), nullable=False),
        sa.Column("lease_owner", sa.String(32)),
        sa.Column("lease_until", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(64)),
        sa.CheckConstraint(
            "status IN ('waiting_payment', 'queued', 'processing', "
            "'complete', 'review', 'cancelled')"
        ),
        sa.CheckConstraint(
            "video_state IN ('not_required', 'pending', 'sent', 'retry', 'uncertain')"
        ),
        sa.CheckConstraint(
            "vip_state IN ('not_required', 'pending', 'sending', 'sent', 'retry', 'uncertain')"
        ),
    )
    op.create_index("ix_fulfillments_next_check_at", "fulfillments", ["next_check_at"])


def downgrade():
    op.drop_index("ix_fulfillments_next_check_at", table_name="fulfillments")
    op.drop_table("fulfillments")
    op.drop_table("web_purchases")
