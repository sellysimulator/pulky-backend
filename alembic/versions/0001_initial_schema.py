"""Initial schema (spec 13.5).

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def _pk() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False)


def _game_id() -> sa.Column:
    return sa.Column("game_id", sa.BigInteger(), nullable=False)


def _int(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False)


def _flag(name: str) -> sa.Column:
    return sa.Column(name, sa.Boolean(), nullable=False)


def _money(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.Numeric(12, 2), nullable=nullable)


def _ratio(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, mysql.DOUBLE(), nullable=nullable)


def _json(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.JSON(), nullable=nullable)


def _game_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE")


PLAYER_DAY_INTS = [
    "day", "order_spot", "order_futures", "lots_conserved", "opening_stock",
    "futures_due", "futures_filled", "futures_unfilled", "allocated_spot",
    "shortfall_spot", "shipped", "lost_transport", "delivered", "peer_bought",
    "peer_sold", "demand", "backlog_open", "sold", "backlog_end", "spoiled",
    "closing_stock",
]  # fmt: skip
PLAYER_DAY_MONEY = [
    "outlay", "revenue_sales", "revenue_salvage", "revenue_peer", "refund_insurance",
    "cost_spot", "cost_futures", "cost_peer", "refund_futures", "cost_transport",
    "cost_insurance", "cost_conservation", "cost_holding", "cost_backlog", "profit",
    "cash_open", "cash_end", "futures_vs_spot",
]  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "users",
        _pk(),
        sa.Column("firebase_uid", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(24), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("photo_url", sa.String(1024), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("firebase_uid"),
    )
    op.create_table(
        "games",
        _pk(),
        sa.Column("public_id", mysql.CHAR(32), nullable=False),
        sa.Column("room_code", mysql.CHAR(6), nullable=False),
        sa.Column("host_user_id", sa.BigInteger(), nullable=True),
        sa.Column("host_display_name", sa.String(24), nullable=False),
        sa.Column("rng_seed", sa.BigInteger(), nullable=False),
        _int("duration_days"),
        _int("days_played"),
        _flag("ended_early"),
        _int("player_count"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(["host_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("room_code", "started_at"),
    )
    op.create_index("ix_games_room_code", "games", ["room_code"])
    op.create_table(
        "game_configs",
        _pk(),
        _game_id(),
        _int("config_schema_version"),
        _json("config"),
        *_stamps(),
        _game_fk(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id"),
    )
    op.create_table(
        "participants",
        _pk(),
        _game_id(),
        sa.Column("alias", sa.String(4), nullable=False),
        sa.Column("display_name", sa.String(24), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("guest_identity", sa.String(64), nullable=True),
        _int("final_rank"),
        _money("total_profit"),
        _int("units_sold"),
        _int("units_spoiled"),
        _int("units_lost_transport"),
        _ratio("overall_market_share", True),
        _ratio("fill_rate", True),
        _ratio("waste_rate", True),
        _ratio("avg_reputation"),
        _ratio("final_reputation"),
        _int("futures_units"),
        _money("cost_insurance"),
        _money("refund_insurance"),
        _int("peer_units_bought"),
        _int("peer_units_sold"),
        _money("cost_peer"),
        _money("revenue_peer"),
        _int("liquidated_units"),
        _money("revenue_liquidation"),
        _money("refund_insurance_in_transit"),
        _int("futures_cancelled_units"),
        _money("refund_futures_cancelled"),
        *_stamps(),
        _game_fk(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "alias"),
    )
    op.create_index("ix_participants_user_id", "participants", ["user_id"])
    op.create_index(
        "ix_participants_guest_identity", "participants", ["guest_identity"]
    )
    op.create_table(
        "player_days",
        _pk(),
        _game_id(),
        _int("day"),
        sa.Column("alias", sa.String(4), nullable=False),
        sa.Column("weekday", sa.String(3), nullable=False),
        *[_int(n) for n in PLAYER_DAY_INTS if n != "day"],
        sa.Column("incoterm", sa.String(3), nullable=False),
        _json("conserve_lots"),
        _flag("cooperate"),
        _json("peer_buy", True),
        _json("peer_sell", True),
        _json("peer_trades"),
        *[_money(n) for n in PLAYER_DAY_MONEY],
        _ratio("fill", True),
        _ratio("waste", True),
        _ratio("reputation_open"),
        _ratio("reputation_end"),
        _ratio("market_share", True),
        _flag("refrigeration_failure"),
        *_stamps(),
        _game_fk(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "day", "alias"),
    )
    op.create_table(
        "market_days",
        _pk(),
        _game_id(),
        _int("day"),
        sa.Column("weekday", sa.String(3), nullable=False),
        _ratio("weekday_multiplier"),
        _ratio("expected_demand"),
        _int("demand_total"),
        _int("capacity"),
        _flag("disrupted"),
        _money("spot_price"),
        _flag("cooperation"),
        _int("cooperators"),
        _flag("refrigeration_failure"),
        _int("units_sold_total"),
        _int("peer_units"),
        _money("peer_avg_price", True),
        *_stamps(),
        _game_fk(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "day"),
    )
    op.create_table(
        "user_stats",
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        _int("games_played"),
        _int("wins"),
        _money("total_profit"),
        _money("best_profit"),
        _ratio("avg_fill_rate", True),
        _ratio("avg_waste_rate", True),
        _ratio("avg_market_share", True),
        *_stamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_stats")
    op.drop_table("market_days")
    op.drop_table("player_days")
    op.drop_table("participants")
    op.drop_table("game_configs")
    op.drop_table("games")
    op.drop_table("users")
