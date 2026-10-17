# ruff: noqa: F811
"""Black-box tests for end_game_early (plan 16, 3.3; criterion 5, failure mode 6)."""

import re
from typing import Any

import pytest

from app.sockets.handlers.host import end_game_early
from tests.conftest import contains_key
from tests.test_sockets.conftest import Game, World, norm
from tests.test_sockets.test_submit import (  # noqa: F401
    HISTORY_KEYS,
    MSG_NOT_RUNNING,
    PersistSpy,
    _never_touch_a_real_database,
    persist,
    play_day,
    room_engine,
    submit,
)

aio = pytest.mark.asyncio
MSG_ONLY_HOST = "Only the host can do that."
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def end_data(game: Game, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"room_id": game.code, "host_secret": game.secret}
    data.update(over)
    return data


async def refused_end(
    world: World, sid: str, data: dict[str, Any], code: str, message: str
) -> None:
    before = await world.room(data["room_id"])
    start = world.mark()
    await end_game_early(sid, data)
    got = world.sm.records[start:]
    assert [(r[0], r[1]) for r in got] == [(sid, "error")], got
    assert got[0][2]["code"] == code
    assert got[0][2]["message"] == message
    assert await world.room(data["room_id"]) == before


# --------------------------------------------------------------- the success
@aio
async def test_the_host_ends_a_running_game_and_everyone_is_told(
    world: World, persist: PersistSpy
) -> None:
    game = await world.running_game(players=3)
    await play_day(game, 1)
    await play_day(game, 2)
    await submit(game, "P1", 3, order_spot=7)  # a pending decision on the open day
    start = world.mark()
    await end_game_early("host-1", end_data(game))
    got = world.sm.records[start:]

    assert [r[1] for r in got] == [
        "your_state",
        "your_state",
        "your_state",
        "host_state",
        "game_finished",
        "game_persisted",
    ]
    assert sorted(r[0] for r in got[:3]) == ["p1", "p2", "p3"]
    assert got[3][0] == "host-1"
    assert got[4][0] == got[5][0] == game.code
    for record in got[:4]:
        assert record[2]["full"] is False  # the daily form
        for key in HISTORY_KEYS:
            assert not contains_key(record[2], key)
    states_seq = {r[2]["seq"] for r in got[:3]}
    assert len(states_seq) == 1  # one shared seq
    assert got[3][2]["seq"] == states_seq.pop() + 1
    assert got[4][2]["seq"] == got[3][2]["seq"] + 1
    assert got[5][2] == {"seq": got[4][2]["seq"] + 1, "game_id": persist.result}

    room = await world.room(game.code)
    engine = room_engine(room)
    finished = got[4][2]
    assert finished["game_id"] == room["game_id"]
    body = {k: v for k, v in finished.items() if k not in ("seq", "game_id")}
    assert body == norm(engine.results())
    assert finished["ended_early"] is True
    assert finished["days_played"] == 2  # the open day was discarded
    assert sorted(finished["stats"]["players"]) == ["P1", "P2", "P3"]
    for alias, settlement in engine.settlements().items():
        assert norm(finished["stats"]["players"][alias]["final_settlement"]) == norm(
            settlement.to_payload()
        )
    assert sorted(e["alias"] for e in finished["leaderboard"]) == ["P1", "P2", "P3"]

    assert engine.ended_early is True
    assert all(len(engine.records(a)) == 2 for a in ("P1", "P2", "P3"))
    assert room["state"] == "finished"
    assert STAMP_RE.match(room["finished_at"])
    assert room["persisted"] is True
    assert room["game_id"] == persist.result
    assert len(persist.snapshots) == 1
    assert persist.snapshots[0]["state"] == "finished"
    # persistence ran after the game_finished emit
    sent = [r[1] for r in world.sm.records[: persist.records_at_call[0]]]
    assert "game_finished" in sent and "game_persisted" not in sent


@aio
async def test_ending_early_survives_a_failed_persistence(
    world: World, persist: PersistSpy
) -> None:
    persist.result = None
    game = await world.running_game()
    await play_day(game, 1)
    start = world.mark()
    await end_game_early("host-1", end_data(game))
    events = [r[1] for r in world.sm.records[start:]]
    assert events[-1] == "game_finished"
    assert "game_persisted" not in events and "error" not in events
    room = await world.room(game.code)
    assert room["state"] == "finished" and room["persisted"] is False


# ------------------------------------------------------------ wrong secret
@aio
async def test_a_wrong_or_missing_secret_is_refused_and_changes_nothing(
    world: World,
) -> None:
    game = await world.running_game()
    await play_day(game, 1)
    for data in (
        end_data(game, host_secret="not-the-secret"),
        end_data(game, host_secret=""),
        {"room_id": game.code},
    ):
        await refused_end(world, "host-1", data, "INVALID_HOST_SECRET", MSG_ONLY_HOST)
        await refused_end(world, "p1", data, "INVALID_HOST_SECRET", MSG_ONLY_HOST)
    room = await world.room(game.code)
    assert room["state"] == "running"
    assert room_engine(room).ended_early is False


@aio
async def test_the_secret_is_checked_before_the_room_state(world: World) -> None:
    code = await world.create_room()
    await world.claim_host(code)
    game = Game(code=code, secret="wrong", host_sid="host-1")
    await refused_end(
        world, "host-1", end_data(game), "INVALID_HOST_SECRET", MSG_ONLY_HOST
    )


# --------------------------------------------------- outside running (FM 6)
@aio
async def test_in_the_lobby_ending_early_is_refused(world: World) -> None:
    code = await world.create_room()
    secret = str((await world.room(code))["host_secret"])
    await world.claim_host(code)
    await world.seat(code, "p1", 1)
    game = Game(code=code, secret=secret, host_sid="host-1")
    await refused_end(
        world, "host-1", end_data(game), "GAME_NOT_RUNNING", MSG_NOT_RUNNING
    )
    room = await world.room(code)
    assert room["state"] == "lobby"
    assert room["finished_at"] is None and room["engine"] is None


@aio
async def test_after_the_end_ending_early_again_is_refused_and_changes_nothing(
    world: World, persist: PersistSpy
) -> None:
    game = await world.running_game()
    await play_day(game, 1)
    await end_game_early("host-1", end_data(game))
    first = await world.room(game.code)
    assert first["state"] == "finished"
    calls = len(persist.snapshots)

    await refused_end(
        world, "host-1", end_data(game), "GAME_NOT_RUNNING", MSG_NOT_RUNNING
    )
    second = await world.room(game.code)
    assert second["finished_at"] == first["finished_at"]
    assert second["game_id"] == first["game_id"]
    assert len(persist.snapshots) == calls  # no second persistence either


@aio
async def test_after_a_natural_end_ending_early_is_refused(
    world: World, persist: PersistSpy
) -> None:
    game = await world.running_game(
        {"duration_days": 7, "futures": {"horizon_days": 3}}
    )
    for day in range(1, 8):
        await play_day(game, day)
    room = await world.room(game.code)
    assert room["state"] == "finished"
    assert room_engine(room).ended_early is False
    await refused_end(
        world, "host-1", end_data(game), "GAME_NOT_RUNNING", MSG_NOT_RUNNING
    )
    after = await world.room(game.code)
    assert room_engine(after).ended_early is False
