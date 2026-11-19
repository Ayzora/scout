"""Alembic environment, driven by Scout's own settings.

The database URL comes from ``Settings`` rather than ``alembic.ini``, so there is one
definition of where the database lives and ``SCOUT_DATA_DIR`` applies to migrations as
it does to everything else.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from scout.config import load_settings
from scout.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = load_settings()
config.set_main_option("sqlalchemy.url", settings.db_url)


def run_migrations_offline() -> None:
    context.configure(
        url=settings.db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place, so Alembic has to copy the table,
        # move the data and swap it. Without this, adding a column with a constraint
        # or changing one fails outright.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # SQLite's "batch" migrations rebuild a table by copying it and swapping it in.
    # With foreign keys enforced, dependent tables would follow the old table through
    # the rename and end up pointing at nothing. Scout's own engine turns them on per
    # connection (db/session.py); this one must not.
    #
    # It has to be set at connect time. Running the pragma on an open connection
    # instead starts a transaction Alembic does not own, and its migration then never
    # commits -- the command exits 0 having applied nothing, which is worse than
    # failing.
    @event.listens_for(connectable, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
