# ruff: noqa: F811
"""Hidden defaults (spec 9.3): anyone can open a host screen, so the risk odds'
defaults reach no host, REST response or validation message before the game ends.
The room keeps the host's config (null at each hidden default), the engine runs on
the defaults, and ``game_finished`` reveals them."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config_models import GameConfig, host_default_config
from app.core.defaults import DEFAULT_CONFIG, HIDDEN_DEFAULT_KEYS
from app.services import game_service
from app.services.state_service import get_state_service
from app.sockets.handlers.host import end_game_early
from app.sockets.handlers.lobby import config_update, start_game
from tests.test_sockets.conftest import HOST_NAME, Game, World
from tests.test_sockets.test_submit import (  # noqa: F401
    PersistSpy,
    _never_touch_a_real_database,
    persist,
    play_day,
)

aio = pytest.mark.asyncio
API = "/api/v1"


def at(tree: dict[str, Any], path: str) -> Any:
    node: Any = tree
    for part in path.split("."):
        node = node[part]
    return node


def configs_in(payload: Any) -> list[dict[str, Any]]:
    """Every dict found under a ``config`` key, at any depth."""
    found: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "config" and isinstance(value, dict):
                found.append(value)
            found.extend(configs_in(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(configs_in(item))
    return found


def full_configs(payload: Any) -> list[dict[str, Any]]:
    """The configs that carry the supplier section: the full ones, not config_public."""
    return [c for c in configs_in(payload) if "supplier" in c]


async def hidden_game(world: World, players: int = 2) -> Game:
    """A running game whose room was created like ``POST /rooms/create`` does it."""
    svc = get_state_service()
    room = await svc.create_room(host_default_config(), HOST_NAME)
    code = str(room["room_code"])
    if not await svc.room_exists(code):
        await svc.save_room(code, room)
    secret = str((await world.room(code))["host_secret"])
    game = Game(code=code, secret=secret, host_sid="host-1")
    await world.claim_host(code, game.host_sid)
    for n in range(1, players + 1):
        joined = await world.seat(code, f"p{n}", n)
        game.sids[joined["alias"]] = f"p{n}"
    # A config change that leaves the risks alone keeps them null.
    await config_update(
        game.host_sid,
        {"room_id": code, "host_secret": secret, "config": {"starting_cash": 4000}},
    )
    await start_game(game.host_sid, game.start_data())
    return game


@aio
async def test_no_host_emit_before_the_end_carries_a_hidden_default(
    world: World, persist: PersistSpy
) -> None:
    game = await hidden_game(world)
    await play_day(game, 1)
    await play_day(game, 2)
    room = await world.room(game.code)
    engine = game_service.load_engine(room)
    assert engine is not None
    # the engine runs on the defaults ...
    for path in HIDDEN_DEFAULT_KEYS:
        assert at(engine.config.to_dict(), path) == at(DEFAULT_CONFIG, path)
        # ... while the room keeps the host's config
        assert at(room["config"], path) is None
    assert room["config"]["starting_cash"] == 4000

    before_end = world.mark()
    await end_game_early(
        game.host_sid, {"room_id": game.code, "host_secret": game.secret}
    )
    to_host = [r for r in world.sm.records[:before_end] if r[0] == game.host_sid]
    events = {r[1] for r in to_host}
    assert {"config_updated", "host_state"} <= events
    checked = 0
    for _, event, payload, _ in to_host:
        for config in full_configs(payload):
            checked += 1
            for path in HIDDEN_DEFAULT_KEYS:
                assert at(config, path) is None, (event, path)
    assert checked >= 3  # config_updated, the full and the daily host_state

    # the end reveals them, to everyone
    finished = [r for r in world.sm.records[before_end:] if r[1] == "game_finished"]
    assert finished
    for _, _, payload, _ in finished:
        assert payload["config"] == engine.config.to_dict()
        for path in HIDDEN_DEFAULT_KEYS:
            assert at(payload["config"], path) == at(DEFAULT_CONFIG, path)


@aio
async def test_a_typed_risk_is_kept_and_used_and_a_blank_one_is_the_default(
    world: World,
) -> None:
    svc = get_state_service()
    room = await svc.create_room(host_default_config(), HOST_NAME)
    code = str(room["room_code"])
    if not await svc.room_exists(code):
        await svc.save_room(code, room)
    secret = str((await world.room(code))["host_secret"])
    await world.claim_host(code, "host-9")

    async def update(patch: dict[str, Any]) -> dict[str, Any]:
        await config_update(
            "host-9", {"room_id": code, "host_secret": secret, "config": patch}
        )
        return (await world.room(code))["config"]

    typed = await update({"transport": {"failure_probability": 0.42}})
    assert at(typed, "transport.failure_probability") == 0.42
    assert at(typed, "transport.loss_fraction") is None
    engine_config = GameConfig.from_host_config(typed)
    assert engine_config.transport.failure_probability == 0.42
    assert engine_config.transport.loss_fraction == 0.30
    # a blank field sends null: the default again
    blank = await update({"transport": {"failure_probability": None}})
    assert at(blank, "transport.failure_probability") is None


def test_rest_never_reveals_a_hidden_default_before_the_end(client: TestClient) -> None:
    defaults = client.get(f"{API}/config/defaults").json()
    for path in HIDDEN_DEFAULT_KEYS:
        assert at(defaults["config"], path) is None
        assert defaults["fields"][path]["hidden_default"] is True
        assert defaults["fields"][path]["nullable"] is True
    assert {p for p, f in defaults["fields"].items() if f["hidden_default"]} == set(
        HIDDEN_DEFAULT_KEYS
    )

    created = client.post(f"{API}/rooms/create", json={}).json()
    headers = {"X-Host-Secret": created["host_secret"]}
    url = f"{API}/rooms/{created['room_code']}/config"
    got = client.get(url, headers=headers).json()
    put = client.put(url, headers=headers, json={"config": {"duration_days": 10}})
    assert put.status_code == 200
    for body in (created, got, put.json()):
        for path in HIDDEN_DEFAULT_KEYS:
            assert at(body["config"], path) is None, path

    # validation: no ratio, no message quotes a hidden default; typed, they warn
    shared = host_default_config()
    shared["transport"]["shared"] = True
    hidden = client.post(f"{API}/config/validate", json={"config": shared}).json()
    assert hidden == {"ok": True, "errors": [], "warnings": []}
    typed = dict(shared, transport={**shared["transport"]})
    typed["transport"].update(failure_probability=0.14, loss_fraction=0.3)
    warned = client.post(f"{API}/config/validate", json={"config": typed}).json()
    assert [w["field"] for w in warned["warnings"]] == [
        "insurance.tiers.cip.fee",
        "insurance.tiers.dap.fee",
        "insurance.tiers.ddp.fee",
    ]


def test_validate_warns_about_a_pipeline_without_a_lead_time(
    client: TestClient,
) -> None:
    config = host_default_config()
    config["supplier"]["initial_pipeline_qty"] = 10
    body = client.post(f"{API}/config/validate", json={"config": config}).json()
    assert body["ok"] is True
    assert body["warnings"] == [
        {
            "field": "supplier.initial_pipeline_qty",
            "message": (
                "No effect with a lead time of 0: nothing is on its way at the start."
            ),
        }
    ]
    config["supplier"]["lead_time_days"] = 2
    body = client.post(f"{API}/config/validate", json={"config": config}).json()
    assert body["warnings"] == []
