"""Declarative base and the timestamp mixin (spec 13.5)."""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint and index names, the same in PostgreSQL, SQLite and the
# generated DDL, so a migration can always name what it alters or drops.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``server_default`` is mandatory: raw INSERTs bypass ORM ``default=``."""

    created_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
