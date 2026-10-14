"""Engine and sessions (spec 13.5)."""

from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None


def _set_utc(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET time_zone = '+00:00'")
    finally:
        cursor.close()


def get_engine() -> Engine:
    """The process engine, created on first use so importing never connects."""
    global _engine
    if _engine is None:
        current = get_settings()
        _engine = create_engine(
            current.db_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=10,
            max_overflow=20,
            connect_args=current.db_connect_args,
        )
        event.listen(_engine, "connect", _set_utc)
    return _engine


SessionLocal = sessionmaker(autoflush=False, autocommit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    db = SessionLocal(bind=get_engine())
    try:
        yield db
    finally:
        db.close()
