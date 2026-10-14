"""Declarative base and the timestamp mixin (spec 13.5)."""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, func
from sqlalchemy.orm import DeclarativeBase


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


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
