# ruff: noqa: F811
"""Black-box tests for section 19: the results routes (all against MySQL).

This module also owns the builders and fixtures the other section 19 test
modules import (``test_export``, ``test_claim``, ``test_users``). Every game is
played with section 11's engine, put into a finished room document and persisted
by section 18's ``persist_finished_game`` through a ``session_factory`` bound to
section 17's Testcontainers MySQL, never the default ``SessionLocal``.
"""

import asyncio
import copy
import json
import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.engine import PulkyEngine
from app.core.enums import IncotermTier
from app.core.records import Decision, PeerBuy, PeerSell
from app.db.session import get_db
from app.main import app
from app.services.db_service import (
    StoredGame,
    find_game_by_public_id,
    load_stored_game,
    persist_finished_game,
)
from app.services.results_service import results_response
from app.services.state_service import (
    get_state_service,
    new_room_document,
    reset_state_service,
)
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
]
QUIET = {
    "supplier__spot_spread": 0,
    "supplier__capacity_sd_per_player": 0,
    "supplier__disruption_probability": 0,
    "transport__failure_probability": 0,
}
HOST_SECRET = "host-secret-ZZTOP-0001"
STARTED = "2026-10-12T14:00:00Z"
FINISHED = "2026-10-12T15:00:00Z"
RESULTS_KEYS = {
    "game_id",
    "room_code",
    "host_display_name",
    "duration_days",
    "days_played",
    "ended_early",
    "started_at",
    "finished_at",
    "leaderboard",
    "stats",
}
NOT_FOUND = {"detail": "Results not found."}
NEED_SIGN_IN = {"detail": "Sign in to continue."}
NO_SIGN_IN = {"detail": "Sign-in is not available right now."}


# --------------------------------------------------- builders


def make_config(**over: Any) -> GameConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    for path, value in over.items():
        keys = path.split("__")
        node = data
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return GameConfig.from_dict(data)


def guest_id(n: int) -> str:
    """A well-formed guest id: ``guest_`` plus a 36-character uuid string."""
    return f"guest_{uuid.UUID(int=n)}"


def seats(n: int, names: list[str] | None = None) -> dict[str, str]:
    chosen = names or NAMES
    return {f"P{i + 1}": chosen[i] for i in range(n)}


def play_game(
    *,
    players: int = 2,
    seed: int = 11,
    early: bool = False,
    names: list[str] | None = None,
    days: int = 7,
) -> PulkyEngine:
    """A seed-fixed game through the real engine.

    Day 1: P1 orders 10 spot and conserves a lot, P2 orders 30 spot. Day 2: P1 is
    short with buy limits and P2 offers units, so the peer market trades. Every
    other decision is the zero decision. With ``early`` the game ends after day 3.
    """
    config = make_config(
        max_players=players,
        duration_days=days,
        futures__horizon_days=3,
        supplier__capacity_mean_per_player=10,
        supplier__quality_min=100,
        supplier__lead_time_days=0,
        **{**QUIET, "transport__failure_probability": 0.5},
    )
    engine = PulkyEngine.start(config, seats(players, names), seed)
    last = 3 if early else days
    for day in range(1, last + 1):
        for alias in engine.aliases:
            if day == 1 and alias == "P1":
                d = Decision(1, 10, 0, IncotermTier.CIP, ("L0",), False)
            elif day == 1 and alias == "P2":
                d = Decision(1, 30, 0, IncotermTier.DAP, (), True)
            elif day == 2 and alias == "P1":
                d = Decision(
                    2, 40, 0, IncotermTier.EXW, (), False, PeerBuy(20.0, 0), None
                )
            elif day == 2 and alias == "P2":
                d = Decision(
                    2, 0, 0, IncotermTier.EXW, (), False, None, PeerSell(15.0, 100, 25)
                )
            else:
                d = Decision.zero(day)
            engine.submit(alias, d)
        engine.resolve_day()
    if early:
        engine.end_early()
    assert engine.phase == "finished"
    return engine


def make_room(
    engine: PulkyEngine,
    *,
    code: str = "ABC234",
    identities: dict[str, str] | None = None,
    host_identity: str | None = None,
    started_at: str = STARTED,
    finished_at: str = FINISHED,
    host_name: str = "Ms. Ruiz",
    game_id: str | None = None,
) -> dict[str, Any]:
    """A finished room document with the game id it got when it finished.
    Default: every player and the host are guests."""
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
    room["game_id"] = game_id or uuid.uuid4().hex
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
    """Every credential and identity of a room that no response may carry."""
    out = [room["host_secret"], str(room["host_identity"])]
    for p in room["participants"].values():
        out += [p["identity"], p["session_token"]]
    return out


def norm(value: Any) -> Any:
    """The JSON round trip: tuples become lists, enums become wire strings."""
    return json.loads(json.dumps(value))


def assert_no_secrets(body: Any, secrets: list[str]) -> None:
    blob = body if isinstance(body, str) else json.dumps(body)
    for secret in secrets:
        assert secret not in blob, f"a response carries {secret!r}"
    for key in ("identity", "guest_identity", "session_token", "host_secret"):
        assert f'"{key}"' not in blob
        assert f"{key}," not in blob


# --------------------------------------------------- fixtures


@pytest.fixture()
def db_engine(mysql_url: str) -> Iterator[Engine]:
    engine = create_engine(mysql_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def factory(db_engine: Engine) -> Callable[[], Session]:
    return sessionmaker(bind=db_engine)


def _install_db(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Session]
) -> Iterator[None]:
    import app.db.session as session_module

    def dependency() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(session_module, "SessionLocal", factory)
    app.dependency_overrides[get_db] = dependency
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def api(
    client: TestClient,
    factory: Callable[[], Session],
    db_session: Session,  # truncates every table after the test
    memory_backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """The app on the Testcontainers database and a fresh in-memory room store."""
    reset_state_service()
    yield from _wrap(_install_db(monkeypatch, factory), client)
    reset_state_service()


def _wrap(gen: Iterator[None], client: TestClient) -> Iterator[TestClient]:
    next(gen)
    try:
        yield client
    finally:
        for _ in gen:
            pass


@pytest.fixture()
def bare_api(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The app on a database with no tables: any query a route makes would fail."""
    lite = sessionmaker(bind=create_engine("sqlite://"))
    yield from _wrap(_install_db(monkeypatch, lite), client)


def claims_for(uid: str, **extra: Any) -> dict[str, Any]:
    """Token claims; the uid travels under every key a verifier might expose."""
    return {"uid": uid, "sub": uid, "user_id": uid, **extra}


def sign_in(
    tokens: dict[str, dict[str, Any]], uid: str, **extra: Any
) -> dict[str, str]:
    """Register ``uid`` with the Firebase double; return its bearer header."""
    token = f"token-of-{uid}"
    tokens[token] = claims_for(uid, **extra)
    return {"Authorization": f"Bearer {token}"}


def persist(room: dict[str, Any], factory: Callable[[], Session]) -> str:
    gid = persist_finished_game(room, factory)
    assert isinstance(gid, str)
    assert re.fullmatch(r"[0-9a-f]{32}", gid)
    return gid


def stored(db: Session, public_id: str) -> StoredGame:
    db.rollback()
    game = find_game_by_public_id(db, public_id)
    assert game is not None
    return load_stored_game(db, game.id)


def scalar(db: Session, sql: str, **params: Any) -> Any:
    db.rollback()
    return db.execute(text(sql), params).scalar()


def put_live_room(room: dict[str, Any]) -> None:
    """Make ``room`` a live room of the state service (section 13)."""
    asyncio.run(get_state_service().save_room(room["room_code"], room))


def drop_live_room(code: str) -> None:
    asyncio.run(get_state_service().delete_room(code))


def live_room_for(
    game_id: str, base: dict[str, Any], *, secret: str, code: str | None = None
) -> dict[str, Any]:
    room = copy.deepcopy(base)
    room["host_secret"] = secret
    room["game_id"] = game_id
    room["persisted"] = True
    if code is not None:
        room["room_code"] = code
    return room


# --------------------------------------------------- fixtures of games


@pytest.fixture(scope="module")
def natural_engine() -> PulkyEngine:
    return play_game()


@pytest.fixture(scope="module")
def early_engine() -> PulkyEngine:
    return play_game(early=True)


def test_fixture_games_have_the_scenario_they_claim(
    natural_engine: PulkyEngine, early_engine: PulkyEngine
) -> None:
    for engine in (natural_engine, early_engine):
        market = engine.market_history()
        assert sum(m.peer_units for m in market) > 0
        assert any(m.peer_units == 0 and m.peer_avg_price is None for m in market)
        assert engine.records("P1")[0].peer_buy is None
        assert engine.records("P1")[1].peer_buy is not None
    assert natural_engine.ended_early is False
    assert early_engine.ended_early is True
    assert len(early_engine.market_history()) == 3


# --------------------------------------------------- AC 1, metadata


def check_parity(body: dict[str, Any], engine: PulkyEngine) -> None:
    live = norm(engine.results())
    assert body["leaderboard"] == live["leaderboard"]
    assert body["stats"] == live["stats"]
    assert body["days_played"] == live["days_played"]
    assert body["ended_early"] == live["ended_early"]
    assert "seq" not in body
    market = body["stats"]["market"]
    assert market["peer_units_total"] > 0  # not a vacuous comparison


@pytest.mark.parametrize("fixture_name", ["natural_engine", "early_engine"])
def test_ac1_the_results_route_equals_the_live_results(
    fixture_name: str,
    request: pytest.FixtureRequest,
    api: TestClient,
    factory: Callable[[], Session],
) -> None:
    engine: PulkyEngine = request.getfixturevalue(fixture_name)
    gid = persist(make_room(engine), factory)
    by_id = api.get(f"/api/v1/games/id/{gid}/results")
    assert by_id.status_code == 200
    check_parity(by_id.json(), engine)


def test_results_metadata_and_shape(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    natural_engine: PulkyEngine,
) -> None:
    room = make_room(natural_engine, code="XYZ789", host_name="Ms. Ruiz")
    gid = persist(room, factory)
    body = api.get(f"/api/v1/games/id/{gid}/results").json()
    assert set(body) == RESULTS_KEYS
    assert body["game_id"] == gid
    assert body["room_code"] == "XYZ789"
    assert body["host_display_name"] == "Ms. Ruiz"
    assert body["duration_days"] == room["config"]["duration_days"]
    assert body["days_played"] == len(natural_engine.market_history())
    assert body["ended_early"] is False
    assert body["started_at"] == STARTED
    assert body["finished_at"] == FINISHED
    assert set(body["stats"]) == {"players", "market"}
    assert set(body["stats"]["players"]) == set(natural_engine.aliases)
    # the service builds the same document the route serves
    assert norm(results_response(stored(db_session, gid))) == body


def test_ac1_early_end_metadata(
    api: TestClient, factory: Callable[[], Session], early_engine: PulkyEngine
) -> None:
    gid = persist(make_room(early_engine), factory)
    body = api.get(f"/api/v1/games/id/{gid}/results").json()
    assert body["ended_early"] is True
    assert body["days_played"] == 3
    assert body["days_played"] < body["duration_days"]


# --------------------------------------------------- FM 7


def test_fm7_a_stored_row_that_drifts_from_the_live_result_fails_parity(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
    natural_engine: PulkyEngine,
) -> None:
    """The parity check can fail: corrupting one stored field breaks it."""
    gid = persist(make_room(natural_engine), factory)
    clean = api.get(f"/api/v1/games/id/{gid}/results").json()
    check_parity(clean, natural_engine)
    db_session.rollback()
    db_session.execute(
        text(
            "UPDATE player_days SET sold = sold + 1 WHERE alias = 'P1' AND day = 1 "
            "AND game_id = (SELECT id FROM games WHERE public_id = :g)"
        ),
        {"g": gid},
    )
    db_session.commit()
    drifted = api.get(f"/api/v1/games/id/{gid}/results").json()
    assert drifted["stats"] != norm(natural_engine.results())["stats"]


def test_fm7_peer_fields_survive_the_round_trip_into_the_summary(
    api: TestClient, factory: Callable[[], Session], natural_engine: PulkyEngine
) -> None:
    gid = persist(make_room(natural_engine), factory)
    stats = api.get(f"/api/v1/games/id/{gid}/results").json()["stats"]
    live = norm(natural_engine.results())["stats"]
    p1, p2 = stats["players"]["P1"], stats["players"]["P2"]
    assert p1["peer_units_bought"] == live["players"]["P1"]["peer_units_bought"] > 0
    assert p2["peer_units_sold"] == live["players"]["P2"]["peer_units_sold"] > 0
    assert p1["cost_peer"] == p2["revenue_peer"] > 0
    assert stats["market"]["series"]["peer_avg_price"] == (
        live["market"]["series"]["peer_avg_price"]
    )
    assert None in stats["market"]["series"]["peer_avg_price"]
    assert p1["final_settlement"] == live["players"]["P1"]["final_settlement"]


# --------------------------------------------------- AC 2, 404s


def test_ac2_games_under_a_reused_room_code_are_each_served_by_their_own_id(
    api: TestClient, factory: Callable[[], Session], natural_engine: PulkyEngine
) -> None:
    """A room code is reissued once its live room expires: results are reached by
    game id only, so a later game never takes over an earlier game's link."""
    other = play_game(seed=5, early=True)
    newer = persist(
        make_room(
            other,
            started_at="2026-10-13T09:00:00Z",
            finished_at="2026-10-13T10:00:00Z",
        ),
        factory,
    )
    older = persist(make_room(natural_engine), factory)
    assert newer != older
    for gid, engine in ((older, natural_engine), (newer, other)):
        body = api.get(f"/api/v1/games/id/{gid}/results").json()
        assert body["game_id"] == gid and body["room_code"] == "ABC234"
        check_parity(body, engine)
    # there is no route by room code
    assert api.get("/api/v1/games/ABC234/results").status_code == 404


def test_unknown_code_and_unknown_id_answer_404(
    api: TestClient, factory: Callable[[], Session], natural_engine: PulkyEngine
) -> None:
    gid = persist(make_room(natural_engine), factory)
    for url in (
        f"/api/v1/games/id/{'0' * 32}/results",
        "/api/v1/games/id/not-a-game/results",
    ):
        res = api.get(url)
        assert res.status_code == 404
        assert res.json() == NOT_FOUND
    assert api.get(f"/api/v1/games/id/{gid}/results").status_code == 200


def test_ac3_results_routes_carry_no_identity_or_credential(
    api: TestClient, factory: Callable[[], Session], natural_engine: PulkyEngine
) -> None:
    room = make_room(natural_engine)
    gid = persist(room, factory)
    for url in (f"/api/v1/games/id/{gid}/results",):
        res = api.get(url)
        assert_no_secrets(res.text, secrets_of(room))
        assert_no_secrets(dict(res.headers), secrets_of(room))
