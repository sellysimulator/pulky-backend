"""Black-box tests for the host claim (section 15 3.2, criterion 2)."""

import pytest

from app.services import game_service
from app.services.room_service import STATUS_NOT_FOUND
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import join_waiting
from tests.test_sockets.conftest import World, guest, norm

aio = pytest.mark.asyncio
ONLY_HOST = "Only the host can do that."


@aio
async def test_claim_by_secret_sends_the_emits_in_order(world: World) -> None:
    code = await world.create_room()
    await world.connect("host-1", 900)
    room = await world.room(code)
    start = world.mark()
    await join_waiting("host-1", {"room_id": code, "host_secret": room["host_secret"]})

    seen = world.reaching("host-1", code, start)
    assert [e for e, _ in seen] == ["host_claimed", "lobby_update", "config_updated"]
    claimed = seen[0][1]
    assert claimed == {"room_id": code, "host_secret": room["host_secret"]}
    lobby, config = seen[1][1], seen[2][1]
    assert lobby["state"] == "lobby"
    assert lobby["participants"] == []
    assert config["config"] == room["config"]  # the full config, secrets included
    assert isinstance(lobby["seq"], int) and isinstance(config["seq"], int)
    assert lobby["seq"] < config["seq"]


@aio
async def test_claim_records_the_host_and_never_gives_an_alias(world: World) -> None:
    code = await world.create_room()
    await world.claim_host(code, "host-1", 900)
    room = await world.room(code)
    assert room["host_sid"] == "host-1"
    assert room["host_identity"] == world.sm.sid_to_identity["host-1"]
    assert room["participants"] == {}
    assert "host-1" not in room["sid_to_alias"]
    assert "host-1" not in world.sm.sid_to_alias
    assert world.sm.sid_to_host_room["host-1"] == code


@aio
async def test_host_takes_no_seat(world: World) -> None:
    # Failure mode 4: the host counted as a player is an off-by-one.
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    before = (await world.room(code))["participants"]
    start = world.mark()
    await world.claim_host(code, "host-1", 900)

    room = await world.room(code)
    assert room["participants"] == before
    assert sorted(room["participants"]) == ["P1", "P2"]
    assert "host-1" not in room["sid_to_alias"]
    assert "host-1" not in world.sm.sid_to_alias
    lobby = [d for e, d in world.reaching("host-1", code, start) if e == "lobby_update"]
    assert [p["alias"] for p in lobby[-1]["participants"]] == ["P1", "P2"]
    # A third and fourth player still fit in the default four seats.
    assert (await world.seat(code, "p3", 3))["alias"] == "P3"
    assert (await world.seat(code, "p4", 4))["alias"] == "P4"


@aio
async def test_claim_by_recorded_identity_needs_no_secret(world: World) -> None:
    code = await world.create_room()
    await world.claim_host(code, "host-1", 900)
    await world.connect("host-2", 900)  # a new tab, the same person
    start = world.mark()
    await join_waiting("host-2", {"room_id": code})

    seen = world.reaching("host-2", code, start)
    assert [e for e, _ in seen][:3] == [
        "host_claimed",
        "lobby_update",
        "config_updated",
    ]
    assert (await world.room(code))["host_sid"] == "host-2"
    assert world.sm.sid_to_host_room["host-2"] == code


@aio
async def test_claim_by_identity_works_for_a_firebase_uid(world: World) -> None:
    code = await world.create_room()
    await world.claim_host(code, "host-1", "firebase-host-uid")
    await world.connect("host-2", "firebase-host-uid")
    start = world.mark()
    await join_waiting("host-2", {"room_id": code})
    assert "host_claimed" in world.reaching_events("host-2", code, start)


@aio
async def test_identity_is_recorded_only_while_empty(world: World) -> None:
    code = await world.create_room()
    assert (await world.room(code))["host_identity"] in (None, "")
    await world.claim_host(code, "host-1", 900)
    first = world.sm.sid_to_identity["host-1"]
    # Someone else holding the secret claims the room: host_identity stays.
    await world.claim_host(code, "host-2", 901)
    room = await world.room(code)
    assert room["host_identity"] == first
    assert room["host_sid"] == "host-2"


@aio
async def test_other_identity_without_the_secret_is_refused(world: World) -> None:
    code = await world.create_room()
    await world.claim_host(code, "host-1", 900)
    await world.connect("evil", 901)
    start = world.mark()
    await join_waiting("evil", {"room_id": code})

    assert world.sent("evil", since=start) == [{"message": ONLY_HOST}]
    assert world.sent("evil", "host_claimed", start) == []
    room = await world.room(code)
    assert room["host_sid"] == "host-1"
    assert "evil" not in world.sm.sid_to_host_room


@aio
async def test_wrong_secret_from_another_identity_is_refused(world: World) -> None:
    code = await world.create_room()
    await world.claim_host(code, "host-1", 900)
    await world.connect("evil", 901)
    start = world.mark()
    await join_waiting("evil", {"room_id": code, "host_secret": "guess"})
    assert world.sent("evil", "join_error", start) == [{"message": ONLY_HOST}]
    assert world.sent("evil", "host_claimed", start) == []
    assert (await world.room(code))["host_sid"] == "host-1"


@aio
@pytest.mark.parametrize(
    "payload", [{}, {"host_secret": ""}, {"host_secret": None}], ids=str
)
async def test_empty_secret_claim_is_refused(world: World, payload: dict) -> None:
    # Failure mode 5: compare_digest("", "") is True without the bool() guard.
    code = await world.create_room()
    await world.claim_host(code, "host-1", 900)

    def blank(room: dict) -> None:
        room["host_secret"] = ""

    await world.mutate(code, blank)
    await world.connect("evil", 901)
    start = world.mark()
    await join_waiting("evil", {"room_id": code, **payload})

    assert world.sent("evil", "host_claimed", start) == []
    assert world.sent("evil", "join_error", start) == [{"message": ONLY_HOST}]
    assert (await world.room(code))["host_sid"] == "host-1"


@aio
async def test_missing_room_is_not_found(world: World) -> None:
    await world.connect("host-1", 900)
    await join_waiting("host-1", {"room_id": "NOPE99", "host_secret": "x"})
    assert world.sent("host-1") == [{"message": STATUS_NOT_FOUND}]


@aio
async def test_room_code_is_case_insensitive(world: World) -> None:
    code = await world.create_room()
    secret = (await world.room(code))["host_secret"]
    await world.connect("host-1", 900)
    await join_waiting("host-1", {"room_id": code.lower(), "host_secret": secret})
    claimed = world.last("host-1", "host_claimed")
    assert claimed["room_id"].upper() == code
    assert (await world.room(code))["host_sid"] == "host-1"


@aio
async def test_host_reconnected_only_when_a_dropped_host_returns(
    world: World,
) -> None:
    code = await world.create_room()
    start = world.mark()
    await world.claim_host(code, "host-1", 900)
    assert world.sent(code, "host_reconnected", start) == []  # first claim

    # A second tab while the first is still the host: not a reconnect.
    mark = world.mark()
    await world.claim_host(code, "host-2", 900)
    assert world.sent(code, "host_reconnected", mark) == []

    # The host drops, then returns.
    await disconnect("host-2")
    assert (await world.room(code))["host_sid"] is None
    mark = world.mark()
    await world.claim_host(code, "host-3", 900)
    events = world.sent(code, "host_reconnected", mark)
    assert len(events) == 1
    assert isinstance(events[0]["seq"], int)
    assert set(events[0]) == {"seq"}
    assert (await world.room(code))["host_sid"] == "host-3"


@aio
async def test_claim_of_a_running_room_resyncs_the_host(world: World) -> None:
    game = await world.running_game()
    await disconnect(game.host_sid)
    await world.connect("host-2", 900)
    start = world.mark()
    await join_waiting("host-2", {"room_id": game.code, "host_secret": game.secret})

    seen = world.reaching("host-2", game.code, start)
    events = [e for e, _ in seen if e != "host_reconnected"]
    assert events == ["host_claimed", "lobby_update", "config_updated", "host_state"]
    state = dict(seen)["host_state"]
    assert state["full"] is True
    room = await world.room(game.code)
    engine = game_service.load_engine(room)
    assert engine is not None
    assert state["state"] == "running"
    for alias, view in engine.host_view(True)["players"].items():
        assert state["players"][alias]["display_name"] == view["display_name"]
        assert "own_history" in state["players"][alias]
    assert "public_history" in state and "market_history" in state
    assert state["seq"] > dict(seen)["config_updated"]["seq"]


@aio
async def test_claim_of_a_finished_room_resyncs_the_host(world: World) -> None:
    game = await world.running_game()
    await world.finish_early(game.code, game_id="pub-1234")
    await disconnect(game.host_sid)
    await world.connect("host-2", 900)
    start = world.mark()
    await join_waiting("host-2", {"room_id": game.code, "host_secret": game.secret})

    seen = world.reaching("host-2", game.code, start)
    events = [e for e, _ in seen if e != "host_reconnected"]
    assert events == [
        "host_claimed",
        "lobby_update",
        "config_updated",
        "host_state",
        "game_finished",
        "game_persisted",
    ]
    room = await world.room(game.code)
    engine = game_service.load_engine(room)
    assert engine is not None
    by_event = dict(seen)
    assert by_event["host_state"]["full"] is True
    assert by_event["host_state"]["state"] == "finished"
    finished = {
        k: v
        for k, v in by_event["game_finished"].items()
        if k not in ("seq", "game_id")
    }
    assert finished == norm(engine.results())
    assert by_event["game_persisted"]["game_id"] == "pub-1234"
    seqs = [
        by_event[e]["seq"] for e in ("host_state", "game_finished", "game_persisted")
    ]
    assert seqs == sorted(set(seqs))


@aio
async def test_finished_host_resync_without_a_game_id_skips_persisted(
    world: World,
) -> None:
    game = await world.running_game()
    await world.finish_early(game.code, game_id=None)
    await world.connect("host-2", 900)
    start = world.mark()
    await join_waiting("host-2", {"room_id": game.code, "host_secret": game.secret})
    events = world.reaching_events("host-2", game.code, start)
    assert "game_finished" in events
    assert "game_persisted" not in events


@aio
async def test_lobby_claim_has_no_resync(world: World) -> None:
    code = await world.create_room()
    start = world.mark()
    await world.claim_host(code, "host-1", 900)
    events = world.reaching_events("host-1", code, start)
    assert "host_state" not in events
    assert "game_finished" not in events
    assert world.sm.sid_to_identity["host-1"] == guest(900)
