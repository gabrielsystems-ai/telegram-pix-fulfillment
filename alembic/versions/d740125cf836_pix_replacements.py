"""Preserve replacement attempt lineage and serialize settlement."""

import sqlalchemy as sa

from alembic import op

revision = "d740125cf836"
down_revision = "c63f014be725"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pix_replacements",
        sa.Column("old_order_id", sa.String(32), sa.ForeignKey("orders.id"), primary_key=True),
        sa.Column("new_order_id", sa.String(32), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("root_order_id", sa.String(32), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("settled_order_id", sa.String(32), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("replaced_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("new_order_id"),
    )
    op.create_index("ix_pix_replacements_root_order_id", "pix_replacements", ["root_order_id"])


def downgrade():
    # Never silently discard a financial reconciliation chain.
    from alembic import context

    if (
        not context.is_offline_mode()
        and op.get_bind().execute(sa.text("SELECT 1 FROM pix_replacements LIMIT 1")).first()
    ):
        raise RuntimeError("Unsafe downgrade: Pix replacement history exists")
    op.drop_index("ix_pix_replacements_root_order_id", table_name="pix_replacements")
    op.drop_table("pix_replacements")
