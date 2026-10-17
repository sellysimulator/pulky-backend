"""Black-box tests for join, leave and reconnect (section 15 3.3, 3.4, 3.7)."""

import asyncio
import copy
import re

import pytest

from app.services import game_service
from app.services.room_service import (
    STATUS_FINISHED,
    STATUS_FULL,
    STATUS_NOT_FOUND,
    STATUS_STARTED,
    sanitise_display_name,
)
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import config_update, join
from tests.test_sockets.conftest import World, norm

aio = pytest.mark.asyncio
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


# ----------------------------------------------------------------- new seats
@aio
async def test_new_seat_is_stored_and_announced(world: World) -> None:
    code = await world.create_room()
    start = world.mark()
    sent = await world.join(code, "a", 1, name="La Chula")

    joined = [d for d in sent if "alias" in d]
    assert len(joined) == 1
    assert set(joined[0]) == {"alias", "session_token", "display_name"}
    assert joined[0]["alias"] == "P1"
    assert joined[0]["display_name"] == "La Chula"
    assert TOKEN_RE.match(joined[0]["session_token"])

    room = await world.room(code)
    seat = room["participants"]["P1"]
    assert seat["alias"] == "P1"
    assert seat["identity"] == world.sm.sid_to_identity["a"]
    assert seat["session_token"] == joined[0]["session_token"]
    assert seat["display_name"] == "La Chula"
    assert seat["connected"] is True
    assert room["sid_to_alias"]["a"] == "P1"
    assert world.sm.sid_to_alias["a"] == (code, "P1")

    lobby = world.sent(code, "lobby_update", start)
    assert len(lobby) == 1
    assert lobby[0]["state"] == "lobby"
    assert lobby[0]["participants"] == [
        {"alias": "P1", "display_name": "La Chula", "connected": True}
    ]
    assert world.sent(code, "joined", start) == []  # joined is never a room emit


@aio
async def test_display_name_defaults_and_is_sanitised(world: World) -> None:
    code = await world.create_room()
    assert (await world.seat(code, "a", 1))["display_name"] == "Pulquería P1"
    named = await world.seat(code, "b", 2, name="  La\x07 Chula  ")
    assert named["display_name"] == "La Chula"
    raw = "y" * 60
    long = await world.seat(code, "c", 3, name=raw)
    assert long["display_name"] == sanitise_display_name(raw, "Pulquería P3")
    assert long["display_name"] == "y" * 24
    blank = await world.seat(code, "d", 4, name="   ")
    assert blank["display_name"] == "Pulquería P4"


@aio
async def test_room_code_is_case_insensitive_and_echoed_canonically(
    world: World,
) -> None:
    code = await world.create_room()
    await world.seat(code.lower(), "a", 1)
    room = await world.room(code)
    assert sorted(room["participants"]) == ["P1"]
    cached_code, cached_alias = world.sm.sid_to_alias["a"]
    assert cached_code.upper() == code and cached_alias == "P1"
    assert world.sent(code, "lobby_update")


@aio
async def test_missing_room_is_not_found(world: World) -> None:
    sent = await world.join("NOPE99", "a", 1)
    assert sent == [{"message": STATUS_NOT_FOUND}]


# ------------------------------------------------------------- idempotency
@aio
@pytest.mark.parametrize("who", [7, "firebase-uid-7"], ids=["guest", "firebase"])
async def test_one_identity_joining_three_times_seats_one_participant(
    world: World, who: int | str
) -> None:
    # Failure mode 1 and the section 4 example.
    code = await world.create_room()
    first = await world.seat(code, "a", who)
    second = await world.seat(code, "b", who)  # fresh sid, no token
    third = await world.seat(code, "c", who, token=first["session_token"])

    room = await world.room(code)
    assert list(room["participants"]) == ["P1"]
    assert first["alias"] == second["alias"] == third["alias"] == "P1"
    assert first["session_token"] == second["session_token"] == third["session_token"]
    # the room is not full for a stranger
    assert (await world.seat(code, "z", 99))["alias"] == "P2"


@aio
async def test_concurrent_joins_from_one_identity_seat_one_participant(
    world: World,
) -> None:
    code = await world.create_room()
    for sid in ("a", "b", "c"):
        await world.connect(sid, 5)
    await asyncio.gather(*(join(sid, {"room_id": code}) for sid in ("a", "b", "c")))
    room = await world.room(code)
    assert list(room["participants"]) == ["P1"]
    assert len(room["sid_to_alias"]) == 1


@aio
async def test_concurrent_joins_never_overfill_or_duplicate_aliases(
    world: World,
) -> None:
    code = await world.create_room()  # four seats by default
    for n in range(1, 7):
        await world.connect(f"s{n}", n)
    start = world.mark()
    await asyncio.gather(*(join(f"s{n}", {"room_id": code}) for n in range(1, 7)))
    room = await world.room(code)
    assert sorted(room["participants"]) == ["P1", "P2", "P3", "P4"]
    assert len({p["identity"] for p in room["participants"].values()}) == 4
    refused = [
        world.sent(f"s{n}", "join_error", start)
        for n in range(1, 7)
        if world.sent(f"s{n}", "join_error", start)
    ]
    assert len(refused) == 2
    assert all(r == [{"message": STATUS_FULL}] for r in refused)


@aio
async def test_a_later_join_keeps_the_stored_display_name(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "a", 1, name="Alice")
    again = await world.seat(code, "b", 1, name="Bob")
    assert again["display_name"] == "Alice"
    assert (await world.room(code))["participants"]["P1"]["display_name"] == "Alice"


@aio
async def test_an_unknown_token_with_the_right_identity_still_finds_the_seat(
    world: World,
) -> None:
    code = await world.create_room()
    await world.seat(code, "a", 1)
    again = await world.seat(code, "b", 1, token="0" * 32)
    assert again["alias"] == "P1"
    assert list((await world.room(code))["participants"]) == ["P1"]


@aio
async def test_stolen_token_never_takes_over_the_seat(world: World) -> None:
    # Failure mode 3.
    code = await world.create_room()
    victim = await world.seat(code, "p1", 1)
    before = copy.deepcopy((await world.room(code))["participants"]["P1"])
    thief = await world.seat(code, "evil", 2, token=victim["session_token"])

    assert thief["alias"] == "P2"
    assert thief["session_token"] != victim["session_token"]
    room = await world.room(code)
    assert room["participants"]["P1"] == before
    assert room["sid_to_alias"]["p1"] == "P1"
    assert room["sid_to_alias"]["evil"] == "P2"
    assert room["participants"]["P2"]["identity"] == world.sm.sid_to_identity["evil"]


@aio
async def test_stolen_token_in_a_full_room_is_refused(world: World) -> None:
    code = await world.create_room({"max_players": 2})
    victim = await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    before = copy.deepcopy((await world.room(code))["participants"])
    sent = await world.join(code, "evil", 3, token=victim["session_token"])

    assert sent == [{"message": STATUS_FULL}]
    room = await world.room(code)
    assert room["participants"] == before
    assert "evil" not in room["sid_to_alias"]
    assert room["sid_to_alias"]["p1"] == "P1"


# ------------------------------------------------------------ alias choice
@aio
async def test_lowest_free_alias_is_reissued_and_other_seats_are_untouched(
    world: World,
) -> None:
    # Failure mode 2 and the section 4 example.
    code = await world.create_room()
    for n in (1, 2, 3):
        await world.seat(code, f"p{n}", n)
    p3 = copy.deepcopy((await world.room(code))["participants"]["P3"])
    await world.leave(code, "p2")

    joined = await world.seat(code, "p4", 4)
    assert joined["alias"] == "P2"
    room = await world.room(code)
    assert room["participants"]["P3"] == p3
    assert room["participants"]["P3"]["identity"] == world.sm.sid_to_identity["p3"]
    assert room["participants"]["P2"]["identity"] == world.sm.sid_to_identity["p4"]
    assert sorted(room["participants"]) == ["P1", "P2", "P3"]
    assert room["sid_to_alias"]["p3"] == "P3"
    lobby = world.last(code, "lobby_update")
    assert [p["alias"] for p in lobby["participants"]] == ["P1", "P2", "P3"]


# ----------------------------------------------------------------- refusals
@aio
async def test_full_room_refuses_new_seats_but_not_a_returning_player(
    world: World,
) -> None:
    code = await world.create_room({"max_players": 2})
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    sent = await world.join(code, "p3", 3)
    assert sent == [{"message": STATUS_FULL}]
    assert sorted((await world.room(code))["participants"]) == ["P1", "P2"]
    assert (await world.seat(code, "p1b", 1))["alias"] == "P1"


@aio
async def test_running_game_refuses_new_seats(world: World) -> None:
    game = await world.running_game()
    before = copy.deepcopy((await world.room(game.code))["participants"])
    sent = await world.join(game.code, "late", 9)
    assert sent == [{"message": STATUS_STARTED}]
    room = await world.room(game.code)
    assert room["participants"] == before
    assert "late" not in room["sid_to_alias"]


@aio
async def test_finished_game_refuses_new_seats(world: World) -> None:
    game = await world.running_game()
    await world.finish_early(game.code)
    sent = await world.join(game.code, "late", 9)
    assert sent == [{"message": STATUS_FINISHED}]
    assert sorted((await world.room(game.code))["participants"]) == ["P1", "P2"]


# --------------------------------------------------------------- reconnects
@aio
async def test_reconnect_drops_the_stale_sid(world: World) -> None:
    # Failure mode 9, first half.
    code = await world.create_room()
    await world.seat(code, "a", 1)
    await world.seat(code, "b", 1)
    room = await world.room(code)
    assert room["sid_to_alias"] == {"b": "P1"}
    assert "a" not in world.sm.sid_to_alias
    assert world.sm.sid_to_alias["b"] == (code, "P1")


@aio
async def test_stale_sid_disconnecting_later_does_not_disconnect_the_player(
    world: World,
) -> None:
    # Failure mode 9, second half.
    code = await world.create_room()
    await world.seat(code, "a", 1)
    await world.seat(code, "b", 1)
    start = world.mark()
    await disconnect("a")

    room = await world.room(code)
    assert room["participants"]["P1"]["connected"] is True
    assert room["sid_to_alias"] == {"b": "P1"}
    assert world.sent(code, "participant_disconnected", start) == []


@aio
async def test_participant_reconnected_only_after_a_disconnect(world: World) -> None:
    code = await world.create_room()
    first = await world.seat(code, "a", 1)
    start = world.mark()
    await world.seat(code, "a2", 1)  # still connected: not a reconnect
    assert world.sent(code, "participant_reconnected", start) == []

    await disconnect("a2")
    assert (await world.room(code))["participants"]["P1"]["connected"] is False
    start = world.mark()
    await world.seat(code, "a3", 1, token=first["session_token"])

    events = world.sent(code, "participant_reconnected", start)
    assert len(events) == 1
    assert set(events[0]) == {"seq", "alias", "display_name"}
    assert events[0]["alias"] == "P1"
    assert events[0]["display_name"] == "Pulquería P1"
    assert (await world.room(code))["participants"]["P1"]["connected"] is True
    lobby = world.sent(code, "lobby_update", start)
    assert lobby and lobby[-1]["participants"][0]["connected"] is True


@aio
async def test_reconnect_mid_game_receives_a_full_resync(world: World) -> None:
    game = await world.running_game()
    await disconnect("p1")
    start = world.mark()
    await world.connect("p1b", 1)
    await join("p1b", {"room_id": game.code, "session_token": game.tokens["P1"]})

    seen = world.reaching("p1b", game.code, start)
    names = ["joined", "game_started", "day_opened", "your_state"]
    assert [e for e, _ in seen if e in names + ["lobby_update"]] == names
    by_event = dict(seen)
    assert by_event["joined"]["alias"] == "P1"
    assert by_event["joined"]["session_token"] == game.tokens["P1"]
    assert by_event["game_started"]["participants"]

    room = await world.room(game.code)
    engine = game_service.load_engine(room)
    assert engine is not None
    assert by_event["day_opened"]["brief"] == norm(engine.brief())
    state = by_event["your_state"]
    assert state["full"] is True
    expected = {"seq": state["seq"], "full": True, **engine.player_view("P1", True)}
    assert state == norm(expected)
    assert "own_history" in state and "public_history" in state

    seqs = [by_event[e]["seq"] for e in names[1:]]
    assert seqs == [seqs[0], seqs[0] + 1, seqs[0] + 2]
    reconnected = world.sent(game.code, "participant_reconnected", start)
    assert reconnected and reconnected[0]["seq"] < seqs[0]
    # nobody else gets this player's private state
    assert world.sent("p2", "your_state", start) == []
    assert world.sent(game.code, "your_state", start) == []


@aio
async def test_reconnect_after_the_end_receives_game_finished(world: World) -> None:
    game = await world.running_game()
    await world.finish_early(game.code, game_id="pub-777")
    await disconnect("p1")
    start = world.mark()
    await world.connect("p1b", 1)
    await join("p1b", {"room_id": game.code, "session_token": game.tokens["P1"]})

    seen = world.reaching("p1b", game.code, start)
    names = ["joined", "game_started", "your_state", "game_finished", "game_persisted"]
    assert [e for e, _ in seen if e in names] == names
    by_event = dict(seen)
    room = await world.room(game.code)
    engine = game_service.load_engine(room)
    assert engine is not None
    assert by_event["your_state"]["full"] is True
    finished = {
        k: v
        for k, v in by_event["game_finished"].items()
        if k not in ("seq", "game_id")
    }
    assert finished == norm(engine.results())
    assert by_event["game_persisted"]["game_id"] == "pub-777"
    seqs = [by_event[e]["seq"] for e in names[1:]]
    assert seqs == sorted(set(seqs))
    assert (await world.room(game.code))["state"] == "finished"


@aio
async def test_reconnect_after_the_end_without_a_game_id(world: World) -> None:
    game = await world.running_game()
    await world.finish_early(game.code, game_id=None)
    await world.connect("p1b", 1)
    start = world.mark()
    await join("p1b", {"room_id": game.code})
    events = world.reaching_events("p1b", game.code, start)
    assert "game_finished" in events
    assert "game_persisted" not in events


@aio
async def test_session_token_is_sent_only_in_joined(world: World) -> None:
    code = await world.create_room()
    joined = await world.seat(code, "a", 1)
    await world.seat(code, "b", 2)
    token = joined["session_token"]
    hits = [r for r in world.sm.records if token in str(r[2])]
    assert [(r[0], r[1]) for r in hits] == [("a", "joined")]


# ---------------------------------------------------------------------- leave
@aio
async def test_leave_in_the_lobby_removes_the_seat(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    start = world.mark()
    await world.leave(code, "p2")

    assert world.sent("p2", since=start) == [{"room_id": code}]
    assert world.last("p2", "leave_ack") == {"room_id": code}
    room = await world.room(code)
    assert sorted(room["participants"]) == ["P1"]
    assert "p2" not in room["sid_to_alias"]
    assert "p2" not in world.sm.sid_to_alias
    lobby = world.sent(code, "lobby_update", start)
    assert [p["alias"] for p in lobby[-1]["participants"]] == ["P1"]


@aio
async def test_leave_accepts_a_lowercase_room_code(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    await world.leave(code.lower(), "p1")
    ack = world.last("p1", "leave_ack")
    assert set(ack) == {"room_id"}
    assert ack["room_id"].upper() == code
    assert (await world.room(code))["participants"] == {}


@aio
async def test_leaving_is_idempotent(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    await world.leave(code, "p2")
    snapshot = copy.deepcopy(await world.room(code))
    start = world.mark()
    await world.leave(code, "p2")

    assert world.sent("p2", since=start) == [{"room_id": code}]
    assert world.sent(code, since=start) == []
    assert await world.room(code) == snapshot


@aio
async def test_leave_without_a_seat_changes_nothing(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    await world.connect("stranger", 2)
    snapshot = copy.deepcopy(await world.room(code))
    start = world.mark()
    await world.leave(code, "stranger")
    assert world.sent("stranger", since=start) == [{"room_id": code}]
    assert world.sent(code, since=start) == []
    assert await world.room(code) == snapshot


@aio
async def test_leave_from_a_stale_sid_keeps_the_seat(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "a", 1)
    await world.seat(code, "b", 1)
    await world.leave(code, "a")
    assert world.last("a", "leave_ack") == {"room_id": code}
    room = await world.room(code)
    assert list(room["participants"]) == ["P1"]
    assert room["sid_to_alias"] == {"b": "P1"}


@aio
async def test_leave_of_a_missing_room_is_acknowledged(world: World) -> None:
    await world.connect("a", 1)
    await world.leave("NOPE99", "a")
    assert world.sent("a", "leave_ack") == [{"room_id": "NOPE99"}]
    assert world.sent("a", "join_error") == []


@aio
async def test_leave_is_allowed_while_configuring(world: World) -> None:
    code = await world.create_room()
    secret = (await world.room(code))["host_secret"]
    await world.claim_host(code, "host-1", 900)
    await world.seat(code, "p1", 1)
    await config_update(
        "host-1",
        {"room_id": code, "host_secret": secret, "config": {"duration_days": 20}},
    )
    assert (await world.room(code))["state"] == "configuring"
    await world.leave(code, "p1")
    assert (await world.room(code))["participants"] == {}
    assert world.last("p1", "leave_ack") == {"room_id": code}


@aio
async def test_leave_after_the_start_is_refused_and_the_seat_stays(
    world: World,
) -> None:
    game = await world.running_game()
    before = copy.deepcopy(await world.room(game.code))
    start = world.mark()
    await world.leave(game.code, "p1")
    assert world.sent("p1", since=start) == [{"message": STATUS_STARTED}]
    assert world.sent("p1", "leave_ack", start) == []
    after = await world.room(game.code)
    assert after["participants"] == before["participants"]
    assert after["sid_to_alias"] == before["sid_to_alias"]

    await world.finish_early(game.code)
    start = world.mark()
    await world.leave(game.code, "p2")
    assert world.sent("p2", since=start) == [{"message": STATUS_FINISHED}]
    assert sorted((await world.room(game.code))["participants"]) == ["P1", "P2"]


@aio
async def test_leave_of_a_seatless_sid_in_a_running_game_is_still_acknowledged(
    world: World,
) -> None:
    game = await world.running_game()
    await world.connect("stranger", 9)
    start = world.mark()
    await world.leave(game.code, "stranger")
    assert world.sent("stranger", since=start) == [{"room_id": game.code}]
