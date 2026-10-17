# ruff: noqa: F811
"""Schema of spec 13.5 as migrated (section 17): AC1, AC2, AC3, AC8, FM2, FM3, FM5."""

from typing import Any

import pytest
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alembic import command
from tests.fixtures.mysql import alembic_config, mysql_url  # noqa: F401

pytestmark = pytest.mark.dbschema

TIMESTAMPS = {"created_at", "updated_at"}

PLAYER_DAYS_MONEY = [
    "revenue_sales", "revenue_salvage", "revenue_peer", "refund_insurance",
    "cost_spot", "cost_futures", "cost_peer", "refund_futures", "cost_transport",
    "cost_insurance", "cost_conservation", "cost_holding", "cost_backlog",
    "profit", "cash_open", "cash_end", "futures_vs_spot",
]  # fmt: skip
PLAYER_DAYS_DECISION = [
    "order_spot", "order_futures", "incoterm", "conserve_lots", "lots_conserved",
    "cooperate", "peer_buy", "peer_sell", "outlay",
]  # fmt: skip
PLAYER_DAYS_FLOW = [
    "opening_stock", "futures_due", "futures_filled", "futures_unfilled",
    "allocated_spot", "shortfall_spot", "shipped", "lost_transport", "delivered",
    "peer_bought", "peer_sold", "peer_trades", "demand", "backlog_open", "sold",
    "backlog_end", "spoiled", "closing_stock",
]  # fmt: skip
PLAYER_DAYS_OTHER = [
    "fill", "waste", "reputation_open", "reputation_end", "market_share",
    "refrigeration_failure",
]  # fmt: skip
PLAYER_DAYS_COLUMNS = (
    {"id", "game_id", "day", "alias", "weekday"}
    | set(PLAYER_DAYS_DECISION)
    | set(PLAYER_DAYS_FLOW)
    | set(PLAYER_DAYS_MONEY)
    | set(PLAYER_DAYS_OTHER)
    | TIMESTAMPS
)
MARKET_DAYS_COLUMNS = {
    "id", "game_id", "day", "weekday", "weekday_multiplier", "expected_demand",
    "demand_total", "capacity", "disrupted", "spot_price", "cooperation",
    "cooperators", "refrigeration_failure", "units_sold_total", "peer_units",
    "peer_avg_price",
} | TIMESTAMPS  # fmt: skip
PARTICIPANTS_COLUMNS = {
    "id", "game_id", "alias", "display_name", "user_id", "guest_identity",
    "final_rank", "total_profit", "units_sold", "units_spoiled",
    "units_lost_transport", "overall_market_share", "fill_rate", "waste_rate",
    "avg_reputation", "final_reputation", "futures_units", "cost_insurance",
    "refund_insurance", "peer_units_bought", "peer_units_sold", "cost_peer",
    "revenue_peer", "liquidated_units", "revenue_liquidation",
    "refund_insurance_in_transit", "futures_cancelled_units",
    "refund_futures_cancelled",
} | TIMESTAMPS  # fmt: skip
GAMES_COLUMNS = {
    "id", "public_id", "room_code", "host_user_id", "host_display_name",
    "rng_seed", "duration_days", "days_played", "ended_early", "player_count",
    "started_at", "finished_at",
} | TIMESTAMPS  # fmt: skip
USERS_COLUMNS = {
    "id",
    "firebase_uid",
    "display_name",
    "email",
    "photo_url",
} | TIMESTAMPS
GAME_CONFIGS_COLUMNS = {"id", "game_id", "config_schema_version", "config"} | TIMESTAMPS
USER_STATS_COLUMNS = {
    "user_id", "games_played", "wins", "total_profit", "best_profit",
    "avg_fill_rate", "avg_waste_rate", "avg_market_share",
} | TIMESTAMPS  # fmt: skip

ALL_TABLES = {
    "users": USERS_COLUMNS,
    "games": GAMES_COLUMNS,
    "game_configs": GAME_CONFIGS_COLUMNS,
    "participants": PARTICIPANTS_COLUMNS,
    "player_days": PLAYER_DAYS_COLUMNS,
    "market_days": MARKET_DAYS_COLUMNS,
    "user_stats": USER_STATS_COLUMNS,
}


@pytest.fixture()
def engine(mysql_url: str) -> Any:  # noqa: F811
    eng = create_engine(mysql_url)
    yield eng
    eng.dispose()


def columns(engine: Engine, table: str) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, EXTRA "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ),
            {"t": table},
        ).all()
    return {
        r[0].lower() if isinstance(r[0], str) else r[0]: {
            "type": str(r[1]).lower(),
            "data_type": str(r[2]).lower(),
            "nullable": r[3] == "YES",
            "extra": str(r[4]).lower(),
        }
        for r in rows
    }


def indexes(engine: Engine, table: str) -> dict[str, tuple[bool, list[str]]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM "
                "information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :t ORDER BY INDEX_NAME, SEQ_IN_INDEX"
            ),
            {"t": table},
        ).all()
    out: dict[str, tuple[bool, list[str]]] = {}
    for name, non_unique, col in rows:
        unique, cols = out.setdefault(name, (not non_unique, []))
        cols.append(col)
    return out


def foreign_keys(engine: Engine, table: str) -> dict[str, tuple[str, str, str]]:
    """column -> (referenced table, referenced column, delete rule)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT k.COLUMN_NAME, k.REFERENCED_TABLE_NAME, "
                "k.REFERENCED_COLUMN_NAME, r.DELETE_RULE "
                "FROM information_schema.KEY_COLUMN_USAGE k "
                "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
                "ON r.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
                "AND r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
                "WHERE k.TABLE_SCHEMA = DATABASE() AND k.TABLE_NAME = :t "
                "AND k.REFERENCED_TABLE_NAME IS NOT NULL"
            ),
            {"t": table},
        ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def unique_column_sets(engine: Engine, table: str) -> list[list[str]]:
    return [cols for unique, cols in indexes(engine, table).values() if unique]


def indexed_leading_columns(engine: Engine, table: str) -> set[str]:
    return {cols[0] for _, cols in indexes(engine, table).values()}


# --- AC1 / AC8 / FM5: columns -------------------------------------------------


@pytest.mark.parametrize("table", sorted(ALL_TABLES))
def test_table_has_exactly_the_spec_columns(engine: Engine, table: str) -> None:
    assert set(columns(engine, table)) == ALL_TABLES[table]


@pytest.mark.parametrize(
    "column", ["refrigeration_failure", "shortfall_spot", "peer_trades"]
)
def test_player_days_has_the_named_record_fields(engine: Engine, column: str) -> None:
    """Failure mode 5: a record field with no column cannot be stored."""
    assert column in columns(engine, "player_days")


def test_ids_are_bigint_autoincrement_except_user_stats(engine: Engine) -> None:
    for table in ALL_TABLES:
        cols = columns(engine, table)
        if table == "user_stats":
            assert "id" not in cols
            assert indexes(engine, table)["PRIMARY"][1] == ["user_id"]
            continue
        assert cols["id"]["data_type"] == "bigint", table
        assert "auto_increment" in cols["id"]["extra"], table
        assert indexes(engine, table)["PRIMARY"][1] == ["id"], table


# --- column types -------------------------------------------------------------

MONEY = {
    "player_days": PLAYER_DAYS_MONEY,
    "participants": [
        "total_profit", "cost_insurance", "refund_insurance", "cost_peer",
        "revenue_peer", "revenue_liquidation", "refund_insurance_in_transit",
        "refund_futures_cancelled",
    ],  # fmt: skip
    "market_days": ["spot_price", "peer_avg_price"],
    "user_stats": ["total_profit", "best_profit"],
}


@pytest.mark.parametrize(
    ("table", "column"),
    [(t, c) for t, cs in MONEY.items() for c in cs],
)
def test_money_columns_are_decimal_12_2(
    engine: Engine, table: str, column: str
) -> None:
    """Failure mode 3: money is DECIMAL(12,2), never FLOAT or DOUBLE."""
    assert columns(engine, table)[column]["type"] == "decimal(12,2)"


DOUBLES = {
    "participants": [
        "overall_market_share", "fill_rate", "waste_rate", "avg_reputation",
        "final_reputation",
    ],  # fmt: skip
    "player_days": [
        "fill",
        "waste",
        "reputation_open",
        "reputation_end",
        "market_share",
    ],
    "market_days": ["weekday_multiplier", "expected_demand"],
    "user_stats": ["avg_fill_rate", "avg_waste_rate", "avg_market_share"],
}


@pytest.mark.parametrize(
    ("table", "column"),
    [(t, c) for t, cs in DOUBLES.items() for c in cs],
)
def test_ratio_columns_are_double(engine: Engine, table: str, column: str) -> None:
    assert columns(engine, table)[column]["data_type"] == "double"


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("participants", "fill_rate"),
        ("participants", "waste_rate"),
        ("participants", "overall_market_share"),
        ("user_stats", "avg_fill_rate"),
        ("user_stats", "avg_waste_rate"),
        ("user_stats", "avg_market_share"),
        ("market_days", "peer_avg_price"),
        ("player_days", "peer_buy"),
        ("player_days", "peer_sell"),
        ("users", "email"),
        ("users", "photo_url"),
        ("participants", "user_id"),
        ("participants", "guest_identity"),
        ("games", "host_user_id"),
    ],
)
def test_nullable_columns(engine: Engine, table: str, column: str) -> None:
    assert columns(engine, table)[column]["nullable"] is True


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("player_days", "peer_trades"),
        ("player_days", "conserve_lots"),
        ("game_configs", "config"),
        ("games", "public_id"),
        ("games", "room_code"),
        ("games", "started_at"),
        ("games", "finished_at"),
        ("users", "firebase_uid"),
        ("participants", "alias"),
        ("player_days", "profit"),
        ("games", "created_at"),
    ],
)
def test_not_null_columns(engine: Engine, table: str, column: str) -> None:
    assert columns(engine, table)[column]["nullable"] is False


@pytest.mark.parametrize(
    ("table", "column", "column_type"),
    [
        ("games", "public_id", "char(32)"),
        ("games", "room_code", "char(6)"),
        ("games", "rng_seed", "bigint"),
        ("games", "duration_days", "int"),
        ("games", "ended_early", "tinyint(1)"),
        ("games", "host_display_name", "varchar(24)"),
        ("games", "started_at", "datetime"),
        ("games", "finished_at", "datetime"),
        ("participants", "alias", "varchar(4)"),
        ("participants", "display_name", "varchar(24)"),
        ("participants", "guest_identity", "varchar(64)"),
        ("participants", "units_sold", "int"),
        ("participants", "final_rank", "int"),
        ("users", "firebase_uid", "varchar(128)"),
        ("users", "display_name", "varchar(24)"),
        ("users", "email", "varchar(320)"),
        ("users", "photo_url", "varchar(1024)"),
        ("game_configs", "config", "json"),
        ("player_days", "conserve_lots", "json"),
        ("player_days", "peer_trades", "json"),
        ("player_days", "peer_buy", "json"),
        ("player_days", "peer_sell", "json"),
        ("player_days", "weekday", "varchar(3)"),
        ("player_days", "incoterm", "varchar(3)"),
        ("player_days", "alias", "varchar(4)"),
        ("player_days", "cooperate", "tinyint(1)"),
        ("player_days", "refrigeration_failure", "tinyint(1)"),
        ("player_days", "sold", "int"),
        ("market_days", "weekday", "varchar(3)"),
        ("market_days", "disrupted", "tinyint(1)"),
        ("market_days", "cooperation", "tinyint(1)"),
        ("market_days", "refrigeration_failure", "tinyint(1)"),
        ("market_days", "capacity", "int"),
        ("user_stats", "games_played", "int"),
    ],
)
def test_column_types(
    engine: Engine, table: str, column: str, column_type: str
) -> None:
    actual = columns(engine, table)[column]["type"]
    assert actual.startswith(column_type), (actual, column_type)


# --- keys and indexes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("game_configs", ["game_id"]),
        ("participants", ["game_id", "alias"]),
        ("player_days", ["game_id", "day", "alias"]),
        ("market_days", ["game_id", "day"]),
        ("games", ["public_id"]),
        ("users", ["firebase_uid"]),
    ],
)
def test_unique_constraints(engine: Engine, table: str, expected: list[str]) -> None:
    assert expected in unique_column_sets(engine, table)


def test_room_code_is_indexed_but_never_part_of_a_unique_key(engine: Engine) -> None:
    for columns in unique_column_sets(engine, "games"):
        assert "room_code" not in columns
    assert "room_code" in indexed_leading_columns(engine, "games")


def test_secondary_indexes(engine: Engine) -> None:
    assert "user_id" in indexed_leading_columns(engine, "participants")
    assert "guest_identity" in indexed_leading_columns(engine, "participants")


@pytest.mark.parametrize(
    ("table", "column", "target", "rule"),
    [
        ("game_configs", "game_id", "games", "CASCADE"),
        ("participants", "game_id", "games", "CASCADE"),
        ("player_days", "game_id", "games", "CASCADE"),
        ("market_days", "game_id", "games", "CASCADE"),
        ("games", "host_user_id", "users", "SET NULL"),
        ("participants", "user_id", "users", "SET NULL"),
    ],
)
def test_foreign_keys(
    engine: Engine, table: str, column: str, target: str, rule: str
) -> None:
    fk = foreign_keys(engine, table)[column]
    assert fk[0] == target
    assert fk[1] == "id"
    assert fk[2] == rule


def test_user_stats_references_users(engine: Engine) -> None:
    assert foreign_keys(engine, "user_stats")["user_id"][0] == "users"


# --- AC2 / AC3 / FM2: migration -----------------------------------------------


def test_alembic_check_reports_no_drift(mysql_url: str) -> None:  # noqa: F811
    """Failure mode 2: a model column missing from the migration (or vice versa)."""
    command.check(alembic_config(mysql_url))


def test_alembic_check_detects_drift(mysql_url: str) -> None:  # noqa: F811
    """The drift check can fail: an extra column in the database is reported."""
    eng = create_engine(mysql_url)
    try:
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN drift_probe INT NULL"))
        with pytest.raises(CommandError):
            command.check(alembic_config(mysql_url))
    finally:
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE users DROP COLUMN drift_probe"))
        eng.dispose()


def test_downgrade_removes_everything_and_upgrade_restores(
    mysql_url: str,  # noqa: F811
) -> None:
    cfg = alembic_config(mysql_url)
    eng = create_engine(mysql_url)
    try:
        command.downgrade(cfg, "base")
        with eng.connect() as conn:
            left = set(
                conn.execute(
                    text(
                        "SELECT TABLE_NAME FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = DATABASE()"
                    )
                ).scalars()
            )
        assert not (left & set(ALL_TABLES))
        command.upgrade(cfg, "head")
        assert set(columns(eng, "player_days")) == PLAYER_DAYS_COLUMNS
    finally:
        command.upgrade(cfg, "head")
        eng.dispose()
