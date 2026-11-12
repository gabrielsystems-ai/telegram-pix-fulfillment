"""Inspect or install only the missing canonical VIP product. Never confirms payments."""

import argparse
import json

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from vip.catalog import OFFERS
from vip.config import Settings
from vip.db import Database
from vip.errors import DomainError
from vip.models import Product


def ensure_vip_product(session, chat_id):
    """Insert only if absent; never rewrite an existing offer, review or group."""
    if type(chat_id) is not int or chat_id >= 0:
        raise DomainError("vip_group_required")
    code = "vip_permanent"
    name, price, _ = OFFERS[code]
    if session.get(Product, code) is None:
        other = session.scalar(
            select(Product.code)
            .where(Product.code != code, func.lower(Product.name) == name.casefold())
            .limit(1)
        )
        if other:
            raise DomainError("vip_product_code_conflict")
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    session.execute(
        insert(Product)
        .values(
            code=code,
            name=name,
            amount_minor=price,
            telegram_group_id=chat_id,
            recovery_only=False,
            active=True,
            rights_reference=None,
        )
        .on_conflict_do_nothing(index_elements=[Product.code])
    )
    return session.get(Product, code, populate_existing=True)


def inspect_product(session, chat_id):
    product = session.get(Product, "vip_permanent")
    if product is None:
        return {
            "code": "vip_permanent",
            "exists": False,
            "visible": False,
            "cause": "missing_product",
        }
    cause = "inactive" if not product.active else "recovery_only" if product.recovery_only else None
    return {
        "code": product.code,
        "exists": True,
        "visible": cause is None,
        "cause": cause,
        "amount_minor": product.amount_minor,
        "group_matches_configuration": chat_id is not None and product.telegram_group_id == chat_id,
        "distribution_reviewed": bool(product.rights_reference),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Insert missing VIP; otherwise read only"
    )
    args = parser.parse_args()
    db = None
    try:
        settings = Settings()
        if args.apply and any(
            (
                settings.omega_pix_enabled,
                settings.vip_group_enabled,
                settings.recovery_enabled,
                settings.recovery_promotions_approved,
            )
        ):
            raise DomainError("repair_disable_automation_first")
        db = Database(settings.database_url)
        with db.transaction() as session:
            before = inspect_product(session, settings.vip_chat_id)
            if args.apply:
                ensure_vip_product(session, settings.vip_chat_id)
            after = inspect_product(session, settings.vip_chat_id)
        print(
            json.dumps(
                {
                    "database": db.engine.dialect.name,
                    "applied": args.apply,
                    "before": before,
                    "after": after,
                }
            )
        )
    except DomainError as error:
        parser.exit(1, f"Repair rejected: {error.code}\n")
    except ValueError:
        parser.exit(1, "Repair rejected: invalid_configuration\n")
    except SQLAlchemyError as error:
        parser.exit(1, f"Repair unavailable: {type(error).__name__}\n")
    finally:
        if db is not None:
            db.engine.dispose()


if __name__ == "__main__":
    main()
