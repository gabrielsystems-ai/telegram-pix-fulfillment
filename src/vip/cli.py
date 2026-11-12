import argparse
from time import time

from vip.catalog import OFFERS, install_catalog
from vip.config import Settings
from vip.db import Database
from vip.errors import DomainError
from vip.models import AuditEvent, Product
from vip.orders import expire_orders
from vip.plans import create_plan
from vip.subscriptions import expire_subscriptions


def main():
    parser = argparse.ArgumentParser(
        description="VIP local administration; never confirms payments"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("create-plan")
    plan.add_argument("--name", required=True)
    plan.add_argument("--amount-minor", required=True, type=int)
    plan.add_argument("--duration-days", required=True, type=int)
    plan.add_argument("--group-id", required=True, type=int)
    commands.add_parser(
        "expire", help="Expire DB records only; does not change Telegram membership"
    )
    commands.add_parser("install-catalog", help="Install/update the three offers using VIP_CHAT_ID")
    commands.add_parser("update-video-offer", help="Update only the video product name and price")
    review = commands.add_parser(
        "review-product", help="Record an external rights review or disable an offer"
    )
    review.add_argument("--code", choices=OFFERS, required=True)
    review.add_argument("--rights-reference")
    review.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    db = Database(settings.database_url)
    try:
        with db.transaction() as session:
            if args.command == "create-plan":
                created = create_plan(
                    session,
                    name=args.name,
                    amount_minor=args.amount_minor,
                    duration_days=args.duration_days,
                    telegram_group_id=args.group_id,
                )
                result = f"Plan created: {created.id}"
            elif args.command == "install-catalog":
                install_catalog(session, settings.vip_chat_id)
                result = "Catalog installed. Distribution review remains required."
            elif args.command == "update-video-offer":
                from vip.video_delivery import update_video_offer

                update_video_offer(session)
                result = "Video offer updated. Previous orders and other products unchanged."
            elif args.command == "review-product":
                product = session.get(Product, args.code)
                reference = args.rights_reference
                if product is None or (
                    not args.disable and (not reference or len(reference) > 100)
                ):
                    raise DomainError("distribution_review_required")
                if reference:
                    product.rights_reference = reference
                if args.disable:
                    product.active = False
                session.add(
                    AuditEvent(
                        action="product_review_updated",
                        entity_id=product.code,
                        occurred_at=int(time()),
                    )
                )
                result = "Product review/status updated. No payment or delivery was performed."
            else:
                orders = expire_orders(session)
                subscriptions = expire_subscriptions(session)
                result = (
                    f"Expired orders: {orders}; subscriptions: {subscriptions}. "
                    "Telegram group membership was not changed."
                )
        print(result)
    except DomainError as error:
        parser.exit(status=1, message=f"Operation rejected: {error.code}\n")
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
