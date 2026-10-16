# ruff: noqa: F811
"""Black-box tests for the resolution emits and finish (plan section 16, 3.1, 3.4).

Criteria 2, 3, 4 and 10; failure modes 3, 4 and 8. Persistence is the spy of
test_submit (see its docstring for the seam).
"""

import logging
import re
import threading
from typing import Any

import pytest

from app.services import game_service, play_service
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.play import submit_day
from tests.conftest import contains_key
from tests.test_sockets.conftest import Game, World, norm
from tests.test_sockets.test_submit import (  # noqa: F401
    HISTORY_KEYS,
    LockWatch,
    PersistSpy,
    _never_touch_a_real_database,
    dec,
    persist,
    play_day,
    room_engine,
    submit,
)

aio = pytest.mark.asyncio
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHORT = {"duration_days": 7, "futures": {"horizon_days": 3}}
LONG = {"duration_days": 12, "futures": {"horizon_days": 3}}


def names(records: list[tuple[Any, ...]]) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in records]


async def last_submit(
    world: World,
    game: Game,
    day: int,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[Any, ...]]:
    """Everyone but the last alias submits; return what the last submit emitted."""
    overrides = overrides or {}
    aliases = sorted(game.sids, key=lambda a: int(a[1:]))
    for alias in aliases[:-1]:
        await submit(game, alias, day, **overrides.get(alias, {}))
    start = world.mark()
    await submit(game, aliases[-1], day, **overrides.get(aliases[-1], {}))
    return world.sm.records[start:]


# ------------------------------------------------------ order and seq (AC 2)
@aio
async def test_a_resolution_emits_in_the_pinned_order(world: World) -> None:
    game = await world.running_game()
    await submit(game, "P1", 1, order_spot=10)
    start = world.mark()
    await submit(game, "P2", 1)
    got = world.sm.records[start:]

    assert [r[1] for r in got] == [
        "decision_submitted",
        "day_closed",
        "your_day_closed",
        "your_day_closed",
        "your_state",
        "your_state",
        "host_state",
        "day_opened",
    ]
    code = game.code
    assert got[0][0] == got[1][0] == got[7][0] == code
    assert {got[2][0], got[3][0]} == {"p1", "p2"}
    assert {got[4][0], got[5][0]} == {"p1", "p2"}
    assert got[6][0] == "host-1"
    assert got[0][2]["awaiting"] == []


@aio
async def test_resolution_seq_values_increase_and_are_shared_per_event(
    world: World,
) -> None:
    game = await world.running_game()
    await submit(game, "P1", 1)
    start = world.mark()
    await submit(game, "P2", 1)
    got = [r[2] for r in world.sm.records[start:]]
    submitted, closed, c1, c2, s1, s2, host, opened = got

    assert c1["seq"] == c2["seq"]  # one shared seq for the per-player copies
    assert s1["seq"] == s2["seq"]
    chain = [
        submitted["seq"],
        closed["seq"],
        c1["seq"],
        s1["seq"],
        host["seq"],
        opened["seq"],
    ]
    assert chain == list(range(chain[0], chain[0] + 6))  # strictly consecutive
    assert (await world.room(game.code))["seq"] == opened["seq"]


# --------------------------------------------- the payloads (AC 2, AC 3)
@aio
async def test_resolution_payloads_are_the_engines_own(world: World) -> None:
    game = await world.running_game()
    got = await last_submit(world, game, 1, {"P1": {"order_spot": 10}})
    room = await world.room(game.code)
    engine = room_engine(room)

    closed = next(r[2] for r in got if r[1] == "day_closed")
    assert closed["day"] == 1
    assert norm(closed["public"]) == norm(engine.public_results(1))

    opened = next(r[2] for r in got if r[1] == "day_opened")
    assert opened["brief"]["day"] == 2
    assert norm(opened["brief"]) == norm(engine.brief())

    for record in got:
        if record[1] == "your_state":
            alias = "P1" if record[0] == "p1" else "P2"
            body = {k: v for k, v in record[2].items() if k not in ("seq", "full")}
            assert record[2]["full"] is False
            assert norm(body) == norm(engine.player_view(alias, full=False))
            assert body["has_submitted"] is False  # a fresh day: the form unlocks
            assert body["day"] == 2
        if record[1] == "host_state":
            assert record[0] == "host-1"
            expected = game_service.host_state(room, 0, False)
            body = {k: v for k, v in record[2].items() if k != "seq"}
            assert record[2]["full"] is False
            assert norm(body) == norm({k: v for k, v in expected.items() if k != "seq"})


@aio
async def test_each_player_gets_their_own_record_on_their_own_sid_only(
    world: World,
) -> None:
    game = await world.running_game()
    got = await last_submit(world, game, 1, {"P1": {"order_spot": 10}})
    room_engine_ = room_engine(await world.room(game.code))

    closed = [r for r in got if r[1] == "your_day_closed"]
    assert sorted(r[0] for r in closed) == ["p1", "p2"]  # sids, never the room
    by_sid = {r[0]: r[2] for r in closed}
    for sid, alias in (("p1", "P1"), ("p2", "P2")):
        assert by_sid[sid]["day"] == 1
        assert norm(by_sid[sid]["record"]) == norm(
            room_engine_.records(alias)[-1].to_payload()
        )
    assert by_sid["p1"]["record"]["order_spot"] == 10
    assert by_sid["p2"]["record"]["order_spot"] == 0
    assert by_sid["p1"]["record"] != by_sid["p2"]["record"]
    assert not [r for r in got if r[1] == "your_day_closed" and r[0] == game.code]
    assert not [r for r in got if r[1] == "your_day_closed" and r[0] == "host-1"]


@aio
async def test_your_day_closed_goes_to_the_players_current_sid(world: World) -> None:
    game = await world.running_game()
    await submit(game, "P1", 1)
    await disconnect("p2")
    await world.seat(game.code, "p2b", 2, token=game.tokens["P2"])
    start = world.mark()
    await submit_day("p2b", dec(game.code, 1))
    got = world.sm.records[start:]
    assert sorted(r[0] for r in got if r[1] == "your_day_closed") == ["p1", "p2b"]
    assert sorted(r[0] for r in got if r[1] == "your_state") == ["p1", "p2b"]
    assert not [r for r in got if r[0] == "p2"]


# ----------------------------------------- daily form, no history (FM 8)
@aio
async def test_no_resolution_state_ever_resends_the_history(world: World) -> None:
    game = await world.running_game(LONG)
    for day in range(1, 11):
        start = world.mark()
        await play_day(game, day)
        states = [
            r for r in world.sm.records[start:] if r[1] in ("your_state", "host_state")
        ]
        resolved = [r for r in states if r[2]["full"] is False]
        assert len(resolved) == 3 + 2  # P1/P2 after P1's submit, then P2's resolution
        for record in states:
            assert record[2]["full"] is False
            for key in HISTORY_KEYS:
                assert not contains_key(record[2], key), (day, record[1], key)
    # the day-10 resolution still carries today's record for the host
    host = world.last("host-1", "host_state")
    assert host["day"] == 11
    assert host["players"]["P1"]["last_record"]["day"] == 10
    assert host["last_market_day"]["day"] == 10


# ---------------------------------------------------- emit after lock (FM 3)
@aio
async def test_nothing_is_emitted_while_the_room_lock_is_held(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = await world.running_game()
    watch = LockWatch(world, monkeypatch)
    await submit(game, "P1", 1)
    await submit(game, "P2", 1)  # resolves
    await submit(game, "P1", 1)  # stale: error + your_state
    assert watch.acquired >= 3  # the lock was really used
    assert watch.depth == 0
    assert watch.violations == []
    assert world.sm.emits("day_closed")


@aio
async def test_persistence_runs_after_the_emits_outside_the_lock_and_off_the_loop(
    world: World, persist: PersistSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = await world.running_game(SHORT)
    watch = LockWatch(world, monkeypatch)
    persist.watch = watch
    for day in range(1, 8):
        await play_day(game, day)
    assert watch.violations == []
    assert len(persist.snapshots) == 1
    assert persist.lock_depth == [0]
    assert persist.idents[0] != threading.get_ident()  # a worker thread (to_thread)
    snapshot = persist.snapshots[0]
    assert snapshot["state"] == "finished"
    assert snapshot["room_code"] == game.code
    # the finishing emits had all been sent when persistence began
    sent = world.sm.records[: persist.records_at_call[0]]
    assert "game_finished" in [r[1] for r in sent]
    assert "game_persisted" not in [r[1] for r in sent]


# ------------------------------------------------------ finishing (AC 4)
@aio
async def test_a_finishing_day_emits_game_finished_then_game_persisted(
    world: World, persist: PersistSpy, caplog: pytest.LogCaptureFixture
) -> None:
    game = await world.running_game(SHORT)
    for day in range(1, 7):
        await play_day(game, day)
    with caplog.at_level(logging.INFO):
        got = await last_submit(world, game, 7)

    assert [r[1] for r in got] == [
        "decision_submitted",
        "day_closed",
        "your_day_closed",
        "your_day_closed",
        "your_state",
        "your_state",
        "host_state",
        "game_finished",
        "game_persisted",
    ]
    assert "day_opened" not in [r[1] for r in got]
    assert got[7][0] == got[8][0] == game.code
    for record in got:
        if record[1] in ("your_state", "host_state"):
            assert record[2]["full"] is False
            for key in HISTORY_KEYS:
                assert not contains_key(record[2], key)

    room = await world.room(game.code)
    engine = room_engine(room)
    finished = got[7][2]
    assert {k: v for k, v in finished.items() if k != "seq"} == norm(engine.results())
    assert finished["ended_early"] is False
    assert finished["days_played"] == 7
    persisted = got[8][2]
    assert persisted["game_id"] == persist.result
    assert persisted["seq"] == finished["seq"] + 1
    assert finished["seq"] == got[6][2]["seq"] + 1  # after host_state
    assert room["state"] == "finished"
    assert STAMP_RE.match(room["finished_at"])
    assert room["persisted"] is True
    assert room["game_id"] == persist.result
    assert room["seq"] == persisted["seq"]
    assert len(persist.snapshots) == 1
    assert persist.snapshots[0]["state"] == "finished"

    messages = [r.getMessage() for r in caplog.records]
    code = game.code
    assert any(
        re.fullmatch(rf"room {code} day 7 resolved in \d+ ms", m) for m in messages
    )
    assert f"room {code} finished" in messages


@aio
async def test_every_resolution_logs_its_day_and_duration(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    game = await world.running_game()
    with caplog.at_level(logging.INFO):
        await play_day(game, 1)
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        re.fullmatch(rf"room {game.code} day 1 resolved in \d+ ms", m) for m in messages
    )
    assert f"room {game.code} finished" not in messages


# -------------------------------------------- persistence failure (AC 10)
@aio
async def test_a_failed_persistence_leaves_the_game_finished_and_silent(
    world: World, persist: PersistSpy
) -> None:
    persist.result = None
    game = await world.running_game(SHORT)
    for day in range(1, 7):
        await play_day(game, day)
    got = await last_submit(world, game, 7)  # raises nothing to the caller

    events = [r[1] for r in got]
    assert events[-1] == "game_finished"
    assert "game_persisted" not in events
    assert "error" not in [r[1] for r in world.sm.records]
    assert len(persist.snapshots) == 1
    room = await world.room(game.code)
    assert room["state"] == "finished"
    assert room["persisted"] is False
    assert room["game_id"] is None


# ------------------------------------------------------------ finish() (3.4)
@aio
async def test_finish_records_the_game_id_then_announces_it(
    world: World, persist: PersistSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = await world.running_game()
    await world.finish_early(game.code)
    before = (await world.room(game.code))["seq"]
    watch = LockWatch(world, monkeypatch)
    persist.watch = watch
    start = world.mark()
    await play_service.finish(game.code)

    assert persist.lock_depth == [0]
    assert persist.snapshots[0]["state"] == "finished"
    assert persist.snapshots[0]["persisted"] is False
    assert watch.violations == []
    got = world.sm.records[start:]
    assert names(got) == [(game.code, "game_persisted")]
    assert got[0][2] == {"seq": before + 1, "game_id": persist.result}
    room = await world.room(game.code)
    assert room["persisted"] is True
    assert room["game_id"] == persist.result


@aio
async def test_finish_emits_nothing_when_persistence_returns_none(
    world: World, persist: PersistSpy
) -> None:
    persist.result = None
    game = await world.running_game()
    await world.finish_early(game.code)
    before = await world.room(game.code)
    start = world.mark()
    await play_service.finish(game.code)
    assert world.sm.records[start:] == []
    room = await world.room(game.code)
    assert room["persisted"] is False
    assert room["game_id"] is None
    assert room["seq"] == before["seq"]
