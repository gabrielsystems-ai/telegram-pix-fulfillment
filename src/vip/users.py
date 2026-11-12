from time import time

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from vip.errors import DomainError
from vip.models import User


def lock_buyer(session, telegram_id):
    result = session.execute(
        update(User).where(User.telegram_id == telegram_id).values(telegram_id=User.telegram_id)
    )
    if result.rowcount != 1:
        raise DomainError("unknown_user")


def register_user(session: Session, telegram_id: int) -> User:
    if telegram_id <= 0:
        raise DomainError("invalid_telegram_id")
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    session.execute(
        insert(User)
        .values(telegram_id=telegram_id, created_at=int(time()))
        .on_conflict_do_nothing(index_elements=[User.telegram_id])
    )
    return session.scalar(select(User).where(User.telegram_id == telegram_id))
