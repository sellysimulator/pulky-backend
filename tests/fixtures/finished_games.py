"""Builders and fixtures of the REST suites over finished games (``test_results``,
``test_export``, ``test_claim``, ``test_users``).

Every game is played with the engine, put into a finished room document and
persisted by ``persist_finished_game`` through a ``session_factory`` bound to the
Testcontainers PostgreSQL of tests/fixtures/postgres.py, never the default
``SessionLocal``.
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
from app.services.state_service import (
    get_state_service,
    new_room_document,
    reset_state_service,
)

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
    host_uid: str | None = None,
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
    room["host_uid"] = host_uid
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
    if room.get("host_uid"):
        out.append(room["host_uid"])
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
    """Make ``room`` a live room of the state service."""
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
