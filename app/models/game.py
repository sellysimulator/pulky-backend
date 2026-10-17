"""Game, participant, day and statistics tables (spec 13.5)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import CHAR, DOUBLE
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.user import MAX_DISPLAY_NAME

_BigId = BigInteger()


def _id() -> Mapped[int]:
    return mapped_column(_BigId, primary_key=True, autoincrement=True)


def _game_fk() -> Mapped[int]:
    return mapped_column(
        _BigId, ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )


def _money() -> Mapped[Decimal]:
    return mapped_column(Numeric(12, 2), nullable=False)


def _ratio(nullable: bool = False) -> Mapped[Any]:
    # asdecimal=False: read back as the exact float stored, never a rounded Decimal.
    return mapped_column(DOUBLE(asdecimal=False), nullable=nullable)


def _int() -> Mapped[int]:
    return mapped_column(Integer, nullable=False)


def _flag() -> Mapped[bool]:
    return mapped_column(Boolean, nullable=False)


def _json() -> Mapped[Any]:
    return mapped_column(JSON, nullable=False)


class Game(TimestampMixin, Base):
    __tablename__ = "games"

    id: Mapped[int] = _id()
    public_id: Mapped[str] = mapped_column(CHAR(32), unique=True)
    room_code: Mapped[str] = mapped_column(CHAR(6), index=True)
    host_user_id: Mapped[int | None] = mapped_column(
        _BigId, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    host_display_name: Mapped[str] = mapped_column(String(MAX_DISPLAY_NAME))
    rng_seed: Mapped[int] = mapped_column(BigInteger)
    duration_days: Mapped[int] = _int()
    days_played: Mapped[int] = _int()
    ended_early: Mapped[bool] = _flag()
    player_count: Mapped[int] = _int()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GameConfigRow(TimestampMixin, Base):
    __tablename__ = "game_configs"

    id: Mapped[int] = _id()
    game_id: Mapped[int] = mapped_column(
        _BigId, ForeignKey("games.id", ondelete="CASCADE"), unique=True
    )
    config_schema_version: Mapped[int] = _int()
    config: Mapped[Any] = _json()


class Participant(TimestampMixin, Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("game_id", "alias"),)

    id: Mapped[int] = _id()
    game_id: Mapped[int] = _game_fk()
    alias: Mapped[str] = mapped_column(String(4))
    display_name: Mapped[str] = mapped_column(String(MAX_DISPLAY_NAME))
    user_id: Mapped[int | None] = mapped_column(
        _BigId, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    guest_identity: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    final_rank: Mapped[int] = _int()
    total_profit: Mapped[Decimal] = _money()
    units_sold: Mapped[int] = _int()
    units_spoiled: Mapped[int] = _int()
    units_lost_transport: Mapped[int] = _int()
    overall_market_share: Mapped[float | None] = _ratio(True)
    fill_rate: Mapped[float | None] = _ratio(True)
    waste_rate: Mapped[float | None] = _ratio(True)
    avg_reputation: Mapped[float] = _ratio()
    final_reputation: Mapped[float] = _ratio()
    futures_units: Mapped[int] = _int()
    cost_insurance: Mapped[Decimal] = _money()
    refund_insurance: Mapped[Decimal] = _money()
    peer_units_bought: Mapped[int] = _int()
    peer_units_sold: Mapped[int] = _int()
    cost_peer: Mapped[Decimal] = _money()
    revenue_peer: Mapped[Decimal] = _money()
    liquidated_units: Mapped[int] = _int()
    revenue_liquidation: Mapped[Decimal] = _money()
    refund_insurance_in_transit: Mapped[Decimal] = _money()
    futures_cancelled_units: Mapped[int] = _int()
    refund_futures_cancelled: Mapped[Decimal] = _money()


class PlayerDay(TimestampMixin, Base):
    __tablename__ = "player_days"
    __table_args__ = (UniqueConstraint("game_id", "day", "alias"),)

    id: Mapped[int] = _id()
    game_id: Mapped[int] = _game_fk()
    day: Mapped[int] = _int()
    alias: Mapped[str] = mapped_column(String(4))
    weekday: Mapped[str] = mapped_column(String(3))
    # Decision
    order_spot: Mapped[int] = _int()
    order_futures: Mapped[int] = _int()
    incoterm: Mapped[str] = mapped_column(String(3))
    conserve_lots: Mapped[Any] = _json()
    lots_conserved: Mapped[int] = _int()
    cooperate: Mapped[bool] = _flag()
    peer_buy: Mapped[Any | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    peer_sell: Mapped[Any | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    outlay: Mapped[Decimal] = _money()
    # Flow
    opening_stock: Mapped[int] = _int()
    futures_due: Mapped[int] = _int()
    futures_filled: Mapped[int] = _int()
    futures_unfilled: Mapped[int] = _int()
    allocated_spot: Mapped[int] = _int()
    shortfall_spot: Mapped[int] = _int()
    shipped: Mapped[int] = _int()
    lost_transport: Mapped[int] = _int()
    delivered: Mapped[int] = _int()
    peer_bought: Mapped[int] = _int()
    peer_sold: Mapped[int] = _int()
    peer_trades: Mapped[Any] = _json()
    demand: Mapped[int] = _int()
    backlog_open: Mapped[int] = _int()
    sold: Mapped[int] = _int()
    backlog_end: Mapped[int] = _int()
    spoiled: Mapped[int] = _int()
    closing_stock: Mapped[int] = _int()
    # Money
    revenue_sales: Mapped[Decimal] = _money()
    revenue_salvage: Mapped[Decimal] = _money()
    revenue_peer: Mapped[Decimal] = _money()
    refund_insurance: Mapped[Decimal] = _money()
    cost_spot: Mapped[Decimal] = _money()
    cost_futures: Mapped[Decimal] = _money()
    cost_peer: Mapped[Decimal] = _money()
    refund_futures: Mapped[Decimal] = _money()
    cost_transport: Mapped[Decimal] = _money()
    cost_insurance: Mapped[Decimal] = _money()
    cost_conservation: Mapped[Decimal] = _money()
    cost_holding: Mapped[Decimal] = _money()
    cost_backlog: Mapped[Decimal] = _money()
    profit: Mapped[Decimal] = _money()
    cash_open: Mapped[Decimal] = _money()
    cash_end: Mapped[Decimal] = _money()
    futures_vs_spot: Mapped[Decimal] = _money()
    # Other
    fill: Mapped[float | None] = _ratio(True)
    waste: Mapped[float | None] = _ratio(True)
    reputation_open: Mapped[float] = _ratio()
    reputation_end: Mapped[float] = _ratio()
    market_share: Mapped[float | None] = _ratio(True)
    refrigeration_failure: Mapped[bool] = _flag()


class MarketDayRow(TimestampMixin, Base):
    __tablename__ = "market_days"
    __table_args__ = (UniqueConstraint("game_id", "day"),)

    id: Mapped[int] = _id()
    game_id: Mapped[int] = _game_fk()
    day: Mapped[int] = _int()
    weekday: Mapped[str] = mapped_column(String(3))
    weekday_multiplier: Mapped[float] = _ratio()
    expected_demand: Mapped[float] = _ratio()
    demand_total: Mapped[int] = _int()
    capacity: Mapped[int] = _int()
    disrupted: Mapped[bool] = _flag()
    spot_price: Mapped[Decimal] = _money()
    cooperation: Mapped[bool] = _flag()
    cooperators: Mapped[int] = _int()
    refrigeration_failure: Mapped[bool] = _flag()
    units_sold_total: Mapped[int] = _int()
    peer_units: Mapped[int] = _int()
    peer_avg_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )


class UserStats(TimestampMixin, Base):
    __tablename__ = "user_stats"

    user_id: Mapped[int] = mapped_column(
        _BigId, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    games_played: Mapped[int] = _int()
    wins: Mapped[int] = _int()
    total_profit: Mapped[Decimal] = _money()
    best_profit: Mapped[Decimal] = _money()
    avg_fill_rate: Mapped[float | None] = _ratio(True)
    avg_waste_rate: Mapped[float | None] = _ratio(True)
    avg_market_share: Mapped[float | None] = _ratio(True)


__all__ = [
    "Game",
    "GameConfigRow",
    "MarketDayRow",
    "Participant",
    "PlayerDay",
    "UserStats",
]
