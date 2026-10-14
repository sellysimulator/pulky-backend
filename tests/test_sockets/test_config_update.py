"""Black-box tests for the socket config update (section 15 3.5, criteria 6, 11)."""

import copy
from typing import Any

import pytest

from app.core.config_models import MSG_CONFIG_LOCKED, GameConfig, config_public
from app.core.defaults import PLAYER_CONFIG_KEYS
from app.services.state_service import StateService
from app.sockets.errors import SERVER_ERROR_MESSAGE
from app.sockets.handlers.lobby import (
    config_update,
    join,
    join_waiting,
    leave,
    start_game,
)
from tests.test_sockets.conftest import World, leaf_paths

aio = pytest.mark.asyncio
ONLY_HOST = "Only the host can do that."


async def hosted(world: World, patch: dict | None = None) -> tuple[str, str]:
    code = await world.create_room(patch)
    secret = str((await world.room(code))["host_secret"])
    await world.claim_host(code, "host-1", 900)
    return code, secret


def update(code: str, secret: str | None, patch: dict) -> dict[str, Any]:
    data: dict[str, Any] = {"room_id": code, "config": patch}
    if secret is not None:
        data["host_secret"] = secret
    return data


@aio
async def test_valid_update_sends_the_full_config_to_the_host_only(
    world: World,
) -> None:
    code, secret = await hosted(world)
    start = world.mark()
    await config_update(
        "host-1", update(code, secret, {"demand": {"retail_price": 31}})
    )

    room = await world.room(code)
    assert room["config"]["demand"]["retail_price"] == 31
    host_copy = world.sent("host-1", "config_updated", start)
    assert len(host_copy) == 1
    assert host_copy[0]["config"] == room["config"]
    assert isinstance(host_copy[0]["seq"], int)


@aio
async def test_room_copy_skips_the_host_and_carries_config_public(
    world: World,
) -> None:
    # Failure mode 7: a broadcast that reaches the host overwrites its full config.
    code, secret = await hosted(world, {"random_seed": 918273645})
    start = world.mark()
    await config_update(
        "host-1", update(code, secret, {"demand": {"retail_price": 31}})
    )

    room_records = [
        r for r in world.sm.records[start:] if r[0] == code and r[1] == "config_updated"
    ]
    assert len(room_records) == 1
    _, _, data, skip_sid = room_records[0]
    assert skip_sid == "host-1"
    room = await world.room(code)
    expected = config_public(GameConfig.from_dict(room["config"]))
    assert data["config"] == expected
    assert leaf_paths(data["config"]) == set(PLAYER_CONFIG_KEYS)
    assert data["config"]["demand"]["retail_price"] == 31
    host_copy = world.sent("host-1", "config_updated", start)[0]
    assert data["seq"] == host_copy["seq"]  # one seq for both versions


@aio
async def test_first_save_moves_the_room_to_configuring_and_patches_merge(
    world: World,
) -> None:
    code, secret = await hosted(world)
    before = (await world.room(code))["config"]
    assert (await world.room(code))["state"] == "lobby"
    await config_update("host-1", update(code, secret, {"duration_days": 20}))
    room = await world.room(code)
    assert room["state"] == "configuring"
    expected = copy.deepcopy(before)
    expected["duration_days"] = 20
    assert room["config"] == expected
    await config_update("host-1", update(code, secret, {"starting_cash": 6000}))
    expected["starting_cash"] = 6000
    assert (await world.room(code))["config"] == expected


@aio
async def test_missing_room_is_not_in_room(world: World) -> None:
    await world.connect("host-1", 900)
    await config_update("host-1", update("NOPE99", "whatever", {"duration_days": 20}))
    sent = world.sent("host-1")
    assert sent == [{"code": "NOT_IN_ROOM", "message": "You are not in this room."}]


@aio
async def test_wrong_secret_is_refused_and_changes_nothing(world: World) -> None:
    code, _ = await hosted(world)
    before = copy.deepcopy(await world.room(code))
    start = world.mark()
    await config_update("host-1", update(code, "guess", {"duration_days": 20}))

    assert world.sent("host-1", since=start) == [
        {"code": "INVALID_HOST_SECRET", "message": ONLY_HOST}
    ]
    assert world.sent(code, since=start) == []
    assert await world.room(code) == before


@aio
@pytest.mark.parametrize("secret", [None, ""], ids=["absent", "empty"])
async def test_empty_stored_secret_does_not_authorise_a_blank_one(
    world: World, secret: str | None
) -> None:
    code, _ = await hosted(world)

    def blank(room: dict) -> None:
        room["host_secret"] = ""

    await world.mutate(code, blank)
    before = copy.deepcopy(await world.room(code))
    start = world.mark()
    await config_update("host-1", update(code, secret, {"duration_days": 20}))
    sent = world.sent("host-1", since=start)
    assert [d["code"] for d in sent] == ["INVALID_HOST_SECRET"]
    assert await world.room(code) == before


@aio
async def test_host_identity_is_not_enough_for_a_mutation(world: World) -> None:
    code, _ = await hosted(world)  # host-1 holds the room by secret
    await world.claim_host(code, "host-2", 900, with_secret=False)  # by identity
    assert (await world.room(code))["host_sid"] == "host-2"
    before = copy.deepcopy(await world.room(code))
    start = world.mark()
    await config_update("host-2", update(code, None, {"duration_days": 20}))
    assert world.sent("host-2", since=start) == [
        {"code": "INVALID_HOST_SECRET", "message": ONLY_HOST}
    ]
    assert await world.room(code) == before


@aio
async def test_invalid_patch_reports_the_field_and_leaves_the_config(
    world: World,
) -> None:
    code, secret = await hosted(world)
    before = copy.deepcopy(await world.room(code))
    start = world.mark()
    await config_update("host-1", update(code, secret, {"stock": {"max_days": 1}}))

    assert world.sent("host-1", since=start) == [
        {
            "code": "INVALID_CONFIG",
            "field": "stock.max_days",
            "message": "Must be more than the fresh days (1).",
        }
    ]
    assert world.sent(code, since=start) == []
    after = await world.room(code)
    assert after["config"] == before["config"]
    assert after["state"] == "lobby"


@aio
async def test_out_of_range_value_names_its_field(world: World) -> None:
    code, secret = await hosted(world)
    await config_update("host-1", update(code, secret, {"duration_days": 3}))
    err = world.last("host-1", "error")
    assert err["code"] == "INVALID_CONFIG"
    assert err["field"] == "duration_days"
    assert isinstance(err["message"], str) and err["message"]
    assert (await world.room(code))["config"]["duration_days"] == 14


@aio
async def test_locked_after_the_start(world: World) -> None:
    game = await world.running_game()
    for finished in (False, True):
        if finished:
            await world.finish_early(game.code)
        before = copy.deepcopy(await world.room(game.code))
        start = world.mark()
        await config_update(
            game.host_sid, update(game.code, game.secret, {"duration_days": 20})
        )
        errors = world.sent(game.host_sid, "error", start)
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_CONFIG"
        assert errors[0]["message"] == MSG_CONFIG_LOCKED
        assert not errors[0].get("field")
        assert world.sent(game.code, "config_updated", start) == []
        assert (await world.room(game.code))["config"] == before["config"]


@aio
async def test_lowering_max_players_below_the_seats_taken_removes_nobody(
    world: World,
) -> None:
    code, secret = await hosted(world)
    for n in (1, 2, 3):
        await world.seat(code, f"p{n}", n)
    start = world.mark()
    await config_update("host-1", update(code, secret, {"max_players": 2}))

    assert world.sent("host-1", "error", start) == []
    room = await world.room(code)
    assert room["config"]["max_players"] == 2
    assert sorted(room["participants"]) == ["P1", "P2", "P3"]
    assert set(room["sid_to_alias"]) == {"p1", "p2", "p3"}

    # new joins wait until a seat frees up: 3 seated, 2 allowed
    assert await world.join(code, "p4", 4) == [{"message": "This room is full."}]
    await world.leave(code, "p3")
    assert await world.join(code, "p4", 4) == [{"message": "This room is full."}]
    await world.leave(code, "p2")
    assert (await world.seat(code, "p4", 4))["alias"] == "P2"


# ------------------------------------------------------- @guarded (criterion 11)
@aio
@pytest.mark.parametrize("name", ["join_waiting", "join", "leave", "start_game"])
async def test_lobby_handler_failures_become_join_error(
    world: World, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    handlers = {
        "join_waiting": join_waiting,
        "join": join,
        "leave": leave,
        "start_game": start_game,
    }
    code = await world.create_room()
    await world.connect("a", 1)

    async def boom(self: StateService, room_code: str) -> dict:
        raise RuntimeError("boom-internal-detail")

    monkeypatch.setattr(StateService, "get_room", boom)
    start = world.mark()
    await handlers[name]("a", {"room_id": code, "host_secret": "x"})

    assert world.sent("a", since=start) == [{"message": SERVER_ERROR_MESSAGE}]
    assert world.sent(code, since=start) == []
    assert "boom" not in str(world.sm.records[start:])


@aio
async def test_config_update_failure_becomes_server_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, secret = await hosted(world)

    async def boom(self: StateService, room_code: str) -> dict:
        raise RuntimeError("boom-internal-detail")

    monkeypatch.setattr(StateService, "get_room", boom)
    start = world.mark()
    await config_update("host-1", update(code, secret, {"duration_days": 20}))

    assert world.sent("host-1", since=start) == [
        {"code": "SERVER_ERROR", "message": SERVER_ERROR_MESSAGE}
    ]
    assert world.sent(code, since=start) == []
    assert "boom" not in str(world.sm.records[start:])
