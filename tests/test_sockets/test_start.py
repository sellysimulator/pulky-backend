"""Black-box tests for start_game and game_service (section 15 3.6, criterion 7)."""

import copy
import logging
import re
from typing import Any

import pytest

from app.core.config_models import GameConfig, config_public
from app.services import game_service
from app.services.room_service import (
    START_NEED_PLAYERS,
    STATUS_NOT_FOUND,
    Emit,
)
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import start_game
from tests.test_sockets.conftest import World, norm

aio = pytest.mark.asyncio
ONLY_HOST = "Only the host can do that."
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
START_EVENTS = ["game_started", "day_opened", "your_state", "host_state"]


def next_seq(room: dict) -> int:
    room["seq"] += 1
    return int(room["seq"])


async def lobby_with(
    world: World, players: int, patch: dict | None = None, tag: str = ""
) -> tuple[str, str]:
    offset = 100 if tag else 0
    code = await world.create_room(patch)
    secret = str((await world.room(code))["host_secret"])
    await world.claim_host(code, f"{tag}host-1", 900 + offset)
    for n in range(1, players + 1):
        await world.seat(code, f"{tag}p{n}", n + offset)
    return code, secret


def data_of(code: str, secret: str) -> dict[str, str]:
    return {"room_id": code, "host_secret": secret}


# ------------------------------------------------------------- the sequence
@aio
async def test_start_emits_the_sequence_in_order_with_increasing_seq(
    world: World,
) -> None:
    code, secret = await lobby_with(world, 2)
    before = (await world.room(code))["seq"]
    start = world.mark()
    await start_game("host-1", data_of(code, secret))

    records = [r for r in world.sm.records[start:] if r[1] in START_EVENTS]
    assert [r[1] for r in records] == [
        "game_started",
        "day_opened",
        "your_state",
        "your_state",
        "host_state",
    ]
    assert records[0][0] == records[1][0] == code
    assert {records[2][0], records[3][0]} == {"p1", "p2"}
    assert records[4][0] == "host-1"
    started, opened, mine, theirs, host = (r[2] for r in records)
    assert records[0][3] == "host-1"  # the host gets host_state, not game_started
    assert records[1][3] is None  # day_opened reaches the host too
    assert started["seq"] > before
    assert opened["seq"] == started["seq"] + 1
    assert mine["seq"] == theirs["seq"] == opened["seq"] + 1  # one shared seq
    assert host["seq"] > mine["seq"]
    assert (await world.room(code))["seq"] >= host["seq"]


@aio
async def test_start_payloads_match_the_engine(world: World) -> None:
    code, secret = await lobby_with(world, 2, {"duration_days": 20})
    start = world.mark()
    await start_game("host-1", data_of(code, secret))

    room = await world.room(code)
    engine = game_service.load_engine(room)
    assert engine is not None
    assert room["state"] == "running"
    assert engine.aliases == ("P1", "P2")
    assert engine.display_names == {"P1": "Pulquería P1", "P2": "Pulquería P2"}
    assert engine.seed == room["seed"]

    started = world.sent(code, "game_started", start)[0]
    assert started["duration_days"] == 20
    assert started["participants"] == [
        {"alias": "P1", "display_name": "Pulquería P1", "connected": True},
        {"alias": "P2", "display_name": "Pulquería P2", "connected": True},
    ]
    assert started["config"] == config_public(GameConfig.from_dict(room["config"]))

    opened = world.sent(code, "day_opened", start)[0]
    assert opened["brief"] == norm(engine.brief())

    for alias, sid in (("P1", "p1"), ("P2", "p2")):
        states = world.sent(sid, "your_state", start)
        assert len(states) == 1
        state = states[0]
        assert state["full"] is True
        assert state == norm(
            {"seq": state["seq"], "full": True, **engine.player_view(alias, True)}
        )

    host = world.sent("host-1", "host_state", start)
    assert len(host) == 1
    expected = {**engine.host_view(True), "seq": host[0]["seq"], "full": True}
    expected["state"] = "running"
    for alias, row in expected["players"].items():
        row["connected"] = room["participants"][alias]["connected"]
    assert host[0] == norm(expected)
    assert host[0]["full"] is True


@aio
async def test_game_started_skips_the_host_sid(world: World) -> None:
    # Failure mode 7: reaching the host, it would overwrite its full config.
    code, secret = await lobby_with(world, 2)
    start = world.mark()
    await start_game("host-1", data_of(code, secret))
    rooms = [r for r in world.sm.records[start:] if r[1] == "game_started"]
    assert len(rooms) == 1
    assert rooms[0][0] == code
    assert rooms[0][3] == "host-1"
    assert world.sent("host-1", "game_started", start) == []


@aio
async def test_seed_is_generated_when_the_config_has_none(world: World) -> None:
    code, secret = await lobby_with(world, 2)
    await start_game("host-1", data_of(code, secret))
    room = await world.room(code)
    assert isinstance(room["seed"], int) and 0 <= room["seed"] < 2**53
    assert room["config"]["random_seed"] is None  # the config is not rewritten
    other, other_secret = await lobby_with(world, 2, tag="b-")
    await start_game("b-host-1", data_of(other, other_secret))
    assert (await world.room(other))["seed"] != room["seed"]


@aio
async def test_a_configured_seed_is_used(world: World) -> None:
    code, secret = await lobby_with(world, 2, {"random_seed": 918273645})
    await start_game("host-1", data_of(code, secret))
    room = await world.room(code)
    engine = game_service.load_engine(room)
    assert engine is not None
    assert room["seed"] == 918273645 == engine.seed


@aio
async def test_started_at_is_a_utc_stamp_and_the_start_is_logged(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    code, secret = await lobby_with(world, 2)
    assert (await world.room(code))["started_at"] is None
    with caplog.at_level(logging.INFO):
        await start_game("host-1", data_of(code, secret))
    stamp = (await world.room(code))["started_at"]
    assert isinstance(stamp, str) and STAMP_RE.match(stamp)
    assert any(
        r.levelno == logging.INFO and r.getMessage() == f"room {code} started"
        for r in caplog.records
    )


@aio
async def test_players_who_are_offline_get_no_your_state(world: World) -> None:
    code, secret = await lobby_with(world, 2)
    await disconnect("p2")
    start = world.mark()
    await start_game("host-1", data_of(code, secret))
    assert (await world.room(code))["state"] == "running"
    assert len(world.sent("p1", "your_state", start)) == 1
    assert world.sent("p2", "your_state", start) == []
    started = world.sent(code, "game_started", start)[0]
    assert [p["connected"] for p in started["participants"]] == [True, False]


@aio
async def test_a_host_who_is_offline_gets_no_host_state(world: World) -> None:
    code, secret = await lobby_with(world, 2)
    await disconnect("host-1")
    await world.connect("anywhere", 777)
    start = world.mark()
    await start_game("anywhere", data_of(code, secret))
    assert (await world.room(code))["state"] == "running"
    assert world.sent("anywhere", "host_state", start) == []
    assert world.sent("host-1", "host_state", start) == []
    assert len(world.sent("p1", "your_state", start)) == 1


# ------------------------------------------------------------------ refusals
@aio
async def test_blocked_start_repeats_the_lobby_update_reason(world: World) -> None:
    # Failure mode 6.
    code, secret = await lobby_with(world, 1)
    lobby = world.last(code, "lobby_update")
    assert lobby["can_start"] is False
    assert lobby["start_blocked_reason"] == START_NEED_PLAYERS
    before = copy.deepcopy(await world.room(code))
    start = world.mark()
    await start_game("host-1", data_of(code, secret))

    sent = world.sent("host-1", since=start)
    assert sent == [{"message": lobby["start_blocked_reason"]}]
    assert sent[0]["message"] == START_NEED_PLAYERS
    assert await world.room(code) == before
    assert world.sent(code, since=start) == []


@aio
async def test_blocked_start_with_nobody_seated(world: World) -> None:
    code = await world.create_room()
    secret = str((await world.room(code))["host_secret"])
    await world.claim_host(code, "host-1", 900)
    lobby = [d for e, d in world.reaching("host-1", code) if e == "lobby_update"][-1]
    await start_game("host-1", data_of(code, secret))
    assert world.last("host-1", "join_error") == {
        "message": lobby["start_blocked_reason"]
    }
    assert (await world.room(code))["state"] == "lobby"


@aio
async def test_the_second_player_unblocks_the_start(world: World) -> None:
    code, secret = await lobby_with(world, 1)
    await world.seat(code, "p2", 2)
    lobby = world.last(code, "lobby_update")
    assert lobby["can_start"] is True
    assert lobby["start_blocked_reason"] is None
    await start_game("host-1", data_of(code, secret))
    assert (await world.room(code))["state"] == "running"


@aio
async def test_missing_room_is_not_found(world: World) -> None:
    await world.connect("host-1", 900)
    await start_game("host-1", data_of("NOPE99", "x"))
    assert world.sent("host-1") == [{"message": STATUS_NOT_FOUND}]


@aio
async def test_wrong_secret_is_refused(world: World) -> None:
    code, _ = await lobby_with(world, 2)
    before = copy.deepcopy(await world.room(code))
    start = world.mark()
    await start_game("host-1", data_of(code, "guess"))
    assert world.sent("host-1", since=start) == [{"message": ONLY_HOST}]
    assert await world.room(code) == before
    assert world.sent(code, since=start) == []


@aio
async def test_blank_secret_is_refused_even_for_a_blank_stored_secret(
    world: World,
) -> None:
    code, _ = await lobby_with(world, 2)

    def blank(room: dict) -> None:
        room["host_secret"] = ""

    await world.mutate(code, blank)
    start = world.mark()
    await start_game("host-1", {"room_id": code})
    await start_game("host-1", data_of(code, ""))
    assert world.sent("host-1", "join_error", start) == [{"message": ONLY_HOST}] * 2
    assert (await world.room(code))["state"] == "lobby"


@aio
async def test_host_identity_alone_cannot_start(world: World) -> None:
    code, _ = await lobby_with(world, 2)
    await world.claim_host(code, "host-2", 900, with_secret=False)
    start = world.mark()
    await start_game("host-2", {"room_id": code})
    assert world.sent("host-2", since=start) == [{"message": ONLY_HOST}]
    assert (await world.room(code))["state"] == "lobby"


@aio
async def test_a_double_start_is_a_silent_no_op(world: World) -> None:
    game = await world.running_game()
    before = copy.deepcopy(await world.room(game.code))
    start = world.mark()
    await start_game(game.host_sid, game.start_data())
    assert world.sm.records[start:] == []
    assert await world.room(game.code) == before

    await world.finish_early(game.code)
    before = copy.deepcopy(await world.room(game.code))
    start = world.mark()
    await start_game(game.host_sid, game.start_data())
    assert world.sm.records[start:] == []
    assert await world.room(game.code) == before


# -------------------------------------------------------------- game_service
@aio
async def test_load_and_store_engine_round_trip(world: World) -> None:
    code, _ = await lobby_with(world, 2)
    room = await world.room(code)
    assert game_service.load_engine(room) is None

    game = await world.running_game()
    room = await world.room(game.code)
    engine = game_service.load_engine(room)
    assert engine is not None
    engine.end_early()
    game_service.store_engine(room, engine)
    assert room["engine"] is not None
    again = game_service.load_engine(room)
    assert again is not None
    assert again.ended_early is True
    assert again.phase == engine.phase


@aio
async def test_display_names_maps_alias_to_name(world: World) -> None:
    code = await world.create_room()
    await world.seat(code, "a", 1, name="Alice")
    await world.seat(code, "b", 2)
    assert game_service.display_names(await world.room(code)) == {
        "P1": "Alice",
        "P2": "Pulquería P2",
    }


@aio
async def test_service_start_returns_emits_and_runs_the_room(world: World) -> None:
    code, _ = await lobby_with(world, 2)
    room = await world.room(code)
    prior = room["seq"]

    emits = game_service.start(room, next_seq)

    assert all(isinstance(e, Emit) for e in emits)
    assert room["state"] == "running"
    assert room["engine"] is not None
    assert isinstance(room["seed"], int)
    assert STAMP_RE.match(room["started_at"])
    names = [e.event for e in emits]
    assert names[:2] == ["game_started", "day_opened"]
    assert sorted(names) == sorted(
        ["game_started", "day_opened"] + ["your_state"] * 2 + ["host_state"]
    )
    assert names.index("host_state") == len(names) - 1
    by_event = {e.event: e for e in emits}
    assert by_event["game_started"].target == code
    assert by_event["game_started"].skip_sid == "host-1"
    assert by_event["day_opened"].target == code
    assert by_event["host_state"].target == "host-1"
    states = [e for e in emits if e.event == "your_state"]
    assert {e.target for e in states} == {"p1", "p2"}
    assert states[0].data["seq"] == states[1].data["seq"]
    assert by_event["game_started"].data["seq"] == prior + 1
    assert by_event["day_opened"].data["seq"] == prior + 2
    assert states[0].data["seq"] == prior + 3
    assert by_event["host_state"].data["seq"] > prior + 3
    assert room["seq"] == by_event["host_state"].data["seq"]


@aio
async def test_player_and_host_state_builders(world: World) -> None:
    game = await world.running_game()
    room = await world.room(game.code)
    engine = game_service.load_engine(room)
    assert engine is not None

    for full in (True, False):
        state = game_service.player_state(room, "P1", 42, full)
        assert state == norm(
            {"seq": 42, "full": full, **engine.player_view("P1", full)}
        )

        host = game_service.host_state(room, 43, full)
        expected: dict[str, Any] = {
            "seq": 43,
            "full": full,
            **engine.host_view(full),
            "state": room["state"],
        }
        for alias, row in expected["players"].items():
            row["connected"] = room["participants"][alias]["connected"]
        assert host == norm(expected)


@aio
async def test_host_state_reports_a_disconnected_player(world: World) -> None:
    game = await world.running_game()
    await disconnect("p2")
    room = await world.room(game.code)
    state = game_service.host_state(room, 50, True)
    assert state["players"]["P1"]["connected"] is True
    assert state["players"]["P2"]["connected"] is False


@aio
async def test_resync_builders(world: World) -> None:
    game = await world.running_game()
    room = await world.room(game.code)
    prior = room["seq"]

    emits = game_service.resync_player(room, "P1", "new-sid", next_seq)
    assert [(e.target, e.event) for e in emits] == [
        ("new-sid", "game_started"),
        ("new-sid", "day_opened"),
        ("new-sid", "your_state"),
    ]
    assert [e.data["seq"] for e in emits] == [prior + 1, prior + 2, prior + 3]
    assert emits[2].data["full"] is True

    emits = game_service.resync_host(room, "host-new", next_seq)
    assert [(e.target, e.event) for e in emits] == [("host-new", "host_state")]
    assert emits[0].data["full"] is True
    assert emits[0].data["seq"] == prior + 4

    await world.finish_early(game.code, game_id="pub-1")
    room = await world.room(game.code)
    emits = game_service.resync_player(room, "P1", "new-sid", next_seq)
    assert [e.event for e in emits] == [
        "game_started",
        "your_state",
        "game_finished",
        "game_persisted",
    ]
    assert emits[3].data["game_id"] == "pub-1"
    emits = game_service.resync_host(room, "host-new", next_seq)
    assert [e.event for e in emits] == ["host_state", "game_finished", "game_persisted"]
