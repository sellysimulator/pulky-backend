"""Black-box tests for disconnect bookkeeping (section 15 3.8, criterion 9)."""

import copy

import pytest

from app.services import game_service
from app.sockets.handlers.connection import disconnect
from tests.test_sockets.conftest import World, norm

aio = pytest.mark.asyncio


@aio
async def test_player_disconnect_marks_the_seat_and_tells_the_room(
    world: World,
) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2, name="La Chula")
    prior = (await world.room(code))["seq"]
    start = world.mark()
    await disconnect("p2")

    room = await world.room(code)
    assert room["participants"]["P2"]["connected"] is False
    assert room["participants"]["P1"]["connected"] is True
    assert room["sid_to_alias"] == {"p1": "P1"}
    assert "p2" not in world.sm.sid_to_alias
    assert "p2" not in world.sm.sid_to_identity
    events = world.sent(code, "participant_disconnected", start)
    assert len(events) == 1
    assert set(events[0]) == {"seq", "alias", "display_name"}
    assert events[0]["alias"] == "P2"
    assert events[0]["display_name"] == "La Chula"
    assert events[0]["seq"] > prior
    assert room["seq"] >= events[0]["seq"]


@aio
async def test_each_disconnect_takes_a_new_seq(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    start = world.mark()
    await disconnect("p1")
    await disconnect("p2")
    seqs = [e["seq"] for e in world.sent(code, "participant_disconnected", start)]
    assert len(seqs) == 2
    assert seqs[1] == seqs[0] + 1


@aio
async def test_the_seat_survives_a_disconnect_in_the_lobby(world: World) -> None:
    code = await world.create_room()
    joined = await world.seat(code, "p1", 1)
    await disconnect("p1")
    assert list((await world.room(code))["participants"]) == ["P1"]
    again = await world.seat(code, "p1b", 1, token=joined["session_token"])
    assert again["alias"] == "P1"
    assert (await world.room(code))["participants"]["P1"]["connected"] is True


@aio
async def test_host_disconnect_clears_the_host_sid(world: World) -> None:
    code = await world.create_room()
    await world.claim_host(code, "host-1", 900)
    await world.seat(code, "p1", 1)
    prior = (await world.room(code))["seq"]
    start = world.mark()
    await disconnect("host-1")

    room = await world.room(code)
    assert room["host_sid"] is None
    assert room["host_identity"]  # recovery still possible
    assert "host-1" not in world.sm.sid_to_host_room
    assert "host-1" not in world.sm.sid_to_identity
    events = world.sent(code, "host_disconnected", start)
    assert len(events) == 1
    assert set(events[0]) == {"seq"}
    assert events[0]["seq"] > prior
    assert world.sent(code, "participant_disconnected", start) == []
    assert room["participants"]["P1"]["connected"] is True


@aio
async def test_a_stale_host_sid_disconnecting_leaves_the_host_connected(
    world: World,
) -> None:
    code = await world.create_room()
    await world.claim_host(code, "host-1", 900)
    await world.claim_host(code, "host-2", 900)  # a new tab takes over
    start = world.mark()
    await disconnect("host-1")
    assert (await world.room(code))["host_sid"] == "host-2"
    assert world.sent(code, "host_disconnected", start) == []


@aio
async def test_the_room_document_is_the_authority_not_the_cache(
    world: World,
) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    world.sm.sid_to_alias["ghost"] = (code, "P1")  # a cache entry the room lacks
    snapshot = copy.deepcopy(await world.room(code))
    start = world.mark()
    await disconnect("ghost")
    assert await world.room(code) == snapshot
    assert world.sent(code, "participant_disconnected", start) == []
    assert "ghost" not in world.sm.sid_to_alias


@aio
async def test_an_unknown_sid_does_nothing(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    snapshot = copy.deepcopy(await world.room(code))
    start = world.mark()
    await disconnect("never-seen")
    assert world.sm.records[start:] == []
    assert await world.room(code) == snapshot


@aio
async def test_a_connected_sid_with_no_seat_is_forgotten_quietly(
    world: World,
) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    await world.connect("watcher", 5)
    snapshot = copy.deepcopy(await world.room(code))
    start = world.mark()
    await disconnect("watcher")
    assert "watcher" not in world.sm.sid_to_identity
    assert world.sm.records[start:] == []
    assert await world.room(code) == snapshot


@aio
async def test_disconnects_never_block_pause_or_end_the_game(world: World) -> None:
    game = await world.running_game()
    room = await world.room(game.code)
    engine_before = copy.deepcopy(room["engine"])
    start = world.mark()
    await disconnect("p1")
    await disconnect(game.host_sid)
    await disconnect("p2")

    room = await world.room(game.code)
    assert room["state"] == "running"
    assert room["finished_at"] is None
    assert room["engine"] == engine_before
    assert all(not p["connected"] for p in room["participants"].values())
    assert room["host_sid"] is None
    engine = game_service.load_engine(room)
    assert engine is not None
    assert engine.day == 1
    assert engine.awaiting() == ["P1", "P2"]  # the day keeps waiting (D6)
    events = {r[1] for r in world.sm.records[start:]}
    assert events == {"participant_disconnected", "host_disconnected"}
    # and the offline picture reaches the host on its return
    state = game_service.host_state(room, 99, True)
    assert state["players"]["P1"]["connected"] is False
    assert norm(state["state"]) == "running"


@aio
async def test_a_player_can_come_back_to_a_running_game_after_a_drop(
    world: World,
) -> None:
    game = await world.running_game()
    await disconnect("p1")
    sent = await world.join(game.code, "p1b", 1, token=game.tokens["P1"])
    assert sent[0]["alias"] == "P1"
    room = await world.room(game.code)
    assert room["participants"]["P1"]["connected"] is True
    assert room["state"] == "running"
