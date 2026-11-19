"""PostgreSQL baseline: every table of spec 13.5.

Constraint and index names come from ``Base.metadata``'s naming convention.
A stored game is keyed by its ``public_id`` alone: a room code is reissued once
its live room has expired, so ``room_code`` is indexed but never unique.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("firebase_uid", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=24), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("photo_url", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("firebase_uid", name=op.f("uq_users_firebase_uid")),
    )
    op.create_table(
        "games",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=32), nullable=False),
        sa.Column("room_code", sa.CHAR(length=6), nullable=False),
        sa.Column("host_user_id", sa.BigInteger(), nullable=True),
        sa.Column("host_display_name", sa.String(length=24), nullable=False),
        sa.Column("rng_seed", sa.BigInteger(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("days_played", sa.Integer(), nullable=False),
        sa.Column("ended_early", sa.Boolean(), nullable=False),
        sa.Column("player_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["host_user_id"],
            ["users.id"],
            name=op.f("fk_games_host_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_games")),
        sa.UniqueConstraint("public_id", name=op.f("uq_games_public_id")),
    )
    op.create_index(op.f("ix_games_room_code"), "games", ["room_code"], unique=False)
    op.create_table(
        "user_stats",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("games_played", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("total_profit", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("best_profit", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("avg_fill_rate", sa.Double(), nullable=True),
        sa.Column("avg_waste_rate", sa.Double(), nullable=True),
        sa.Column("avg_market_share", sa.Double(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_stats_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_stats")),
    )
    op.create_table(
        "game_configs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.BigInteger(), nullable=False),
        sa.Column("config_schema_version", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name=op.f("fk_game_configs_game_id_games"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_configs")),
        sa.UniqueConstraint("game_id", name=op.f("uq_game_configs_game_id")),
    )
    op.create_table(
        "market_days",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.String(length=3), nullable=False),
        sa.Column("weekday_multiplier", sa.Double(), nullable=False),
        sa.Column("expected_demand", sa.Double(), nullable=False),
        sa.Column("demand_total", sa.Integer(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("disrupted", sa.Boolean(), nullable=False),
        sa.Column("spot_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cooperation", sa.Boolean(), nullable=False),
        sa.Column("cooperators", sa.Integer(), nullable=False),
        sa.Column("refrigeration_failure", sa.Boolean(), nullable=False),
        sa.Column("units_sold_total", sa.Integer(), nullable=False),
        sa.Column("peer_units", sa.Integer(), nullable=False),
        sa.Column("peer_avg_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name=op.f("fk_market_days_game_id_games"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_days")),
        sa.UniqueConstraint("game_id", "day", name=op.f("uq_market_days_game_id_day")),
    )
    op.create_table(
        "participants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.BigInteger(), nullable=False),
        sa.Column("alias", sa.String(length=4), nullable=False),
        sa.Column("display_name", sa.String(length=24), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("guest_identity", sa.String(length=64), nullable=True),
        sa.Column("final_rank", sa.Integer(), nullable=False),
        sa.Column("total_profit", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("units_sold", sa.Integer(), nullable=False),
        sa.Column("units_spoiled", sa.Integer(), nullable=False),
        sa.Column("units_lost_transport", sa.Integer(), nullable=False),
        sa.Column("overall_market_share", sa.Double(), nullable=True),
        sa.Column("fill_rate", sa.Double(), nullable=True),
        sa.Column("waste_rate", sa.Double(), nullable=True),
        sa.Column("avg_reputation", sa.Double(), nullable=False),
        sa.Column("final_reputation", sa.Double(), nullable=False),
        sa.Column("futures_units", sa.Integer(), nullable=False),
        sa.Column("cost_insurance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "refund_insurance", sa.Numeric(precision=12, scale=2), nullable=False
        ),
        sa.Column("peer_units_bought", sa.Integer(), nullable=False),
        sa.Column("peer_units_sold", sa.Integer(), nullable=False),
        sa.Column("cost_peer", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("revenue_peer", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("liquidated_units", sa.Integer(), nullable=False),
        sa.Column(
            "revenue_liquidation", sa.Numeric(precision=12, scale=2), nullable=False
        ),
        sa.Column(
            "refund_insurance_in_transit",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
        ),
        sa.Column("futures_cancelled_units", sa.Integer(), nullable=False),
        sa.Column(
            "refund_futures_cancelled",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name=op.f("fk_participants_game_id_games"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_participants_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_participants")),
        sa.UniqueConstraint(
            "game_id", "alias", name=op.f("uq_participants_game_id_alias")
        ),
    )
    op.create_index(
        op.f("ix_participants_guest_identity"),
        "participants",
        ["guest_identity"],
        unique=False,
    )
    op.create_index(
        op.f("ix_participants_user_id"), "participants", ["user_id"], unique=False
    )
    op.create_table(
        "player_days",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("game_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=4), nullable=False),
        sa.Column("weekday", sa.String(length=3), nullable=False),
        sa.Column("order_spot", sa.Integer(), nullable=False),
        sa.Column("order_futures", sa.Integer(), nullable=False),
        sa.Column("incoterm", sa.String(length=3), nullable=False),
        sa.Column("conserve_lots", sa.JSON(), nullable=False),
        sa.Column("lots_conserved", sa.Integer(), nullable=False),
        sa.Column("cooperate", sa.Boolean(), nullable=False),
        sa.Column("peer_buy", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("peer_sell", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("outlay", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("opening_stock", sa.Integer(), nullable=False),
        sa.Column("futures_due", sa.Integer(), nullable=False),
        sa.Column("futures_filled", sa.Integer(), nullable=False),
        sa.Column("futures_unfilled", sa.Integer(), nullable=False),
        sa.Column("allocated_spot", sa.Integer(), nullable=False),
        sa.Column("shortfall_spot", sa.Integer(), nullable=False),
        sa.Column("shipped", sa.Integer(), nullable=False),
        sa.Column("lost_transport", sa.Integer(), nullable=False),
        sa.Column("delivered", sa.Integer(), nullable=False),
        sa.Column("peer_bought", sa.Integer(), nullable=False),
        sa.Column("peer_sold", sa.Integer(), nullable=False),
        sa.Column("peer_trades", sa.JSON(), nullable=False),
        sa.Column("demand", sa.Integer(), nullable=False),
        sa.Column("backlog_open", sa.Integer(), nullable=False),
        sa.Column("sold", sa.Integer(), nullable=False),
        sa.Column("backlog_end", sa.Integer(), nullable=False),
        sa.Column("spoiled", sa.Integer(), nullable=False),
        sa.Column("closing_stock", sa.Integer(), nullable=False),
        sa.Column("revenue_sales", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("revenue_salvage", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("revenue_peer", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "refund_insurance", sa.Numeric(precision=12, scale=2), nullable=False
        ),
        sa.Column("cost_spot", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cost_futures", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cost_peer", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("refund_futures", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cost_transport", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cost_insurance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "cost_conservation", sa.Numeric(precision=12, scale=2), nullable=False
        ),
        sa.Column("cost_holding", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cost_backlog", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("profit", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cash_open", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cash_end", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("futures_vs_spot", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("fill", sa.Double(), nullable=True),
        sa.Column("waste", sa.Double(), nullable=True),
        sa.Column("reputation_open", sa.Double(), nullable=False),
        sa.Column("reputation_end", sa.Double(), nullable=False),
        sa.Column("market_share", sa.Double(), nullable=True),
        sa.Column("refrigeration_failure", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name=op.f("fk_player_days_game_id_games"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_player_days")),
        sa.UniqueConstraint(
            "game_id", "day", "alias", name=op.f("uq_player_days_game_id_day_alias")
        ),
    )


def downgrade() -> None:
    op.drop_table("player_days")
    op.drop_index(op.f("ix_participants_user_id"), table_name="participants")
    op.drop_index(op.f("ix_participants_guest_identity"), table_name="participants")
    op.drop_table("participants")
    op.drop_table("market_days")
    op.drop_table("game_configs")
    op.drop_table("user_stats")
    op.drop_index(op.f("ix_games_room_code"), table_name="games")
    op.drop_table("games")
    op.drop_table("users")
