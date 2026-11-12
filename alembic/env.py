from alembic import context
from vip.config import Settings
from vip.db import Database, normalize_database_url
from vip.models import Base

target_metadata = Base.metadata


def offline():
    context.configure(
        url=normalize_database_url(Settings().database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def online():
    db = Database(Settings().database_url)
    try:
        with db.engine.connect() as connection:
            sqlite = db.engine.dialect.name == "sqlite"
            # SQLite batch ALTER must replace the physical table. Referencing rows
            # are retained, and FK integrity is checked before committing the migration.
            raw = connection.connection.driver_connection if sqlite else None
            if sqlite:
                raw.execute("PRAGMA foreign_keys=OFF")
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=db.engine.dialect.name == "sqlite",
                compare_type=True,
                transactional_ddl=True,
            )
            with context.begin_transaction():
                context.run_migrations()
                if sqlite and raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("Migration foreign key validation failed")
            if sqlite:
                raw.execute("PRAGMA foreign_keys=ON")
    finally:
        db.engine.dispose()


if context.is_offline_mode():
    offline()
else:
    online()
