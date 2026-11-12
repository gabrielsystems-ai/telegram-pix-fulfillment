"""Add Pushin Pay without rewriting Omega orders or payment history."""

import sqlalchemy as sa

from alembic import op

revision = "a41d7e8c902b"
down_revision = "f39c62e7a810"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "orders",
        sa.Column("payment_provider", sa.String(16), nullable=False, server_default="omega"),
    )
    op.create_table(
        "pushin_charges",
        sa.Column("order_id", sa.String(32), sa.ForeignKey("orders.id"), primary_key=True),
        sa.Column("transaction_id", sa.String(36), unique=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("copy_paste", sa.Text()),
        sa.Column("financial_status", sa.String(16)),
        sa.Column("expires_at", sa.BigInteger()),
        sa.CheckConstraint("state IN ('creating', 'uncertain', 'ready')"),
    )
    op.create_table(
        "pushin_notifications",
        sa.Column("transaction_id", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("processed_version", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.BigInteger(), nullable=False),
        sa.Column("payment_hint", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "pushin_query_gates",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("next_query_at", sa.BigInteger(), nullable=False),
    )


def downgrade():
    op.drop_table("pushin_query_gates")
    op.drop_table("pushin_notifications")
    op.drop_table("pushin_charges")
    op.drop_column("orders", "payment_provider")
