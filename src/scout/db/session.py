"""Database engine and session management.

SQLite needs two pragmas set per connection to behave the way the rest of the code
assumes: ``foreign_keys`` is off by default (so FK constraints are silently ignored),
and ``journal_mode=WAL`` lets the API read while the agent worker writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from scout.config import Settings
from scout.db.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # Wait rather than fail instantly if the other process holds a write lock.
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def get_engine(settings: Settings) -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(settings.db_url, future=True)
        event.listen(_engine, "connect", _configure_sqlite)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db(settings: Settings) -> Path:
    """Create the database and all tables. Idempotent.

    ``create_all`` adds missing tables but never alters an existing one, so on a
    database created before a column was added it leaves the schema half-right and
    fails later at query time. New tables therefore come from here and changes to
    existing ones from Alembic (``uv run alembic upgrade head``), with
    ``schema_is_current`` telling the two apart.
    """
    from sqlalchemy import inspect

    engine = get_engine(settings)
    settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
    was_empty = not inspect(engine).has_table("runs")
    Base.metadata.create_all(engine)

    # A database create_all has just built is, by definition, at the latest schema --
    # but Alembic has no way to know that, and would report a brand-new install as
    # predating migrations. Stamping it closes the gap between the two mechanisms.
    if was_empty:
        _stamp_head(settings)
    return settings.db_path


def _stamp_head(settings: Settings) -> None:
    """Record the current revision on a database built by ``create_all``."""
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        return
    ini = Path(__file__).resolve().parents[3] / "alembic.ini"
    if not ini.exists():
        return
    try:
        config = Config(str(ini))
        config.set_main_option("sqlalchemy.url", settings.db_url)
        command.stamp(config, "head")
    except Exception:
        # Not fatal: the database is usable, and schema_is_current will say what to
        # run. Failing init over a bookkeeping row would be worse.
        pass


def schema_is_current(settings: Settings) -> tuple[bool, str | None]:
    """Whether the database is at the latest migration.

    Returns ``(ok, message)``. A database with no ``alembic_version`` row predates
    migrations entirely; one stamped at an older revision needs an upgrade. Either way
    the caller can say so plainly instead of failing on a missing column later.
    """
    from sqlalchemy import inspect, text

    engine = get_engine(settings)
    inspector = inspect(engine)
    if not inspector.has_table("alembic_version"):
        if not inspector.has_table("runs"):
            return True, None  # empty database; init_db will build it
        return False, (
            "this database predates migrations. Run: uv run alembic stamp head"
        )

    with engine.connect() as connection:
        current = connection.execute(
            text("select version_num from alembic_version")
        ).scalar()

    expected = _head_revision()
    if expected is None or current == expected:
        return True, None
    return False, (
        f"database is at migration {current}, latest is {expected}. "
        "Run: uv run alembic upgrade head"
    )


def _head_revision() -> str | None:
    """The newest revision on disk, or None if Alembic is not set up."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError:
        return None
    root = Path(__file__).resolve().parents[3]
    ini = root / "alembic.ini"
    if not ini.exists():
        return None
    try:
        return ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    except Exception:
        return None


@contextmanager
def session_scope(settings: Settings) -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    get_engine(settings)
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop cached engine/session factory. Used by tests."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
