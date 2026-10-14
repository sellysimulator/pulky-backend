# ruff: noqa: F811
"""Black-box tests for section 18: end-of-game persistence (all against MySQL).

Every test passes a ``session_factory`` bound to the Testcontainers MySQL of
section 17. Nothing here ever uses the default session factory.
"""

import copy
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.engine import PulkyEngine
from app.core.enums import Incoterm, TradeSide
from app.core.records import Decision, PeerBuy, PeerSell
from app.models.game import Game
from app.services.db_service import (
    StoredGame,
    find_game_by_public_id,
    find_latest_game_by_room_code,
    load_stored_game,
    persist_finished_game,
)
from app.services.state_service import new_room_document
from app.services.stats_service import get_or_create_user, rebuild_user_stats
from tests.fixtures.mysql import db_session, mysql_url  # noqa: F401

pytestmark = pytest.mark.dbschema

NAMES = [
    "Don Agave",
    "La Chula",
    "El Tinacal",
    "Casa Maguey",
    "La Penca",
    "El Jarro",
    "Las Pencas",
    "El Tlachiquero",
    "La Mixiotera",
    "El Aguamiel",
    "La Mayahuel",
    "El Xoconostle",
]
QUIET = {
    "supplier__spot_spread": 0,
    "supplier__capacity_sd_per_player": 0,
    "supplier__disruption_probability": 0,
    "transport__failure_probability": 0,
}
TABLES = ["games", "game_configs", "participants", "player_days", "market_days"]
HOST_SECRET = "host-secret-ZZTOP-0001"
STARTED = "2026-10-12T14:00:00Z"
FINISHED = "2026-10-12T15:00:00Z"


# ------------------------------------------------------------------ builders


def make_config(**over: Any) -> GameConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    for path, value in over.items():
        keys = path.split("__")
        node = data
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return GameConfig.from_dict(data)


def peer_config(players: int = 2, **over: Any) -> GameConfig:
    return make_config(
        max_players=players,
        duration_days=7,
        futures__horizon_days=3,
        supplier__capacity_mean_per_player=10,
        supplier__quality_min=100,
        supplier__lead_time_days=0,
        **{**QUIET, "transport__failure_probability": 0.5, **over},
    )


def seats(n: int) -> dict[str, str]:
    return {f"P{i + 1}": NAMES[i] for i in range(n)}


def play_peer_game(
    *, seed: int = 11, early: bool = False, peer_enabled: bool = True
) -> PulkyEngine:
    """Two players, seed-fixed. Day 1 has no peer limits and one conserved lot.

    Day 2 has P1 short with buy limits and P2 selling (peer trades). A
    refrigeration failure strikes on day 3 (seed 11). With ``early`` the game
    ends after day 3; otherwise it runs its seven days.
    """
    over = {} if peer_enabled else {"peer_market__enabled": False}
    engine = PulkyEngine.start(peer_config(**over), seats(2), seed)
    last = 3 if early else 7
    for day in range(1, last + 1):
        if day == 1:
            plan = {
                "P1": Decision(1, 10, 0, Incoterm.CIP, ("L0",), False),
                "P2": Decision(1, 30, 0, Incoterm.DAP, (), True),
            }
        elif day == 2 and peer_enabled:
            plan = {
                "P1": Decision(
                    2, 40, 0, Incoterm.EXW, (), False, PeerBuy(20.0, 50), None
                ),
                "P2": Decision(
                    2, 0, 0, Incoterm.EXW, (), False, None, PeerSell(15.0, 100, 25)
                ),
            }
        elif day == 2:
            plan = {"P1": Decision(2, 40, 0, Incoterm.EXW, (), False)}
        else:
            plan = {}
        for alias in engine.aliases:
            engine.submit(alias, plan.get(alias, Decision.zero(day)))
        engine.resolve_day()
    if early:
        engine.end_early()
    assert engine.phase == "finished"
    return engine


def play_big_game(players: int = 12, days: int = 20) -> PulkyEngine:
    config = make_config(max_players=players, duration_days=days, **QUIET)
    engine = PulkyEngine.start(config, seats(players), 5)
    for day in range(1, days + 1):
        for alias in engine.aliases:
            spot = 8 if day <= days - 3 else 0  # spot orders close before the end
            engine.submit(alias, Decision(day, spot, 0, Incoterm.EXW, (), False))
        engine.resolve_day()
    return engine


def guest_id(n: int) -> str:
    return f"guest_{n:032x}"


def make_room(
    engine: PulkyEngine,
    *,
    code: str = "ABC234",
    identities: dict[str, str] | None = None,
    host_identity: str | None = None,
    started_at: str = STARTED,
    finished_at: str = FINISHED,
    host_name: str = "Ms. Ruiz",
) -> dict[str, Any]:
    """A finished room document. Default: every player and the host are guests."""
    room = new_room_document(
        room_code=code,
        config=engine.config.to_dict(),
        host_display_name=host_name,
        host_secret=HOST_SECRET,
        created_at=started_at,
    )
    ids = identities or {a: guest_id(i + 1) for i, a in enumerate(engine.aliases)}
    room["state"] = "finished"
    room["started_at"] = started_at
    room["finished_at"] = finished_at
    room["host_identity"] = host_identity or guest_id(9999)
    room["seed"] = engine.seed
    room["participants"] = {
        alias: {
            "alias": alias,
            "identity": ids[alias],
            "session_token": f"session-token-secret-{alias}",
            "display_name": engine.display_names[alias],
            "connected": False,
        }
        for alias in engine.aliases
    }
    room["engine"] = engine.to_payload()
    return room


def secrets_of(room: dict[str, Any]) -> list[str]:
    out = [HOST_SECRET, str(room["host_identity"])]
    for p in room["participants"].values():
        out += [p["identity"], p["session_token"]]
    return out


# ------------------------------------------------------------------- helpers


@pytest.fixture()
def db_engine(mysql_url: str) -> Iterator[Engine]:
    engine = create_engine(mysql_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def factory(db_engine: Engine) -> Callable[[], Session]:
    return sessionmaker(bind=db_engine)


def rows(db: Session, sql: str, **params: Any) -> list[dict[str, Any]]:
    db.rollback()  # end the read snapshot so later reads see new commits
    return [dict(r) for r in db.execute(text(sql), params).mappings().all()]


def scalar(db: Session, sql: str, **params: Any) -> Any:
    db.rollback()
    return db.execute(text(sql), params).scalar()


def count(db: Session, table: str, where: str = "1=1") -> int:
    return int(scalar(db, f"SELECT COUNT(*) FROM `{table}` WHERE {where}"))


def table_counts(db: Session) -> dict[str, int]:
    return {t: count(db, t) for t in TABLES}


def jv(value: Any) -> Any:
    """A JSON column as Python (a driver may return text)."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def num(value: Any) -> Any:
    return float(value) if isinstance(value, Decimal) else value


def insert_table(statement: str) -> str | None:
    m = re.match(r"\s*INSERT\s+(?:IGNORE\s+)?INTO\s+`?(\w+)`?", statement, re.I)
    return m.group(1).lower() if m else None


class Recorder:
    """Records every statement an engine executes (before_cursor_execute)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.statements: list[str] = []
        event.listen(engine, "before_cursor_execute", self._on)

    def _on(self, conn: Any, cursor: Any, statement: Any, *args: Any) -> None:
        self.statements.append(str(statement))

    def stop(self) -> None:
        event.remove(self.engine, "before_cursor_execute", self._on)

    @property
    def inserts(self) -> list[str]:
        return [s for s in self.statements if insert_table(s)]

    def inserts_into(self, table: str) -> list[str]:
        return [s for s in self.statements if insert_table(s) == table]


def persist(room: dict[str, Any], factory: Callable[[], Session], db: Session) -> str:
    gid = persist_finished_game(room, factory)
    assert isinstance(gid, str)
    assert re.fullmatch(r"[0-9a-f]{32}", gid)
    return gid


def stored(db: Session, public_id: str) -> StoredGame:
    db.rollback()
    game = find_game_by_public_id(db, public_id)
    assert game is not None
    return load_stored_game(db, game.id)


@pytest.fixture(scope="module")
def peer_engine() -> PulkyEngine:
    return play_peer_game()


@pytest.fixture(scope="module")
def early_engine() -> PulkyEngine:
    return play_peer_game(early=True)


# ------------------------------------------------------------- fixture sanity


def test_fixture_game_has_the_scenario_it_claims(
    peer_engine: PulkyEngine, early_engine: PulkyEngine
) -> None:
    for engine in (peer_engine, early_engine):
        market = engine.market_history()
        assert market[0].peer_units == 0 and market[0].peer_avg_price is None
        assert market[1].peer_units > 0 and market[1].peer_avg_price is not None
        assert market[2].refrigeration_failure is True
        p1, p2 = engine.records("P1"), engine.records("P2")
        assert p1[0].peer_buy is None and p1[0].peer_sell is None
        assert p1[0].conserve_lots == ("L0",) and p1[0].lots_conserved == 1
        assert p1[1].peer_buy is not None and p2[1].peer_sell is not None
        assert {t.side for t in p1[1].peer_trades} == {TradeSide.BUY}
        assert {t.side for t in p2[1].peer_trades} == {TradeSide.SELL}
    assert early_engine.ended_early and not peer_engine.ended_early
    assert len(early_engine.market_history()) == 3
    assert len(peer_engine.market_history()) == 7


# ------------------------------------------------------------------ AC 1, 5


def test_ac1_a_finished_room_is_written_as_specified(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    room = make_room(peer_engine)
    gid = persist(room, factory, db_session)

    assert table_counts(db_session) == {
        "games": 1,
        "game_configs": 1,
        "participants": 2,
        "player_days": 2 * 7,
        "market_days": 7,
    }
    (g,) = rows(db_session, "SELECT * FROM games")
    assert g["public_id"] == gid
    assert g["room_code"] == "ABC234"
    assert g["host_display_name"] == "Ms. Ruiz"
    assert g["rng_seed"] == peer_engine.seed
    assert g["duration_days"] == 7 and g["days_played"] == 7
    assert not g["ended_early"] and g["player_count"] == 2
    assert g["started_at"].replace(tzinfo=None) == datetime(2026, 10, 12, 14, 0, 0)
    assert g["finished_at"].replace(tzinfo=None) == datetime(2026, 10, 12, 15, 0, 0)
    assert g["created_at"] is not None and g["updated_at"] is not None

    (cfg_row,) = rows(db_session, "SELECT * FROM game_configs")
    assert cfg_row["game_id"] == g["id"]
    assert cfg_row["config_schema_version"] == 1
    assert jv(cfg_row["config"]) == room["config"]


def test_ac1_participants_carry_standings_and_settlement(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    persist(make_room(peer_engine), factory, db_session)
    standings = peer_engine.standings()["players"]
    settlements = peer_engine.settlements()
    found = {r["alias"]: r for r in rows(db_session, "SELECT * FROM participants")}
    assert set(found) == {"P1", "P2"}
    stat_cols = [
        "total_profit",
        "units_sold",
        "units_spoiled",
        "units_lost_transport",
        "overall_market_share",
        "fill_rate",
        "waste_rate",
        "avg_reputation",
        "final_reputation",
        "futures_units",
        "cost_insurance",
        "refund_insurance",
        "peer_units_bought",
        "peer_units_sold",
        "cost_peer",
        "revenue_peer",
    ]
    settle_cols = [
        "liquidated_units",
        "revenue_liquidation",
        "refund_insurance_in_transit",
        "futures_cancelled_units",
        "refund_futures_cancelled",
    ]
    for alias, row in found.items():
        want = standings[alias]
        assert row["display_name"] == want["display_name"]
        assert row["final_rank"] == want["rank"]
        for col in stat_cols:
            if want[col] is None:
                assert row[col] is None, col
            else:
                assert num(row[col]) == pytest.approx(want[col], abs=0.005), col
        for col in settle_cols:
            assert num(row[col]) == pytest.approx(
                getattr(settlements[alias], col), abs=0.005
            ), col
    assert {r["final_rank"] for r in found.values()} == {1, 2}
    assert found["P1"]["peer_units_bought"] > 0 and found["P2"]["peer_units_sold"] > 0


def test_ac1_player_days_and_market_days_match_the_engine(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    persist(make_room(peer_engine), factory, db_session)
    for alias in peer_engine.aliases:
        stored_days = rows(
            db_session,
            "SELECT pd.* FROM player_days pd WHERE pd.alias = :a ORDER BY pd.day",
            a=alias,
        )
        records = peer_engine.records(alias)
        assert [r["day"] for r in stored_days] == [r.day for r in records]
        for row, rec in zip(stored_days, records, strict=True):
            assert row["weekday"] == str(rec.weekday)
            assert row["incoterm"] == str(rec.incoterm)
            assert row["sold"] == rec.sold and row["demand"] == rec.demand
            assert num(row["profit"]) == pytest.approx(rec.profit, abs=0.005)
    market = peer_engine.market_history()
    days = rows(db_session, "SELECT * FROM market_days ORDER BY day")
    assert [d["day"] for d in days] == [m.day for m in market]
    for row, m in zip(days, market, strict=True):
        assert row["demand_total"] == m.demand_total
        assert row["peer_units"] == m.peer_units


def test_ac5_signed_in_players_get_user_id_and_guests_get_guest_identity(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    room = make_room(
        peer_engine,
        identities={"P1": "firebase-uid-alice", "P2": guest_id(2)},
        host_identity="firebase-uid-host",
    )
    persist(room, factory, db_session)
    users = {
        r["firebase_uid"]: r["id"]
        for r in rows(db_session, "SELECT id, firebase_uid FROM users")
    }
    assert set(users) == {"firebase-uid-alice", "firebase-uid-host"}
    parts = {r["alias"]: r for r in rows(db_session, "SELECT * FROM participants")}
    assert parts["P1"]["user_id"] == users["firebase-uid-alice"]
    assert parts["P1"]["guest_identity"] is None
    assert parts["P2"]["user_id"] is None
    assert parts["P2"]["guest_identity"] == guest_id(2)
    (g,) = rows(db_session, "SELECT host_user_id FROM games")
    assert g["host_user_id"] == users["firebase-uid-host"]


def test_ac5_a_guest_host_has_no_host_user_id(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    persist(make_room(peer_engine), factory, db_session)
    (g,) = rows(db_session, "SELECT host_user_id FROM games")
    assert g["host_user_id"] is None


def test_fm5_a_guest_identity_never_creates_a_user(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    persist(make_room(peer_engine), factory, db_session)
    assert count(db_session, "users") == 0
    assert count(db_session, "participants", "guest_identity LIKE 'guest\\_%'") == 2
    assert count(db_session, "participants", "user_id IS NOT NULL") == 0


# ---------------------------------------------------------------------- AC 2


def test_ac2_persisting_twice_returns_the_same_id_and_writes_nothing(
    peer_engine: PulkyEngine,
    factory: Callable[[], Session],
    db_engine: Engine,
    db_session: Session,
) -> None:
    room = make_room(peer_engine)
    gid = persist(room, factory, db_session)
    before = table_counts(db_session)
    again_room = copy.deepcopy(room)
    rec = Recorder(db_engine)
    try:
        assert persist_finished_game(again_room, factory) == gid
    finally:
        rec.stop()
    assert rec.inserts_into("games") == []
    assert table_counts(db_session) == before
    assert before["games"] == 1


def test_ac2_two_concurrent_calls_produce_one_game(
    peer_engine: PulkyEngine,
    factory: Callable[[], Session],
    db_engine: Engine,
    db_session: Session,
) -> None:
    room = make_room(peer_engine)

    def widen_the_race(conn: Any, cursor: Any, statement: Any, *a: Any) -> None:
        # every caller sits between its lookup and its insert at the same time
        if re.match(r"\s*SELECT", str(statement), re.I) and "games" in str(statement):
            time.sleep(0.4)

    event.listen(db_engine, "before_cursor_execute", widen_the_race)
    results: list[Any] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        results.append(persist_finished_game(copy.deepcopy(room), factory))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
    finally:
        event.remove(db_engine, "before_cursor_execute", widen_the_race)
    assert len(results) == 4
    assert results[0] is not None and len(set(results)) == 1
    assert table_counts(db_session) == {
        "games": 1,
        "game_configs": 1,
        "participants": 2,
        "player_days": 14,
        "market_days": 7,
    }


# ------------------------------------------------------------- AC 3, FM 2, 7


def inject_failure(engine: Engine, table: str) -> Callable[[], None]:
    def boom(conn: Any, cursor: Any, statement: Any, *a: Any) -> None:
        if insert_table(str(statement)) == table:
            raise RuntimeError("injected failure")

    event.listen(engine, "before_cursor_execute", boom)
    return lambda: event.remove(engine, "before_cursor_execute", boom)


def test_ac3_fm3_a_failure_in_market_days_leaves_no_row_anywhere(
    peer_engine: PulkyEngine,
    factory: Callable[[], Session],
    db_engine: Engine,
    db_session: Session,
) -> None:
    room = make_room(peer_engine)
    remove = inject_failure(db_engine, "market_days")
    try:
        result = persist_finished_game(room, factory)
    finally:
        remove()
    assert result is None
    assert table_counts(db_session) == dict.fromkeys(TABLES, 0)
    # nothing blocks a later, healthy attempt
    gid = persist(room, factory, db_session)
    assert count(db_session, "games", f"public_id = '{gid}'") == 1
    assert count(db_session, "market_days") == 7


def test_fm2_fm7_an_injected_failure_is_logged_at_error_and_returns_none(
    peer_engine: PulkyEngine,
    factory: Callable[[], Session],
    db_engine: Engine,
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = make_room(peer_engine)
    caplog.set_level(logging.DEBUG)
    remove = inject_failure(db_engine, "player_days")
    try:
        result = persist_finished_game(room, factory)  # must not raise
    finally:
        remove()
    assert result is None  # not a string, not an error message
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a swallowed failure logs nothing"
    assert any(r.exc_info for r in errors), "the log carries no traceback"
    for record in errors:
        message = record.getMessage()
        for secret in secrets_of(room):
            assert secret not in message


def test_fm7_a_session_factory_that_fails_never_raises(
    peer_engine: PulkyEngine, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> Session:
        raise ConnectionError("db is down")

    caplog.set_level(logging.DEBUG)
    assert persist_finished_game(make_room(peer_engine), broken) is None
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_a_room_that_is_not_finished_is_refused_with_an_error_log(
    peer_engine: PulkyEngine,
    factory: Callable[[], Session],
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lobby = make_room(peer_engine)
    lobby["state"] = "lobby"
    no_engine = make_room(peer_engine)
    no_engine["engine"] = None
    caplog.set_level(logging.DEBUG)
    assert persist_finished_game(lobby, factory) is None
    assert persist_finished_game(no_engine, factory) is None
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) >= 2
    assert table_counts(db_session) == dict.fromkeys(TABLES, 0)
    for record in caplog.records:
        for secret in secrets_of(lobby):
            assert secret not in record.getMessage()


# ------------------------------------------------------------- AC 4, FM 1, 4


def test_ac4_fm4_insert_count_does_not_grow_with_players_or_days(
    factory: Callable[[], Session], db_engine: Engine, db_session: Session
) -> None:
    engine = play_big_game(players=12, days=20)
    identities = {
        a: (f"firebase-uid-{a}" if i < 4 else guest_id(i + 1))
        for i, a in enumerate(engine.aliases)
    }
    room = make_room(engine, identities=identities, host_identity="firebase-uid-host")
    rec = Recorder(db_engine)
    try:
        gid = persist_finished_game(room, factory)
    finally:
        rec.stop()
    assert gid is not None
    assert count(db_session, "player_days") == 240
    assert count(db_session, "market_days") == 20
    for table in TABLES:
        assert len(rec.inserts_into(table)) == 1, f"{table}: one INSERT per row"
    assert len(rec.inserts_into("users")) <= 5  # four players and the host
    assert len(rec.inserts_into("user_stats")) <= 4
    assert len(rec.inserts) <= 5 + 5 + 4


def test_fm1_raw_inserts_never_name_the_timestamp_columns(
    peer_engine: PulkyEngine,
    factory: Callable[[], Session],
    db_engine: Engine,
    db_session: Session,  # empties the tables afterwards
) -> None:
    room = make_room(
        peer_engine,
        identities={"P1": "firebase-uid-alice", "P2": guest_id(2)},
        host_identity="firebase-uid-host",
    )
    rec = Recorder(db_engine)
    try:
        assert persist_finished_game(room, factory) is not None
    finally:
        rec.stop()
    assert {insert_table(s) for s in rec.inserts} >= set(TABLES)
    # the five persistence tables are raw text() INSERTs; users go through the ORM
    for statement in rec.inserts:
        if insert_table(statement) in TABLES:
            assert "created_at" not in statement.lower()
            assert "updated_at" not in statement.lower()


# ---------------------------------------------------------------------- AC 9


def test_ac9_no_log_record_contains_an_identity_token_or_host_secret(
    peer_engine: PulkyEngine,
    factory: Callable[[], Session],
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = make_room(
        peer_engine,
        identities={"P1": "firebase-uid-alice", "P2": guest_id(2)},
        host_identity="firebase-uid-host",
    )
    caplog.set_level(logging.DEBUG)
    gid = persist(room, factory, db_session)
    persist_finished_game(room, factory)  # the idempotent path logs too
    for secret in secrets_of(room):
        assert secret not in caplog.text
    # spec 13.13 quotes this INFO message
    assert any(
        r.levelno == logging.INFO
        and r.getMessage() == f"room ABC234 persisted as {gid}"
        for r in caplog.records
    )


# --------------------------------------------------------- load_stored_game


def test_fm6_a_stored_and_reloaded_game_equals_the_engine_field_for_field(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    room = make_room(
        peer_engine,
        identities={"P1": "firebase-uid-alice", "P2": guest_id(2)},
    )
    g = stored(db_session, persist(room, factory, db_session))
    assert isinstance(g, StoredGame)
    assert g.room_code == "ABC234" and g.host_display_name == "Ms. Ruiz"
    assert g.duration_days == 7 and g.days_played == 7 and g.ended_early is False
    assert g.seed == peer_engine.seed
    assert g.started_at.replace(tzinfo=None) == datetime(2026, 10, 12, 14, 0, 0)
    assert g.finished_at.replace(tzinfo=None) == datetime(2026, 10, 12, 15, 0, 0)
    assert g.config == room["config"]
    assert g.display_names == peer_engine.display_names
    assert isinstance(g.user_ids["P1"], int) and g.user_ids["P2"] is None
    assert g.guest_identities == {"P1": None, "P2": guest_id(2)}

    assert set(g.records) == {"P1", "P2"}
    for alias in peer_engine.aliases:
        original = peer_engine.records(alias)
        assert len(g.records[alias]) == len(original)
        for loaded, want in zip(g.records[alias], original, strict=True):
            assert loaded == want, f"{alias} day {want.day}"
            assert isinstance(loaded.profit, float)
            assert isinstance(loaded.cash_end, float)
            assert isinstance(loaded.conserve_lots, tuple)
            assert isinstance(loaded.peer_trades, tuple)
    assert g.market == peer_engine.market_history()
    assert g.settlements == peer_engine.settlements()
    assert all(isinstance(m.spot_price, float) for m in g.market)

    # the scenario really exercised every field the bug would lose
    p1, p2 = g.records["P1"], g.records["P2"]
    assert p1[0].conserve_lots == ("L0",)
    assert p1[1].peer_buy == PeerBuy(20.0, 50) and p2[1].peer_sell is not None
    assert p1[1].peer_trades and p2[1].peer_trades
    assert any(r.refrigeration_failure for r in p1)
    assert {r.incoterm for r in p1} >= {Incoterm.CIP, Incoterm.EXW}
    assert g.market[0].peer_avg_price is None
    assert g.market[1].peer_avg_price is not None


def test_fm6_an_early_ended_game_round_trips_with_its_settlement(
    early_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    g = stored(db_session, persist(make_room(early_engine), factory, db_session))
    assert g.ended_early is True and g.days_played == 3 and g.duration_days == 7
    assert g.settlements == early_engine.settlements()
    assert g.market == early_engine.market_history()
    assert g.records["P1"] == early_engine.records("P1")


def test_fm8_days_without_limits_store_sql_null_not_json_null(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    gid = persist(make_room(peer_engine), factory, db_session)
    want_buy_null = sum(
        r.peer_buy is None for a in peer_engine.aliases for r in peer_engine.records(a)
    )
    want_sell_null = sum(
        r.peer_sell is None for a in peer_engine.aliases for r in peer_engine.records(a)
    )
    assert want_buy_null >= 2 and want_buy_null < 14
    assert count(db_session, "player_days", "peer_buy IS NULL") == want_buy_null
    assert count(db_session, "player_days", "peer_sell IS NULL") == want_sell_null
    # a stored JSON null would be NOT SQL NULL; the day-1 rows must be found by IS NULL
    assert count(db_session, "player_days", "day = 1 AND peer_buy IS NULL") == 2
    assert count(db_session, "player_days", "day = 1 AND peer_sell IS NULL") == 2
    assert count(db_session, "player_days", "JSON_TYPE(peer_buy) = 'NULL'") == 0
    assert count(db_session, "player_days", "JSON_TYPE(peer_sell) = 'NULL'") == 0
    assert (
        count(db_session, "player_days", "JSON_TYPE(peer_buy) = 'OBJECT'")
        == 14 - want_buy_null
    )
    # empty lists, not NULL and not JSON null
    assert count(db_session, "player_days", "JSON_LENGTH(peer_trades) = 0") == 12
    assert count(db_session, "player_days", "JSON_TYPE(conserve_lots) <> 'ARRAY'") == 0
    # market days: SQL NULL on a no-trade day
    no_trade = sum(m.peer_avg_price is None for m in peer_engine.market_history())
    assert no_trade == 6
    assert count(db_session, "market_days", "peer_avg_price IS NULL") == no_trade

    g = stored(db_session, gid)
    for record in g.records["P1"] + g.records["P2"]:
        assert record.peer_buy is None or isinstance(record.peer_buy, PeerBuy)
        assert record.peer_sell is None or isinstance(record.peer_sell, PeerSell)
    assert g.records["P1"][0].peer_buy is None
    assert g.records["P1"][0].peer_sell is None
    assert g.records["P2"][0].peer_buy is None
    assert [m.peer_avg_price is None for m in g.market] == [
        m.peer_avg_price is None for m in peer_engine.market_history()
    ]


def test_load_stored_game_reads_a_constant_number_of_queries(
    factory: Callable[[], Session], db_engine: Engine, db_session: Session
) -> None:
    engine = play_big_game(players=12, days=20)
    gid = persist(make_room(engine), factory, db_session)
    db_session.rollback()
    game = find_game_by_public_id(db_session, gid)
    assert game is not None
    pk = game.id
    db_session.expire_all()
    rec = Recorder(db_engine)
    try:
        g = load_stored_game(db_session, pk)
    finally:
        rec.stop()
    assert len(g.records) == 12 and len(g.market) == 20
    selects = [s for s in rec.statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 5, "one query per table, not per player or per day"


# ----------------------------------------------------------------- finders


def test_find_game_by_public_id(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    gid = persist(make_room(peer_engine), factory, db_session)
    db_session.rollback()
    game = find_game_by_public_id(db_session, gid)
    assert isinstance(game, Game) and game.public_id == gid
    assert find_game_by_public_id(db_session, "0" * 32) is None


def test_ac8_find_latest_game_by_room_code_returns_the_most_recent(
    peer_engine: PulkyEngine, factory: Callable[[], Session], db_session: Session
) -> None:
    later = persist(
        make_room(
            peer_engine,
            started_at="2026-10-13T09:00:00Z",
            finished_at="2026-10-13T10:00:00Z",
        ),
        factory,
        db_session,
    )
    earlier = persist(make_room(peer_engine), factory, db_session)  # inserted last
    assert earlier != later and count(db_session, "games") == 2
    db_session.rollback()
    for code in ("ABC234", "abc234"):
        game = find_latest_game_by_room_code(db_session, code)
        assert game is not None and game.public_id == later
    assert find_latest_game_by_room_code(db_session, "ZZZ999") is None


# --------------------------------------------------------------- user stats


def expected_stats(*games: tuple[PulkyEngine, str]) -> dict[str, Any]:
    """§3.3 for one user from (engine, alias) pairs."""
    players = [e.standings()["players"][a] for e, a in games]

    def mean(key: str) -> float | None:
        vals = [p[key] for p in players if p[key] is not None]
        return sum(vals) / len(vals) if vals else None

    profits = [p["total_profit"] for p in players]
    return {
        "games_played": len(players),
        "wins": sum(p["rank"] == 1 for p in players),
        "total_profit": sum(profits),
        "best_profit": max(profits),
        "avg_fill_rate": mean("fill_rate"),
        "avg_waste_rate": mean("waste_rate"),
        "avg_market_share": mean("overall_market_share"),
    }


def check_stats(row: dict[str, Any], want: dict[str, Any]) -> None:
    assert row["games_played"] == want["games_played"]
    assert row["wins"] == want["wins"]
    for key in ("total_profit", "best_profit"):
        assert num(row[key]) == pytest.approx(want[key], abs=0.01)
    for key in ("avg_fill_rate", "avg_waste_rate", "avg_market_share"):
        if want[key] is None:
            assert row[key] is None, key
        else:
            assert row[key] == pytest.approx(want[key], abs=1e-9), key


def test_ac7_rebuild_user_stats_for_a_user_with_two_games_one_won(
    factory: Callable[[], Session], db_session: Session
) -> None:
    game_a = play_peer_game(seed=11)
    game_b = play_peer_game(seed=3)
    rank_a = game_a.standings()["players"]["P1"]["rank"]
    rank_b = game_b.standings()["players"]["P2"]["rank"]
    assert (rank_a, rank_b) == (1, 2)
    persist(
        make_room(game_a, identities={"P1": "firebase-uid-alice", "P2": guest_id(2)}),
        factory,
        db_session,
    )
    persist(
        make_room(
            game_b,
            code="DEF567",
            identities={"P1": guest_id(3), "P2": "firebase-uid-alice"},
            started_at="2026-10-13T09:00:00Z",
            finished_at="2026-10-13T10:00:00Z",
        ),
        factory,
        db_session,
    )
    (user,) = rows(db_session, "SELECT id FROM users")
    want = expected_stats((game_a, "P1"), (game_b, "P2"))
    assert want["games_played"] == 2 and want["wins"] == 1

    # persisting rebuilt it already
    (row,) = rows(
        db_session, "SELECT * FROM user_stats WHERE user_id = :u", u=user["id"]
    )
    check_stats(row, want)

    # and an explicit rebuild reproduces it without duplicating anything
    db_session.execute(text("DELETE FROM user_stats"))
    db_session.commit()
    rebuild_user_stats(db_session, [user["id"]])
    db_session.commit()
    rebuild_user_stats(db_session, [user["id"]])
    db_session.commit()
    (row,) = rows(db_session, "SELECT * FROM user_stats")
    assert row["user_id"] == user["id"]
    check_stats(row, want)


def raw_insert(db: Session, table: str, **values: Any) -> int:
    """Raw INSERT of every NOT NULL column without a default (no timestamps)."""
    cols = db.execute(
        text(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM "
            "information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :t AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL "
            "AND EXTRA NOT LIKE '%auto_increment%'"
        ),
        {"t": table},
    ).all()
    filled: dict[str, Any] = {}
    for name, data_type, length in cols:
        if data_type == "json":
            filled[name] = "[]"
        elif data_type in ("char", "varchar"):
            filled[name] = "x" * min(3, int(length))
        elif data_type in ("datetime", "timestamp"):
            filled[name] = datetime(2026, 10, 12, 14, 0, 0)
        else:
            filled[name] = 0
    filled.update(values)
    names = ", ".join(f"`{c}`" for c in filled)
    marks = ", ".join(f":{c}" for c in filled)
    result = db.execute(
        text(f"INSERT INTO `{table}` ({names}) VALUES ({marks})"), filled
    )
    db.commit()
    return int(result.lastrowid or 0)


def test_rebuild_user_stats_means_skip_null_rates_and_users_are_independent(
    db_session: Session,
) -> None:
    alice = raw_insert(db_session, "users", firebase_uid="uid-a", display_name="A")
    bob = raw_insert(db_session, "users", firebase_uid="uid-b", display_name="B")
    g1 = raw_insert(db_session, "games", public_id="a" * 32, room_code="AAA111")
    g2 = raw_insert(db_session, "games", public_id="b" * 32, room_code="BBB222")
    raw_insert(
        db_session, "participants", game_id=g1, alias="P1", user_id=alice,
        final_rank=1, total_profit=100.5, fill_rate=0.5, waste_rate=None,
        overall_market_share=0.2,
    )  # fmt: skip
    raw_insert(
        db_session, "participants", game_id=g2, alias="P1", user_id=alice,
        final_rank=3, total_profit=-20.25, fill_rate=None, waste_rate=None,
        overall_market_share=0.4,
    )  # fmt: skip
    raw_insert(
        db_session, "participants", game_id=g1, alias="P2", user_id=bob,
        final_rank=2, total_profit=7.0, fill_rate=1.0, waste_rate=0.25,
        overall_market_share=0.8,
    )  # fmt: skip
    rebuild_user_stats(db_session, [alice, bob])
    db_session.commit()
    stats = {r["user_id"]: r for r in rows(db_session, "SELECT * FROM user_stats")}
    a, b = stats[alice], stats[bob]
    assert a["games_played"] == 2 and a["wins"] == 1
    assert num(a["total_profit"]) == pytest.approx(80.25)
    assert num(a["best_profit"]) == pytest.approx(100.5)
    assert a["avg_fill_rate"] == pytest.approx(0.5)  # the null is not a zero
    assert a["avg_waste_rate"] is None  # no value at all
    assert a["avg_market_share"] == pytest.approx(0.3)
    assert b["games_played"] == 1 and b["wins"] == 0
    assert num(b["best_profit"]) == pytest.approx(7.0)
    assert b["avg_waste_rate"] == pytest.approx(0.25)

    # only the given users are rebuilt
    raw_insert(
        db_session, "participants", game_id=g2, alias="P2", user_id=bob,
        final_rank=1, total_profit=1.0, fill_rate=0.0, waste_rate=0.0,
        overall_market_share=0.1,
    )  # fmt: skip
    rebuild_user_stats(db_session, [alice])
    db_session.commit()
    stats = {r["user_id"]: r for r in rows(db_session, "SELECT * FROM user_stats")}
    assert stats[bob]["games_played"] == 1
    rebuild_user_stats(db_session, [bob])
    db_session.commit()
    assert rows(
        db_session, "SELECT games_played FROM user_stats WHERE user_id = :u", u=bob
    ) == [{"games_played": 2}]


def test_get_or_create_user_is_idempotent_per_firebase_uid(
    db_session: Session,
) -> None:
    first = get_or_create_user(db_session, "uid-new", "Alice")
    db_session.commit()
    second = get_or_create_user(db_session, "uid-new", "Alice")
    db_session.commit()
    other = get_or_create_user(db_session, "uid-other")
    db_session.commit()
    assert first.id == second.id != other.id
    assert first.firebase_uid == "uid-new"
    assert count(db_session, "users", "firebase_uid = 'uid-new'") == 1
    assert count(db_session, "users") == 2
    assert (
        scalar(db_session, "SELECT display_name FROM users WHERE id = :i", i=first.id)
        == "Alice"
    )
