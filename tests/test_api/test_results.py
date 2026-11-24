"""Black-box tests for the results routes (all against PostgreSQL)."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config_models import GameConfig, host_default_config
from app.core.defaults import DEFAULT_CONFIG, HIDDEN_DEFAULT_KEYS
from app.core.engine import PulkyEngine
from app.core.records import Decision
from app.services.results_service import results_response
from app.services.state_service import (
    get_state_service,
)
from tests.fixtures.finished_games import (
    FINISHED,
    NOT_FOUND,
    STARTED,
    assert_no_secrets,
    make_room,
    norm,
    persist,
    play_game,
    seats,
    secrets_of,
    stored,
)

pytestmark = pytest.mark.dbschema

RESULTS_KEYS = {
    "game_id",
    "room_code",
    "duration_days",
    "days_played",
    "ended_early",
    "started_at",
    "finished_at",
    "leaderboard",
    "stats",
    "config",
}


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


# -------------------------------------------- live parity, metadata


def check_parity(body: dict[str, Any], engine: PulkyEngine) -> None:
    live = norm(engine.results())
    assert body["leaderboard"] == live["leaderboard"]
    assert body["stats"] == live["stats"]
    assert body["days_played"] == live["days_played"]
    assert body["ended_early"] == live["ended_early"]
    assert body["config"] == live["config"]
    assert "seq" not in body
    market = body["stats"]["market"]
    assert market["peer_units_total"] > 0  # not a vacuous comparison


@pytest.mark.parametrize("fixture_name", ["natural_engine", "early_engine"])
def test_the_results_route_equals_the_live_results(
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


def test_a_game_with_hidden_defaults_is_stored_and_revealed_with_the_full_config(
    api: TestClient,
    factory: Callable[[], Session],
    db_session: Session,
) -> None:
    # Spec 9.3: the room keeps the host's config (null at each hidden default); the
    # engine ran on the defaults, and the stored game and its results carry them.
    engine = PulkyEngine.start(GameConfig.default(), seats(2), 21)
    for alias in engine.aliases:
        engine.submit(alias, Decision.zero(1))
    engine.resolve_day()
    engine.end_early()
    room = make_room(engine)
    room["config"] = host_default_config()
    gid = persist(room, factory)
    body = api.get(f"/api/v1/games/id/{gid}/results").json()
    assert body["config"] == DEFAULT_CONFIG
    for path in HIDDEN_DEFAULT_KEYS:
        section, key = path.split(".")
        assert body["config"][section][key] == DEFAULT_CONFIG[section][key]
    row = db_session.execute(text("SELECT config FROM game_configs")).one()
    stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    assert stored == DEFAULT_CONFIG


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
    assert "Ms. Ruiz" not in json.dumps(body)  # the host's name is never stored
    assert body["duration_days"] == room["config"]["duration_days"]
    assert body["days_played"] == len(natural_engine.market_history())
    assert body["ended_early"] is False
    assert body["started_at"] == STARTED
    assert body["finished_at"] == FINISHED
    assert set(body["stats"]) == {"players", "market"}
    assert set(body["stats"]["players"]) == set(natural_engine.aliases)
    # the service builds the same document the route serves
    assert norm(results_response(stored(db_session, gid))) == body


def test_early_end_metadata(
    api: TestClient, factory: Callable[[], Session], early_engine: PulkyEngine
) -> None:
    gid = persist(make_room(early_engine), factory)
    body = api.get(f"/api/v1/games/id/{gid}/results").json()
    assert body["ended_early"] is True
    assert body["days_played"] == 3
    assert body["days_played"] < body["duration_days"]


# --------------------------------- stored rows round trip


def test_a_stored_row_that_drifts_from_the_live_result_fails_parity(
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


def test_peer_fields_survive_the_round_trip_into_the_summary(
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


# --------------------------------- reused codes, 404s, no leaks


def test_games_under_a_reused_room_code_are_each_served_by_their_own_id(
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


def test_results_never_expire_and_need_no_live_room_or_credential(
    api: TestClient, factory: Callable[[], Session], natural_engine: PulkyEngine
) -> None:
    """The results link is the public id alone and never expires: a game finished
    years ago, whose room is long gone, is served to anyone without a secret or
    bearer (spec 13.4)."""
    gid = persist(
        make_room(
            natural_engine,
            started_at="2001-01-01T09:00:00Z",
            finished_at="2001-01-01T10:00:00Z",
        ),
        factory,
    )
    assert asyncio.run(get_state_service().get_room("ABC234")) is None
    res = api.get(f"/api/v1/games/id/{gid}/results")
    assert res.status_code == 200
    assert res.json()["game_id"] == gid
    assert res.json()["finished_at"] == "2001-01-01T10:00:00Z"


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


def test_results_routes_carry_no_identity_or_credential(
    api: TestClient, factory: Callable[[], Session], natural_engine: PulkyEngine
) -> None:
    room = make_room(natural_engine)
    gid = persist(room, factory)
    for url in (f"/api/v1/games/id/{gid}/results",):
        res = api.get(url)
        assert_no_secrets(res.text, secrets_of(room))
        assert_no_secrets(dict(res.headers), secrets_of(room))
