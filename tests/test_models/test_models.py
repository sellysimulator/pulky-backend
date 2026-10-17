# ruff: noqa: F811
"""Behaviour of the models and the migrated schema (section 17): AC4-AC7, AC9."""

import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.fixtures.mysql import db_session, mysql_url  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]

GAME_INSERT = text(
    "INSERT INTO games (public_id, room_code, rng_seed, duration_days, days_played, "
    "ended_early, player_count, started_at, finished_at, host_display_name) "
    "VALUES ('0123456789abcdef0123456789abcdef', 'ABC234', 42, 14, 14, 0, 4, "
    "'2026-10-12 14:00:00', '2026-10-12 15:00:00', 'Ms. Ruiz')"
)


def insert_row(session: Session, table: str, **values: Any) -> int:
    """Raw INSERT naming every NOT NULL column without a database default.

    Columns are read from ``information_schema`` and filled by type, so a test
    states only what it cares about. Never names ``created_at``/``updated_at``.
    """
    rows = session.execute(
        text(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM "
            "information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :t AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL "
            "AND EXTRA NOT LIKE '%auto_increment%'"
        ),
        {"t": table},
    ).all()
    filled: dict[str, Any] = {}
    for name, data_type, length in rows:
        if data_type in ("json",):
            filled[name] = "[]"
        elif data_type in ("char", "varchar"):
            filled[name] = ("x" * 3)[: int(length)]
        elif data_type in ("datetime", "timestamp"):
            filled[name] = datetime(2026, 10, 12, 14, 0, 0)
        else:
            filled[name] = 0
    filled.update(values)
    cols = ", ".join(f"`{c}`" for c in filled)
    marks = ", ".join(f":{c}" for c in filled)
    result = session.execute(
        text(f"INSERT INTO `{table}` ({cols}) VALUES ({marks})"), filled
    )
    session.commit()
    return int(result.lastrowid or 0)


def make_game(session: Session, room_code: str = "ABC234", **extra: Any) -> int:
    extra.setdefault("public_id", (room_code.lower() * 6)[:32].ljust(32, "0"))
    return insert_row(session, "games", room_code=room_code, **extra)


def count(session: Session, table: str, where: str = "1=1") -> int:
    return int(
        session.execute(text(f"SELECT COUNT(*) FROM `{table}` WHERE {where}")).scalar()
        or 0
    )


@pytest.mark.dbschema
def test_container_runs_in_strict_mode(db_session: Session) -> None:  # noqa: F811
    mode = db_session.execute(text("SELECT @@sql_mode")).scalar()
    assert "STRICT_ALL_TABLES" in str(mode)


# --- AC4 / FM1 ----------------------------------------------------------------


@pytest.mark.dbschema
def test_raw_insert_without_timestamps_succeeds_and_fills_both(
    db_session: Session,  # noqa: F811
) -> None:
    """Failure mode 1: no server_default makes this insert fail under strict mode."""
    db_session.execute(GAME_INSERT)
    db_session.commit()
    row = db_session.execute(text("SELECT created_at, updated_at FROM games")).one()
    assert row[0] is not None
    assert row[1] is not None


@pytest.mark.dbschema
@pytest.mark.parametrize(
    "table",
    ["users", "game_configs", "participants", "player_days", "market_days"],
)
def test_every_table_accepts_a_raw_insert_without_timestamps(
    db_session: Session,  # noqa: F811
    table: str,
) -> None:
    gid = make_game(db_session)
    uid = insert_row(db_session, "users", firebase_uid="uid-1", display_name="Ana")
    overrides: dict[str, Any] = {
        "users": {"firebase_uid": "uid-2", "display_name": "Bo"},
        "game_configs": {"game_id": gid, "config": "{}"},
        "participants": {"game_id": gid, "alias": "P1"},
        "player_days": {"game_id": gid, "alias": "P1"},
        "market_days": {"game_id": gid},
    }[table]
    insert_row(db_session, table, **overrides)
    row = db_session.execute(
        text(f"SELECT created_at, updated_at FROM `{table}` ORDER BY id DESC LIMIT 1")
    ).one()
    assert row[0] is not None
    assert row[1] is not None
    assert uid


@pytest.mark.dbschema
def test_user_stats_raw_insert_without_timestamps(
    db_session: Session,  # noqa: F811
) -> None:
    uid = insert_row(db_session, "users", firebase_uid="uid-s", display_name="Su")
    insert_row(db_session, "user_stats", user_id=uid)
    row = db_session.execute(
        text("SELECT created_at, updated_at FROM user_stats")
    ).one()
    assert row[0] is not None
    assert row[1] is not None


def test_every_model_table_carries_server_default_timestamps() -> None:
    """Failure mode 1, at the metadata level."""
    import app.models.game  # noqa: F401
    import app.models.user  # noqa: F401
    from app.models.base import Base

    assert len(Base.metadata.tables) >= 7
    for name, table in Base.metadata.tables.items():
        for col in ("created_at", "updated_at"):
            assert table.c[col].server_default is not None, (name, col)
            assert table.c[col].nullable is False, (name, col)


def test_public_surface_table_names() -> None:
    from app.models.game import (
        Game,
        GameConfigRow,
        MarketDayRow,
        Participant,
        PlayerDay,
        UserStats,
    )
    from app.models.user import User

    assert [
        m.__tablename__
        for m in (
            User,
            Game,
            GameConfigRow,
            Participant,
            PlayerDay,
            MarketDayRow,
            UserStats,
        )
    ] == [
        "users",
        "games",
        "game_configs",
        "participants",
        "player_days",
        "market_days",
        "user_stats",
    ]


# --- AC5 / FM4 ----------------------------------------------------------------


@pytest.mark.dbschema
def test_same_room_code_and_start_under_two_ids_are_both_stored(
    db_session: Session,
) -> None:  # noqa: F811
    """Failure mode 4: a game is keyed by its public_id alone; a reissued room code,
    even at the same start, is another game."""
    db_session.execute(GAME_INSERT)
    db_session.execute(
        text(
            str(GAME_INSERT).replace(
                "0123456789abcdef0123456789abcdef",
                "fedcba9876543210fedcba9876543210",
            )
        )
    )
    db_session.commit()
    assert count(db_session, "games") == 2


@pytest.mark.dbschema
def test_same_room_code_at_another_start_is_allowed(
    db_session: Session,  # noqa: F811
) -> None:
    make_game(db_session, started_at=datetime(2026, 10, 12, 14))
    make_game(db_session, started_at=datetime(2026, 10, 13, 14), public_id="b" * 32)
    assert count(db_session, "games", "room_code = 'ABC234'") == 2


@pytest.mark.dbschema
def test_duplicate_public_id_is_rejected(db_session: Session) -> None:  # noqa: F811
    make_game(db_session, "AAA222")
    with pytest.raises(IntegrityError):
        make_game(db_session, "BBB333", public_id=(("aaa222" * 6)[:32]))


@pytest.mark.dbschema
def test_duplicate_firebase_uid_is_rejected(db_session: Session) -> None:  # noqa: F811
    insert_row(db_session, "users", firebase_uid="same", display_name="A")
    with pytest.raises(IntegrityError):
        insert_row(db_session, "users", firebase_uid="same", display_name="B")


@pytest.mark.dbschema
def test_duplicate_config_participant_player_day_market_day_rejected(
    db_session: Session,  # noqa: F811
) -> None:
    gid = make_game(db_session)
    cases = [
        ("game_configs", {"game_id": gid, "config": "{}"}),
        ("participants", {"game_id": gid, "alias": "P1"}),
        ("player_days", {"game_id": gid, "alias": "P1", "day": 1}),
        ("market_days", {"game_id": gid, "day": 1}),
    ]
    for table, values in cases:
        insert_row(db_session, table, **values)
        with pytest.raises(IntegrityError):
            insert_row(db_session, table, **values)
        db_session.rollback()
        assert count(db_session, table) == 1, table


@pytest.mark.dbschema
def test_distinct_keys_do_not_collide(db_session: Session) -> None:  # noqa: F811
    gid = make_game(db_session)
    for alias, day in [("P1", 1), ("P1", 2), ("P2", 1)]:
        insert_row(db_session, "player_days", game_id=gid, alias=alias, day=day)
    assert count(db_session, "player_days") == 3


# --- AC6 ----------------------------------------------------------------------


@pytest.mark.dbschema
def test_deleting_a_game_cascades(db_session: Session) -> None:  # noqa: F811
    gid = make_game(db_session)
    other = make_game(db_session, "ZZZ999")
    for table, values in [
        ("game_configs", {"config": "{}"}),
        ("participants", {"alias": "P1"}),
        ("player_days", {"alias": "P1", "day": 1}),
        ("market_days", {"day": 1}),
    ]:
        insert_row(db_session, table, game_id=gid, **values)
        insert_row(db_session, table, game_id=other, **values)
    db_session.execute(text("DELETE FROM games WHERE id = :g"), {"g": gid})
    db_session.commit()
    for table in ("game_configs", "participants", "player_days", "market_days"):
        assert count(db_session, table, f"game_id = {gid}") == 0, table
        assert count(db_session, table, f"game_id = {other}") == 1, table


@pytest.mark.dbschema
def test_deleting_a_user_nulls_references(db_session: Session) -> None:  # noqa: F811
    uid = insert_row(db_session, "users", firebase_uid="u1", display_name="Ana")
    gid = make_game(db_session, host_user_id=uid)
    insert_row(db_session, "participants", game_id=gid, alias="P1", user_id=uid)
    db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    db_session.commit()
    assert count(db_session, "games", "host_user_id IS NULL") == 1
    assert count(db_session, "participants", "user_id IS NULL") == 1
    assert count(db_session, "games") == 1
    assert count(db_session, "participants") == 1


# --- AC7 ----------------------------------------------------------------------


@pytest.mark.dbschema
def test_money_through_the_orm(db_session: Session) -> None:  # noqa: F811
    from app.models.game import UserStats

    uid = insert_row(db_session, "users", firebase_uid="u-m", display_name="Mo")
    db_session.add(
        UserStats(
            user_id=uid,
            games_played=1,
            wins=0,
            total_profit=Decimal("1234.565"),
            best_profit=Decimal("1234.56"),
            avg_fill_rate=0.5,
            avg_waste_rate=0.25,
            avg_market_share=0.125,
        )
    )
    db_session.commit()
    db_session.expire_all()
    row = db_session.get(UserStats, uid)
    assert row is not None
    assert row.total_profit in (Decimal("1234.57"), Decimal("1234.56"))
    assert row.best_profit == Decimal("1234.56")
    assert row.avg_fill_rate == 0.5


@pytest.mark.dbschema
def test_two_decimal_floats_read_back_exactly(
    db_session: Session,
) -> None:  # noqa: F811
    from app.models.game import Game, Participant

    game = Game(
        public_id="c" * 32,
        room_code="CCC333",
        rng_seed=2**40,
        duration_days=14,
        days_played=14,
        ended_early=False,
        player_count=1,
        started_at=datetime(2026, 10, 12, 14),
        finished_at=datetime(2026, 10, 12, 15),
        host_display_name="Ms. Ruiz",
    )
    db_session.add(game)
    db_session.flush()
    values = [0.1, 1234.56, 99999999.99, -17.05, 0.07]
    for i, v in enumerate(values):
        db_session.add(
            Participant(
                game_id=game.id,
                alias=f"P{i}",
                display_name="Ana",
                final_rank=i + 1,
                total_profit=v,
                units_sold=0,
                units_spoiled=0,
                units_lost_transport=0,
                overall_market_share=None,
                fill_rate=None,
                waste_rate=None,
                avg_reputation=0.5,
                final_reputation=0.5,
                futures_units=0,
                cost_insurance=0,
                refund_insurance=0,
                peer_units_bought=0,
                peer_units_sold=0,
                cost_peer=0,
                revenue_peer=0,
                liquidated_units=0,
                revenue_liquidation=0,
                refund_insurance_in_transit=0,
                futures_cancelled_units=0,
                refund_futures_cancelled=0,
            )
        )
    db_session.commit()
    db_session.expire_all()
    got = [
        float(r.total_profit)
        for r in db_session.query(Participant).order_by(Participant.final_rank)
    ]
    assert got == values
    assert db_session.get(Game, game.id).rng_seed == 2**40  # type: ignore[union-attr]


# --- FM6 ----------------------------------------------------------------------


@pytest.mark.dbschema
def test_no_trade_day_rows_insert_under_strict_mode(
    db_session: Session,  # noqa: F811
) -> None:
    """Failure mode 6: NULL peer_avg_price / peer_buy / peer_sell must be accepted."""
    gid = make_game(db_session)
    insert_row(db_session, "market_days", game_id=gid, day=1, peer_avg_price=None)
    insert_row(
        db_session,
        "player_days",
        game_id=gid,
        alias="P1",
        day=1,
        peer_buy=None,
        peer_sell=None,
        peer_trades="[]",
    )
    assert count(db_session, "market_days", "peer_avg_price IS NULL") == 1
    assert (
        count(db_session, "player_days", "peer_buy IS NULL AND peer_sell IS NULL") == 1
    )
    trades = db_session.execute(text("SELECT peer_trades FROM player_days")).scalar()
    assert trades in ("[]", [])


@pytest.mark.dbschema
def test_json_columns_round_trip(db_session: Session) -> None:  # noqa: F811
    import json

    gid = make_game(db_session)
    trade = [{"side": "buy", "counterparty": "P2", "lot_id": 3, "qty": 5,
              "quality": 0.9, "price": 1.25}]  # fmt: skip
    insert_row(
        db_session,
        "player_days",
        game_id=gid,
        alias="P1",
        day=1,
        peer_trades=json.dumps(trade),
        peer_buy=json.dumps({"max_price": 2.0, "qty": 4}),
    )
    row = db_session.execute(
        text("SELECT peer_trades, peer_buy FROM player_days")
    ).one()
    loaded = [json.loads(v) if isinstance(v, str) else v for v in row]
    assert loaded[0] == trade
    assert loaded[1] == {"max_price": 2.0, "qty": 4}


# --- AC9 ----------------------------------------------------------------------


def test_nothing_in_app_calls_create_all() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "app").rglob("*.py")
        if re.search(r"create_all\s*\(", p.read_text())
    ]
    assert offenders == []
