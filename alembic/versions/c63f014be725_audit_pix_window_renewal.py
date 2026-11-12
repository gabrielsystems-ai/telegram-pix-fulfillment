"""Record authenticated renewal of an existing Pix payment window."""

import sqlalchemy as sa

from alembic import op

revision = "c63f014be725"
down_revision = "b52e903ad614"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payment_window_renewals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("order_id", sa.String(32), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("transaction_id", sa.String(36), nullable=False),
        sa.Column("previous_expires_at", sa.BigInteger(), nullable=False),
        sa.Column("new_expires_at", sa.BigInteger(), nullable=False),
        sa.Column("checked_at", sa.BigInteger(), nullable=False),
        sa.Column("provider_expiration", sa.String(100), nullable=False),
        sa.Column("provider_expires_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_payment_window_renewals_order_id", "payment_window_renewals", ["order_id"])


def downgrade():
    op.drop_index("ix_payment_window_renewals_order_id", table_name="payment_window_renewals")
    op.drop_table("payment_window_renewals")
