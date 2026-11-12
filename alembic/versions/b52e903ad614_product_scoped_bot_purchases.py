"""Keep resumable bot purchases independent per buyer and product."""

import sqlalchemy as sa

from alembic import context, op

revision = "b52e903ad614"
down_revision = "a41d7e8c902b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_purchases", sa.Column("product_code", sa.String(32), nullable=True))
    op.add_column(
        "bot_purchases",
        sa.Column("selection_order", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE bot_purchases SET product_code = "
        "(SELECT product_id FROM orders WHERE orders.id = bot_purchases.order_id), "
        "selection_order = 1"
    )
    with op.batch_alter_table(
        "bot_purchases",
        naming_convention={"pk": "%(table_name)s_pkey", "ck": "%(table_name)s_stage_check"},
    ) as batch:
        batch.drop_constraint("bot_purchases_pkey", type_="primary")
        batch.alter_column("product_code", existing_type=sa.String(32), nullable=False)
        batch.create_primary_key("bot_purchases_pkey", ["telegram_id", "product_code"])
        batch.create_foreign_key("fk_bot_purchases_product", "products", ["product_code"], ["code"])


def downgrade():
    if not context.is_offline_mode():
        multiple = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT telegram_id FROM bot_purchases GROUP BY telegram_id "
                    "HAVING count(*) > 1 LIMIT 1"
                )
            )
            .first()
        )
        if multiple is not None:
            raise RuntimeError("Unsafe downgrade: multiple product purchases would be lost")
    with op.batch_alter_table(
        "bot_purchases",
        naming_convention={"pk": "%(table_name)s_pkey", "ck": "%(table_name)s_stage_check"},
    ) as batch:
        batch.drop_constraint("bot_purchases_pkey", type_="primary")
        batch.drop_constraint("fk_bot_purchases_product", type_="foreignkey")
        batch.create_primary_key("bot_purchases_pkey", ["telegram_id"])
        batch.drop_column("product_code")
        batch.drop_column("selection_order")
