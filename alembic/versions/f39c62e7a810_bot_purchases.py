"""Persist the active bot purchase without altering orders or historical grants."""

import sqlalchemy as sa

from alembic import op

revision = "f39c62e7a810"
down_revision = "e28f104a6209"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_purchases",
        sa.Column(
            "telegram_id", sa.BigInteger(), sa.ForeignKey("users.telegram_id"), primary_key=True
        ),
        sa.Column(
            "order_id", sa.String(32), sa.ForeignKey("orders.id"), nullable=False, unique=True
        ),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("pix_message_id", sa.BigInteger()),
        sa.Column("pix_file_id", sa.Text()),
        sa.Column("pix_code_hash", sa.String(64)),
        sa.CheckConstraint("stage IN ('buyer', 'payment')"),
    )


def downgrade():
    op.drop_table("bot_purchases")
