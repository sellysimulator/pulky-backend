# ruff: noqa: F811
"""Black-box tests for request_state (plan section 16, 3.2; criterion 6)."""

from typing import Any

import pytest

from app.services import game_service
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.play import request_state
from tests.conftest import contains_key, contains_value
from tests.test_sockets.conftest import World, norm
from tests.test_sockets.test_submit import (  # noqa: F401
    MSG_NOT_IN_ROOM,
    PersistSpy,
    _never_touch_a_real_database,
    persist,
    play_day,
    room_engine,
    submit,
)

aio = pytest.mark.asyncio


async def ask(world: World, sid: str, code: str) -> list[tuple[Any, ...]]:
    start = world.mark()
    await request_state(sid, {"room_id": code})
    return world.sm.records[start:]


def strip(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in ("seq", "full")}


async def lobby(world: World) -> str:
    code = await world.create_room()
    await world.claim_host(code)
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    return code


# ------------------------------------------------------------- a running game
@aio
async def test_the_host_gets_a_full_host_state(world: World) -> None:
    game = await world.running_game()
    await play_day(game, 1)
    await play_day(game, 2)
    before = await world.room(game.code)
    got = await ask(world, "host-1", game.code)

    assert [(r[0], r[1]) for r in got] == [("host-1", "host_state")]
    state = got[0][2]
    assert state["full"] is True
    assert state["state"] == "running"
    assert state["day"] == 3
    engine = room_engine(before)
    expected = game_service.host_state(before, 0, True)
    assert norm(strip(state)) == norm(strip(expected))
    for alias in ("P1", "P2"):
        assert len(state["players"][alias]["own_history"]) == 2
    assert len(state["public_history"]) == 2
    assert len(state["market_history"]) == 2
    assert len(engine.public_history()) == 2


@aio
async def test_a_seated_player_gets_a_full_your_state_on_their_own_sid(
    world: World,
) -> None:
    game = await world.running_game()
    await play_day(game, 1)
    await submit(game, "P2", 2)  # P2 has submitted today, P1 has not
    room = await world.room(game.code)
    got = await ask(world, "p2", game.code)

    assert [(r[0], r[1]) for r in got] == [("p2", "your_state")]
    state = got[0][2]
    assert state["full"] is True
    assert state["alias"] == "P2"
    assert state["has_submitted"] is True
    assert len(state["own_history"]) == 1
    assert len(state["public_history"]) == 1
    assert norm(strip(state)) == norm(room_engine(room).player_view("P2", full=True))
    other = await ask(world, "p1", game.code)
    assert other[0][2]["alias"] == "P1"
    assert other[0][2]["has_submitted"] is False


@aio
async def test_request_state_takes_a_new_seq_and_saves_the_room(
    world: World,
) -> None:
    game = await world.running_game()
    before = await world.room(game.code)
    got = await ask(world, "p1", game.code)
    after = await world.room(game.code)
    assert got[0][2]["seq"] == before["seq"] + 1
    assert after["seq"] == before["seq"] + 1
    got = await ask(world, "host-1", game.code)
    assert got[0][2]["seq"] == before["seq"] + 2
    assert (await world.room(game.code))["seq"] == before["seq"] + 2


@aio
async def test_request_state_changes_nothing_in_the_game(world: World) -> None:
    game = await world.running_game({"starting_cash": 100_000})
    await submit(game, "P1", 1, order_spot=4321)
    before = await world.room(game.code)
    for sid in ("p1", "p2", "host-1"):
        await ask(world, sid, game.code)
    after = await world.room(game.code)
    assert {k: v for k, v in after.items() if k != "seq"} == {
        k: v for k, v in before.items() if k != "seq"
    }
    engine = room_engine(after)
    assert engine.has_submitted("P1") and engine.awaiting() == ["P2"]


@aio
async def test_a_state_answer_never_exposes_a_pending_decision_to_others(
    world: World,
) -> None:
    game = await world.running_game({"starting_cash": 100_000})
    await submit(game, "P1", 1, order_spot=4321)
    for sid in ("p2", "host-1"):
        got = await ask(world, sid, game.code)
        assert not contains_value(got[0][2], 4321)
    await submit(game, "P2", 1)  # the day resolves; P1's decision is history
    got = await ask(world, "p2", game.code)
    assert not contains_value(got[0][2], 4321)
    assert [rec["order_spot"] for rec in got[0][2]["own_history"]] == [0]


# ---------------------------------------------------------------- before start
@aio
async def test_before_the_start_host_and_player_get_a_lobby_update(
    world: World,
) -> None:
    code = await lobby(world)
    for sid in ("host-1", "p1"):
        before = await world.room(code)
        got = await ask(world, sid, code)
        reaching = [r for r in got if r[0] == sid or (r[0] == code and r[3] != sid)]
        assert [r[1] for r in reaching] == ["lobby_update"]
        assert len(got) == 1
        payload = reaching[0][2]
        assert payload["state"] == "lobby"
        assert [p["alias"] for p in payload["participants"]] == ["P1", "P2"]
        assert "can_start" in payload and "start_blocked_reason" in payload
        assert payload["seq"] == before["seq"] + 1
        assert (await world.room(code))["seq"] == before["seq"] + 1
    room = await world.room(code)
    assert room["state"] == "lobby" and room["engine"] is None


# -------------------------------------------------------------------- finished
@aio
async def test_after_the_end_host_and_player_get_full_states(
    world: World,
) -> None:
    game = await world.running_game()
    await play_day(game, 1)
    await world.finish_early(game.code)
    room = await world.room(game.code)

    got = await ask(world, "host-1", game.code)
    assert [(r[0], r[1]) for r in got] == [("host-1", "host_state")]
    assert got[0][2]["full"] is True
    assert got[0][2]["state"] == "finished"
    assert len(got[0][2]["public_history"]) == 1

    got = await ask(world, "p1", game.code)
    assert [(r[0], r[1]) for r in got] == [("p1", "your_state")]
    assert got[0][2]["full"] is True
    assert got[0][2]["final_settlement"] is not None
    assert norm(strip(got[0][2])) == norm(room_engine(room).player_view("P1", True))


# ------------------------------------------------------------------- strangers
@aio
async def test_anyone_else_gets_not_in_room(world: World) -> None:
    game = await world.running_game()
    await world.connect("ghost", 77)
    for sid, code in (("ghost", game.code), ("never-connected", game.code)):
        before = await world.room(game.code)
        got = await ask(world, sid, code)
        assert [(r[0], r[1]) for r in got] == [(sid, "error")]
        assert got[0][2]["code"] == "NOT_IN_ROOM"
        assert got[0][2]["message"] == MSG_NOT_IN_ROOM
        assert await world.room(game.code) == before
    got = await ask(world, "p1", "QQQQQQ")
    assert got[0][1] == "error" and got[0][2]["code"] == "NOT_IN_ROOM"


@aio
async def test_a_seat_that_dropped_its_sid_is_a_stranger_there(
    world: World,
) -> None:
    game = await world.running_game()
    await disconnect("p2")
    got = await ask(world, "p2", game.code)
    assert [(r[0], r[1]) for r in got] == [("p2", "error")]
    assert got[0][2]["code"] == "NOT_IN_ROOM"
    # the new sid of the same player is served
    await world.seat(game.code, "p2b", 2, token=game.tokens["P2"])
    got = await ask(world, "p2b", game.code)
    assert got[0][1] == "your_state" and got[0][2]["alias"] == "P2"


@aio
async def test_a_player_state_holds_no_host_secret_or_seed(world: World) -> None:
    game = await world.running_game({"random_seed": 918273645})
    got = await ask(world, "p1", game.code)
    assert got[0][1] == "your_state"
    assert not contains_key(got[0][2], "host_secret")
    assert not contains_key(got[0][2], "random_seed")
    assert not contains_value(got[0][2], 918273645)
    assert not contains_value(got[0][2], game.secret)
