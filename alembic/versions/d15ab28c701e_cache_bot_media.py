"""Cache reusable Telegram media references, without changing existing tables."""

import sqlalchemy as sa

from alembic import op

revision = "d15ab28c701e"
down_revision = "c9260528bda3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bot_media_cache",
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("bot_id", "content_hash"),
    )


def downgrade():
    op.drop_table("bot_media_cache")
