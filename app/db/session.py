"""Engine and sessions (spec 13.5)."""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    """The process engine, created on first use so importing never connects.

    Every connection's session time zone is UTC (``db_connect_args``). Neon
    scales the compute to zero after 5 idle minutes and drops its connections;
    ``pool_pre_ping`` replaces them, so the first request after idle only waits
    for the cold start."""
    global _engine
    if _engine is None:
        current = get_settings()
        _engine = create_engine(
            current.db_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=current.DB_POOL_SIZE,
            max_overflow=current.DB_MAX_OVERFLOW,
            pool_timeout=10,
            connect_args=current.db_connect_args,
        )
    return _engine


SessionLocal = sessionmaker(autoflush=False, autocommit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    db = SessionLocal(bind=get_engine())
    try:
        yield db
    finally:
        db.close()
