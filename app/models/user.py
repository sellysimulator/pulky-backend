"""The ``users`` table (spec 13.5)."""

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

MAX_DISPLAY_NAME = 24


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    firebase_uid: Mapped[str] = mapped_column(String(128), unique=True)
    display_name: Mapped[str] = mapped_column(String(MAX_DISPLAY_NAME))
    email: Mapped[str | None] = mapped_column(String(320))
    photo_url: Mapped[str | None] = mapped_column(String(1024))
