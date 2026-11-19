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
    """Create the database and all tables. Idempotent."""
    engine = get_engine(settings)
    settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    return settings.db_path


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
