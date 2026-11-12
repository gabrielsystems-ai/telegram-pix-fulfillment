from sqlalchemy import select
from sqlalchemy.orm import Session

from vip.errors import DomainError
from vip.models import Plan


def create_plan(
    session: Session, *, name: str, amount_minor: int, duration_days: int, telegram_group_id: int
) -> Plan:
    if (
        not name.strip()
        or len(name.strip()) > 100
        or amount_minor <= 0
        or duration_days <= 0
        or telegram_group_id >= 0
    ):
        raise DomainError("invalid_plan")
    plan = Plan(
        name=name.strip(),
        amount_minor=amount_minor,
        duration_days=duration_days,
        telegram_group_id=telegram_group_id,
    )
    session.add(plan)
    session.flush()
    return plan


def list_plans(session: Session) -> list[Plan]:
    return list(session.scalars(select(Plan).where(Plan.active.is_(True)).order_by(Plan.name)))
