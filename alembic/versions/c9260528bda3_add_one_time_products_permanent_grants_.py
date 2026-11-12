"""add one-time products permanent grants and recovery tasks

Revision ID: c9260528bda3
Revises: c36678beb654
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9260528bda3"
down_revision: str | Sequence[str] | None = "c36678beb654"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _legacy_checks():
    if op.get_bind().dialect.name != "sqlite":
        return ()
    # Alembic batch reflection omits unnamed CHECKs unless supplied explicitly.
    return tuple(
        sa.CheckConstraint(expression)
        for expression in (
            "amount_minor > 0",
            "duration_days > 0",
            "currency = 'BRL'",
            "status IN ('pending', 'paid', 'cancelled', 'expired')",
        )
    )


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("telegram_group_id", sa.BigInteger(), nullable=True),
        sa.Column("recovery_only", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("rights_reference", sa.String(length=100), nullable=True),
        sa.CheckConstraint("amount_minor > 0"),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_table(
        "buyer_assurances",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("declared_at", sa.BigInteger(), nullable=True),
        sa.Column("verified_at", sa.BigInteger(), nullable=True),
        sa.Column("verification_reference", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(
            ["telegram_id"],
            ["users.telegram_id"],
        ),
        sa.PrimaryKeyConstraint("telegram_id"),
    )
    op.create_table(
        "recovery_tasks",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("first_started_at", sa.BigInteger(), nullable=False),
        sa.Column("due_at", sa.BigInteger(), nullable=False),
        sa.Column("next_check_at", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("eligible_at", sa.BigInteger(), nullable=True),
        sa.Column("opted_in_at", sa.BigInteger(), nullable=True),
        sa.Column("opted_out_at", sa.BigInteger(), nullable=True),
        sa.Column("blocked_at", sa.BigInteger(), nullable=True),
        sa.Column("send_attempt_at", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'eligible', 'sending', 'sent', 'skipped', "
            "'canceled', 'failed', 'uncertain')"
        ),
        sa.ForeignKeyConstraint(
            ["telegram_id"],
            ["users.telegram_id"],
        ),
        sa.PrimaryKeyConstraint("telegram_id"),
    )
    with op.batch_alter_table("recovery_tasks", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_recovery_tasks_next_check_at"), ["next_check_at"], unique=False
        )

    op.create_table(
        "access_grants",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("permission", sa.String(length=32), nullable=False),
        sa.Column("telegram_group_id", sa.BigInteger(), nullable=True),
        sa.Column("granted_at", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("permission IN ('sample_video_access', 'vip_permanent_access')"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "permission"),
    )
    with op.batch_alter_table("access_grants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_access_grants_order_id"), ["order_id"], unique=False)

    with op.batch_alter_table("orders", schema=None, table_args=_legacy_checks()) as batch_op:
        batch_op.add_column(sa.Column("product_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("product_name", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("buyer_telegram_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("sales_request_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("combo_buyer_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(
            sa.Column("origin", sa.String(length=16), server_default="standard", nullable=False)
        )
        batch_op.alter_column("plan_id", existing_type=sa.VARCHAR(length=32), nullable=True)
        batch_op.alter_column("duration_days", existing_type=sa.INTEGER(), nullable=True)
        batch_op.alter_column("telegram_group_id", existing_type=sa.BIGINT(), nullable=True)
        batch_op.create_check_constraint(
            "ck_orders_kind",
            "(plan_id IS NOT NULL AND product_id IS NULL) OR "
            "(plan_id IS NULL AND product_id IS NOT NULL AND duration_days IS NULL)",
        )
        batch_op.create_index(
            batch_op.f("ix_orders_buyer_telegram_id"), ["buyer_telegram_id"], unique=False
        )
        batch_op.create_unique_constraint("uq_orders_combo_buyer", ["combo_buyer_id"])
        batch_op.create_unique_constraint("uq_orders_sales_request", ["sales_request_id"])
        batch_op.create_foreign_key(
            "fk_orders_combo_buyer", "users", ["combo_buyer_id"], ["telegram_id"]
        )
        batch_op.create_foreign_key("fk_orders_product", "products", ["product_id"], ["code"])
        batch_op.create_foreign_key(
            "fk_orders_buyer", "users", ["buyer_telegram_id"], ["telegram_id"]
        )

    # ### end Alembic commands ###


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM orders WHERE product_id IS NOT NULL LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Downgrade refused: product orders must be archived explicitly first")
    with op.batch_alter_table("orders", schema=None, table_args=_legacy_checks()) as batch_op:
        batch_op.drop_constraint("ck_orders_kind", type_="check")
        batch_op.drop_constraint("fk_orders_combo_buyer", type_="foreignkey")
        batch_op.drop_constraint("fk_orders_product", type_="foreignkey")
        batch_op.drop_constraint("fk_orders_buyer", type_="foreignkey")
        batch_op.drop_constraint("uq_orders_combo_buyer", type_="unique")
        batch_op.drop_constraint("uq_orders_sales_request", type_="unique")
        batch_op.drop_index(batch_op.f("ix_orders_buyer_telegram_id"))
        batch_op.alter_column("telegram_group_id", existing_type=sa.BIGINT(), nullable=False)
        batch_op.alter_column("duration_days", existing_type=sa.INTEGER(), nullable=False)
        batch_op.alter_column("plan_id", existing_type=sa.VARCHAR(length=32), nullable=False)
        batch_op.drop_column("origin")
        batch_op.drop_column("combo_buyer_id")
        batch_op.drop_column("sales_request_id")
        batch_op.drop_column("buyer_telegram_id")
        batch_op.drop_column("product_name")
        batch_op.drop_column("product_id")

    with op.batch_alter_table("access_grants", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_access_grants_order_id"))

    op.drop_table("access_grants")
    with op.batch_alter_table("recovery_tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_recovery_tasks_next_check_at"))

    op.drop_table("recovery_tasks")
    op.drop_table("buyer_assurances")
    op.drop_table("products")
    # ### end Alembic commands ###
