# ruff: noqa: F811
"""Schema of spec 13.5 as migrated."""

from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alembic import command
from tests.fixtures.postgres import (  # noqa: F401
    alembic_config,
    pg_container,
    pg_url,
)

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
def engine(pg_url: str) -> Any:  # noqa: F811
    eng = create_engine(pg_url)
    yield eng
    eng.dispose()


def _column_type(
    data_type: str, length: int | None, precision: int | None, scale: int | None
) -> str:
    """``data_type`` with its length or precision, as ``format_type`` prints it."""
    if length is not None:
        return f"{data_type}({length})"
    if data_type == "numeric" and precision is not None:
        return f"numeric({precision},{scale})"
    return data_type


def columns(engine: Engine, table: str) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type, character_maximum_length, "
                "numeric_precision, numeric_scale, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).all()
    return {
        r[0]: {
            "type": _column_type(r[1], r[2], r[3], r[4]),
            "data_type": r[1],
            "nullable": r[5] == "YES",
            "default": r[6] or "",
        }
        for r in rows
    }


def indexes(engine: Engine, table: str) -> dict[str, tuple[bool, bool, list[str]]]:
    """index name -> (unique, primary, columns in index order)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT i.relname, x.indisunique, x.indisprimary, a.attname "
                "FROM pg_index x "
                "JOIN pg_class t ON t.oid = x.indrelid "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "CROSS JOIN LATERAL unnest(x.indkey::int2[]) "
                "WITH ORDINALITY AS k(attnum, ord) "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                "WHERE n.nspname = current_schema() AND t.relname = :t "
                "ORDER BY i.relname, k.ord"
            ),
            {"t": table},
        ).all()
    out: dict[str, tuple[bool, bool, list[str]]] = {}
    for name, unique, primary, col in rows:
        out.setdefault(name, (unique, primary, []))[2].append(col)
    return out


def primary_key(engine: Engine, table: str) -> list[str]:
    (cols,) = [c for _, primary, c in indexes(engine, table).values() if primary]
    return cols


_DELETE_RULES = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def foreign_keys(engine: Engine, table: str) -> dict[str, tuple[str, str, str]]:
    """column -> (referenced table, referenced column, delete rule)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.attname, rt.relname, ra.attname, c.confdeltype "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "JOIN pg_class rt ON rt.oid = c.confrelid "
                "JOIN pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                "JOIN pg_attribute ra "
                "ON ra.attrelid = c.confrelid AND ra.attnum = c.confkey[1] "
                "WHERE c.contype = 'f' AND n.nspname = current_schema() "
                "AND t.relname = :t"
            ),
            {"t": table},
        ).all()
    return {r[0]: (r[1], r[2], _DELETE_RULES[r[3]]) for r in rows}


def unique_column_sets(engine: Engine, table: str) -> list[list[str]]:
    return [cols for unique, _, cols in indexes(engine, table).values() if unique]


def indexed_leading_columns(engine: Engine, table: str) -> set[str]:
    return {cols[0] for _, _, cols in indexes(engine, table).values()}


# --- columns ------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(ALL_TABLES))
def test_table_has_exactly_the_spec_columns(engine: Engine, table: str) -> None:
    assert set(columns(engine, table)) == ALL_TABLES[table]


@pytest.mark.parametrize(
    "column", ["refrigeration_failure", "shortfall_spot", "peer_trades"]
)
def test_player_days_has_the_named_record_fields(engine: Engine, column: str) -> None:
    """A record field with no column cannot be stored."""
    assert column in columns(engine, "player_days")


def test_ids_are_bigserial_except_user_stats(engine: Engine) -> None:
    for table in ALL_TABLES:
        cols = columns(engine, table)
        if table == "user_stats":
            assert "id" not in cols
            assert primary_key(engine, table) == ["user_id"]
            continue
        assert cols["id"]["data_type"] == "bigint", table
        assert cols["id"]["default"].startswith("nextval("), table
        assert primary_key(engine, table) == ["id"], table


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
    """Money is NUMERIC(12,2), never a binary floating point."""
    assert columns(engine, table)[column]["type"] == "numeric(12,2)"


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
    assert columns(engine, table)[column]["data_type"] == "double precision"


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
        ("games", "public_id", "character(32)"),
        ("games", "room_code", "character(6)"),
        ("games", "rng_seed", "bigint"),
        ("games", "duration_days", "integer"),
        ("games", "ended_early", "boolean"),
        ("games", "host_display_name", "character varying(24)"),
        ("games", "started_at", "timestamp with time zone"),
        ("games", "finished_at", "timestamp with time zone"),
        ("participants", "alias", "character varying(4)"),
        ("participants", "display_name", "character varying(24)"),
        ("participants", "guest_identity", "character varying(64)"),
        ("participants", "units_sold", "integer"),
        ("participants", "final_rank", "integer"),
        ("users", "firebase_uid", "character varying(128)"),
        ("users", "display_name", "character varying(24)"),
        ("users", "email", "character varying(320)"),
        ("users", "photo_url", "character varying(1024)"),
        ("game_configs", "config", "json"),
        ("player_days", "conserve_lots", "json"),
        ("player_days", "peer_trades", "json"),
        ("player_days", "peer_buy", "json"),
        ("player_days", "peer_sell", "json"),
        ("player_days", "weekday", "character varying(3)"),
        ("player_days", "incoterm", "character varying(3)"),
        ("player_days", "alias", "character varying(4)"),
        ("player_days", "cooperate", "boolean"),
        ("player_days", "refrigeration_failure", "boolean"),
        ("player_days", "sold", "integer"),
        ("market_days", "weekday", "character varying(3)"),
        ("market_days", "disrupted", "boolean"),
        ("market_days", "cooperation", "boolean"),
        ("market_days", "refrigeration_failure", "boolean"),
        ("market_days", "capacity", "integer"),
        ("user_stats", "games_played", "integer"),
    ],
)
def test_column_types(
    engine: Engine, table: str, column: str, column_type: str
) -> None:
    actual = columns(engine, table)[column]["type"]
    assert actual == column_type, (actual, column_type)


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


# --- migration ----------------------------------------------------------------


def test_alembic_check_reports_no_drift(pg_url: str) -> None:  # noqa: F811
    """Catches a model column missing from the migration (or vice versa)."""
    command.check(alembic_config(pg_url))


def test_models_match_migrations(pg_url: str) -> None:  # noqa: F811
    import app.models.game  # noqa: F401
    import app.models.user  # noqa: F401
    from app.models.base import Base

    eng = create_engine(pg_url)
    try:
        with eng.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        eng.dispose()
    assert diff == []


SNAPSHOT = Path(__file__).resolve().parents[2] / "db" / "schema.postgres.sql"


def normalize(dump: str) -> str:
    """Drop everything in a pg_dump that varies between runs or pg_dump minors
    (the rule of Novus ``scripts/db/gen_ddl.py``, which writes the snapshot).

    Removed: comments, blank lines, session ``SET``/``set_config`` lines and the
    ``\\restrict``/``\\unrestrict`` guard lines (random token, pg_dump >= 17.6).
    """
    keep = []
    for line in dump.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("--")
            or stripped.startswith("\\restrict")
            or stripped.startswith("\\unrestrict")
            or stripped.startswith("SET ")
            or stripped.startswith("SELECT pg_catalog.set_config")
        ):
            continue
        keep.append(line.rstrip())
    return "\n".join(keep) + "\n"


def test_ddl_snapshot_matches_migrations(pg_container: Any) -> None:  # noqa: F811
    """``db/schema.postgres.sql`` is the DDL ``alembic upgrade head`` produces;
    regenerate it with ``scripts/db/gen_ddl.py`` after changing a migration."""
    result = pg_container.exec(
        [
            "pg_dump",
            "-U",
            pg_container.username,
            "-d",
            pg_container.dbname,
            "--schema-only",
            "--no-owner",
            "--no-privileges",
        ]
    )
    assert result.exit_code == 0, result.output[-2000:]
    assert normalize(result.output.decode()) == SNAPSHOT.read_text()


def test_alembic_check_detects_drift(pg_url: str) -> None:  # noqa: F811
    """The drift check can fail: an extra column in the database is reported."""
    eng = create_engine(pg_url)
    try:
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN drift_probe INT NULL"))
        with pytest.raises(CommandError):
            command.check(alembic_config(pg_url))
    finally:
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE users DROP COLUMN drift_probe"))
        eng.dispose()


def test_downgrade_removes_everything_and_upgrade_restores(
    pg_url: str,  # noqa: F811
) -> None:
    cfg = alembic_config(pg_url)
    eng = create_engine(pg_url)
    try:
        command.downgrade(cfg, "base")
        with eng.connect() as conn:
            left = set(
                conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                ).scalars()
            )
        assert not (left & set(ALL_TABLES))
        command.upgrade(cfg, "head")
        assert set(columns(eng, "player_days")) == PLAYER_DAYS_COLUMNS
    finally:
        command.upgrade(cfg, "head")
        eng.dispose()
