from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from vip.errors import DomainError


def normalize_database_url(url: str) -> str:
    # Some hosts supply the generic PostgreSQL scheme; our installed driver is psycopg 3.
    for scheme in ("postgresql://", "postgres://"):
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url[len(scheme) :]
    return url


class Database:
    def __init__(self, url: str):
        url = normalize_database_url(url)
        sqlite = url.startswith("sqlite:")
        options = {"check_same_thread": False, "timeout": 30} if sqlite else {}
        if url.startswith("postgresql+psycopg://"):
            options = {"connect_timeout": 10}
        self.engine = create_engine(url, connect_args=options, pool_pre_ping=True)
        if sqlite:

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _record):
                connection.isolation_level = None
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

            @event.listens_for(self.engine, "begin")
            def begin_sqlite(connection):
                # Small development database: serialize whole transactions, including reads.
                # This also makes DDL and rollback semantics explicit on Python 3.12+.
                connection.exec_driver_sql("BEGIN IMMEDIATE")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        try:
            with self.sessions.begin() as session:
                yield session
        except IntegrityError:
            # Rollback has already occurred. Do not leak SQL, bound values or credentials.
            raise DomainError("conflicting_operation") from None
