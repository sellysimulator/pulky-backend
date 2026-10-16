# ruff: noqa: F811
"""Black-box tests for submit_day (plan section 16, 3.1; criteria 1, 7, 11, 12, 13).

This module also owns the helpers the other section 16 suites import: the
decision builder, the persistence stand-in and the lock watcher.

Persistence seam: no test here calls section 18 with a real session factory
except the ``dbschema`` test of test_full_game. Everywhere else the module
attribute ``app.services.db_service.persist_finished_game`` is replaced by a
spy (the call is made through the module at call time, plan 18 3.1a and
00-conventions 3), so a finishing game never reaches a developer's MySQL.
"""

import asyncio
import copy
import threading
from typing import Any

import pytest

from app.core.records import (
    MSG_PEER_OFF,
    MSG_UNREADABLE,
    msg_order_bounds,
    msg_unaffordable,
    parse_decision,
)
from app.services import game_service
from app.services.state_service import get_state_service
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.play import submit_day
from tests.test_sockets.conftest import Game, World

aio = pytest.mark.asyncio

MSG_ALREADY = "You have already submitted today's decision."
MSG_STALE = "That day is over. Your screen has been updated."
MSG_NOT_IN_ROOM = "You are not in this room."
MSG_NOT_RUNNING = "The game is not running."
MSG_GAME_OVER = "The game is over."
MSG_SPOT_CLOSED = "Spot orders closed: they would arrive after the game ends"
MSG_FUTURES_CLOSED = "Futures closed: deliveries would arrive after the game ends"
HISTORY_KEYS = ("own_history", "public_history", "market_history")

# A seven-day game in which spot closes on day 3 and futures on day 2.
WINDOW_PATCH: dict[str, Any] = {
    "duration_days": 7,
    "futures": {"horizon_days": 6},
    "supplier": {"lead_time_days": 5},
}


# --------------------------------------------------------------------- helpers
def dec(code: str, day: int, **over: Any) -> dict[str, Any]:
    """A submit_day payload: the zero decision with ``over`` applied."""
    data: dict[str, Any] = {
        "room_id": code,
        "day": day,
        "order_spot": 0,
        "order_futures": 0,
        "incoterm": "exw",
        "conserve_lots": [],
        "cooperate": False,
        "peer_buy": None,
        "peer_sell": None,
    }
    data.update(over)
    return data


async def submit(game: Game, alias: str, day: int, **over: Any) -> None:
    await submit_day(game.sids[alias], dec(game.code, day, **over))


async def play_day(
    game: Game, day: int, overrides: dict[str, dict[str, Any]] | None = None
) -> None:
    """Every seated player submits, in alias order; the last one resolves the day."""
    overrides = overrides or {}
    for alias in sorted(game.sids, key=lambda a: int(a[1:])):
        await submit(game, alias, day, **overrides.get(alias, {}))


def collect_values(payload: Any, key: str) -> list[Any]:
    """Every value stored under ``key`` anywhere in nested dicts and lists."""
    found: list[Any] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k == key:
                found.append(v)
            found.extend(collect_values(v, key))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found.extend(collect_values(item, key))
    return found


def room_engine(room: dict[str, Any]) -> Any:
    engine = game_service.load_engine(room)
    assert engine is not None
    return engine


class PersistSpy:
    """Stands in for section 18's persist_finished_game."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.result: str | None = "game-id-1"
        self.snapshots: list[dict[str, Any]] = []
        self.records_at_call: list[int] = []
        self.idents: list[int] = []
        self.lock_depth: list[int] = []
        self.watch: LockWatch | None = None

    def __call__(self, room: dict[str, Any], session_factory: Any = None) -> str | None:
        self.snapshots.append(copy.deepcopy(room))
        self.records_at_call.append(len(self.world.sm.records))
        self.idents.append(threading.get_ident())
        self.lock_depth.append(self.watch.depth if self.watch else 0)
        return self.result


@pytest.fixture
def persist(world: World, monkeypatch: pytest.MonkeyPatch) -> PersistSpy:
    from app.services import db_service

    spy = PersistSpy(world)
    monkeypatch.setattr(db_service, "persist_finished_game", spy)
    return spy


@pytest.fixture(autouse=True)
def _never_touch_a_real_database(persist: PersistSpy) -> None:
    """A game that finishes in this module must not reach a real database."""


class LockWatch:
    """Counts the room locks held and records every emit made while one is."""

    def __init__(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        self.depth = 0
        self.acquired = 0
        self.violations: list[str] = []
        svc = get_state_service()
        real_lock = svc.lock
        watch = self

        class Wrapped:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            async def __aenter__(self) -> Any:
                result = await self.inner.__aenter__()
                watch.depth += 1
                watch.acquired += 1
                return result

            async def __aexit__(self, *exc: object) -> Any:
                watch.depth -= 1
                return await self.inner.__aexit__(*exc)

        monkeypatch.setattr(svc, "lock", lambda code: Wrapped(real_lock(code)))
        for name in ("emit_to_room", "emit_to_sid"):
            original = getattr(world.sm, name)

            async def watched(
                *args: Any, _orig: Any = original, _name: str = name, **kw: Any
            ) -> None:
                if watch.depth:
                    watch.violations.append(f"{_name}:{args[1]}")
                await _orig(*args, **kw)

            monkeypatch.setattr(world.sm, name, watched)


async def refused(
    world: World,
    sid: str,
    data: dict[str, Any],
    code: str,
    message: str | None = None,
    *,
    room_code: str | None = None,
) -> dict[str, Any]:
    """Submit and assert one error to ``sid``, nothing else, the room unchanged."""
    before = await world.room(room_code) if room_code else None
    start = world.mark()
    await submit_day(sid, data)
    got = world.sm.records[start:]
    assert [(r[0], r[1]) for r in got] == [(sid, "error")], got
    payload = got[0][2]
    assert payload["code"] == code
    assert isinstance(payload["message"], str) and payload["message"]
    if message is not None:
        assert payload["message"] == message
    if room_code:
        assert await world.room(room_code) == before
    return payload


# --------------------------------------------------------- acceptance (AC 12)
@aio
async def test_an_accepted_submission_tells_the_room_the_submitter_and_the_host(
    world: World,
) -> None:
    game = await world.running_game()
    start = world.mark()
    await submit(game, "P1", 1, order_spot=10)

    got = world.sm.records[start:]
    assert [(r[0], r[1]) for r in got] == [
        (game.code, "decision_submitted"),
        ("p1", "your_state"),
        ("host-1", "host_state"),
    ]
    submitted, mine, host = (r[2] for r in got)
    assert submitted["day"] == 1
    assert submitted["alias"] == "P1"
    assert submitted["awaiting"] == ["P2"]
    assert mine["has_submitted"] is True
    assert mine["full"] is False
    assert mine["alias"] == "P1"
    assert host["full"] is False
    assert host["players"]["P1"]["has_submitted"] is True
    assert host["players"]["P2"]["has_submitted"] is False
    assert host["awaiting"] == ["P2"]
    # consecutive seq values, one per logical event (spec 12, plan 3.9)
    assert mine["seq"] == submitted["seq"] + 1
    assert host["seq"] == mine["seq"] + 1
    room = await world.room(game.code)
    assert room["seq"] == host["seq"]
    engine = room_engine(room)
    assert engine.has_submitted("P1") and not engine.has_submitted("P2")
    assert engine.awaiting() == ["P2"]
    assert room["state"] == "running"


@aio
async def test_a_daily_state_after_a_submission_carries_no_history(
    world: World,
) -> None:
    game = await world.running_game()
    await play_day(game, 1)
    start = world.mark()
    await submit(game, "P2", 2)
    for record in world.sm.records[start:]:
        if record[1] in ("your_state", "host_state"):
            assert record[2]["full"] is False
            for key in HISTORY_KEYS:
                assert key not in record[2]
                assert key not in str(record[2].get("players", ""))


# ------------------------------------------------------ the checks, in order
@aio
async def test_a_sid_that_is_not_seated_gets_not_in_room(world: World) -> None:
    game = await world.running_game()
    await world.connect("ghost", 77)
    payload = await refused(
        world,
        "ghost",
        dec(game.code, 1, order_spot=3),
        "NOT_IN_ROOM",
        MSG_NOT_IN_ROOM,
        room_code=game.code,
    )
    assert "field" not in payload or payload["field"] is None
    # the host holds no seat (spec 11.3) and cannot submit a decision
    await refused(
        world,
        game.host_sid,
        dec(game.code, 1),
        "NOT_IN_ROOM",
        MSG_NOT_IN_ROOM,
        room_code=game.code,
    )
    # a seated player naming a room that does not exist
    await refused(world, "p1", dec("QQQQQQ", 1), "NOT_IN_ROOM", MSG_NOT_IN_ROOM)


@aio
async def test_not_in_room_wins_over_the_room_state(world: World) -> None:
    game = await world.running_game()
    await world.finish_early(game.code)
    await world.connect("ghost", 77)
    await refused(
        world, "ghost", {"room_id": game.code}, "NOT_IN_ROOM", MSG_NOT_IN_ROOM
    )


@aio
async def test_a_finished_room_answers_game_finished_before_reading_the_decision(
    world: World,
) -> None:
    game = await world.running_game()
    await world.finish_early(game.code)
    # even an unreadable decision gets the state error, not INVALID_DECISION
    await refused(
        world,
        "p1",
        {"room_id": game.code, "day": "x"},
        "GAME_FINISHED",
        MSG_GAME_OVER,
        room_code=game.code,
    )


@aio
async def test_a_lobby_room_answers_game_not_running_before_reading_the_decision(
    world: World,
) -> None:
    code = await world.create_room()
    await world.claim_host(code)
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    await refused(
        world,
        "p1",
        {"room_id": code, "day": "x"},
        "GAME_NOT_RUNNING",
        MSG_NOT_RUNNING,
        room_code=code,
    )
    await refused(
        world, "p1", dec(code, 1), "GAME_NOT_RUNNING", MSG_NOT_RUNNING, room_code=code
    )


@aio
async def test_the_decision_is_read_before_the_day_is_compared(world: World) -> None:
    game = await world.running_game()
    bad = dec(game.code, 99)
    del bad["peer_sell"]
    await refused(
        world,
        "p1",
        bad,
        "INVALID_DECISION",
        MSG_UNREADABLE,
        room_code=game.code,
    )
    # a readable decision for the wrong day is stale (checked after reading)
    start = world.mark()
    await submit_day("p1", dec(game.code, 99))
    assert [r[1] for r in world.sm.records[start:]][0] == "error"
    assert world.sm.records[start][2]["code"] == "STALE_DAY"


@aio
async def test_an_unreadable_decision_is_refused_before_a_second_submission_check(
    world: World,
) -> None:
    game = await world.running_game()
    await submit(game, "P1", 1)
    bad = dec(game.code, 1)
    bad["order_spot"] = "ten"
    await refused(
        world, "p1", bad, "INVALID_DECISION", MSG_UNREADABLE, room_code=game.code
    )


# ------------------------------------------- one submission per day (FM 1, D5)
@aio
async def test_a_second_submission_is_refused_and_keeps_the_first_decision(
    world: World,
) -> None:
    game = await world.running_game()
    await submit(game, "P1", 1, order_spot=3)
    await refused(
        world,
        "p1",
        dec(game.code, 1, order_spot=9, order_futures=2),
        "ALREADY_SUBMITTED",
        MSG_ALREADY,
        room_code=game.code,
    )
    start = world.mark()
    await submit(game, "P2", 1)
    record = world.sent("p1", "your_day_closed", since=start)[0]["record"]
    assert record["order_spot"] == 3  # the first decision stood
    assert record["order_futures"] == 0
    room = room_engine(await world.room(game.code))
    assert len(room.records("P1")) == 1


@aio
async def test_a_repeated_identical_submission_changes_nothing(world: World) -> None:
    game = await world.running_game()
    await submit(game, "P1", 1, order_spot=10)
    before = await world.room(game.code)
    for _ in range(3):
        await refused(
            world, "p1", dec(game.code, 1, order_spot=10), "ALREADY_SUBMITTED"
        )
    assert await world.room(game.code) == before


# ---------------------------------------------------- stale day (FM 7, AC 2)
@aio
async def test_a_stale_day_is_followed_by_a_full_your_state_to_the_same_sid(
    world: World,
) -> None:
    game = await world.running_game()
    await play_day(game, 1, {"P1": {"order_spot": 5}})
    before = await world.room(game.code)
    start = world.mark()
    await submit(game, "P1", 1, order_spot=7)

    got = world.sm.records[start:]
    assert [(r[0], r[1]) for r in got] == [("p1", "error"), ("p1", "your_state")]
    assert got[0][2]["code"] == "STALE_DAY"
    assert got[0][2]["message"] == MSG_STALE
    state = got[1][2]
    assert state["full"] is True
    assert state["day"] == 2
    assert state["has_submitted"] is False
    assert len(state["own_history"]) == 1
    assert len(state["public_history"]) == 1
    after = await world.room(game.code)
    assert room_engine(after).to_payload() == room_engine(before).to_payload()
    assert room_engine(after).awaiting() == ["P1", "P2"]


@aio
async def test_a_day_from_the_future_is_stale_too(world: World) -> None:
    game = await world.running_game()
    start = world.mark()
    await submit(game, "P2", 5)
    got = world.sm.records[start:]
    assert [(r[0], r[1]) for r in got] == [("p2", "error"), ("p2", "your_state")]
    assert got[0][2]["code"] == "STALE_DAY"
    assert got[1][2]["full"] is True
    assert not room_engine(await world.room(game.code)).has_submitted("P2")


# ------------------------------------------------ INVALID_DECISION (AC 1, 13)
@aio
async def test_the_peer_market_off_refuses_buy_limits_and_sell_offers(
    world: World,
) -> None:
    game = await world.running_game({"peer_market": {"enabled": False}})
    buy = {"max_price": 16, "min_quality": 90}
    sell = {"price": 15.5, "max_quality": 50, "max_units": 4}
    await refused(
        world,
        "p1",
        dec(game.code, 1, peer_buy=buy),
        "INVALID_DECISION",
        MSG_PEER_OFF,
        room_code=game.code,
    )
    await refused(
        world,
        "p1",
        dec(game.code, 1, peer_sell=sell),
        "INVALID_DECISION",
        MSG_PEER_OFF,
        room_code=game.code,
    )
    # null for both is how a player says "none", and is accepted
    start = world.mark()
    await submit(game, "P1", 1)
    assert world.sm.records[start][1] == "decision_submitted"


@aio
@pytest.mark.parametrize("missing", ["peer_buy", "peer_sell"])
@pytest.mark.parametrize("peer_on", [True, False])
async def test_a_submission_missing_a_peer_key_is_unreadable(
    world: World, missing: str, peer_on: bool
) -> None:
    game = await world.running_game({"peer_market": {"enabled": peer_on}})
    data = dec(game.code, 1)
    del data[missing]
    await refused(
        world,
        "p1",
        data,
        "INVALID_DECISION",
        MSG_UNREADABLE,
        room_code=game.code,
    )


@aio
async def test_an_order_out_of_bounds_is_refused_with_the_bounds_message(
    world: World,
) -> None:
    game = await world.running_game()
    payload = await refused(
        world,
        "p1",
        dec(game.code, 1, order_spot=10_000),
        "INVALID_DECISION",
        msg_order_bounds(),
        room_code=game.code,
    )
    assert payload["code"] == "INVALID_DECISION"


@aio
async def test_a_decision_the_player_cannot_afford_is_refused_by_the_engine(
    world: World,
) -> None:
    game = await world.running_game({"starting_cash": 0})
    data = dec(game.code, 1, order_spot=50)
    engine = room_engine(await world.room(game.code))
    outlay = engine.outlay("P1", parse_decision(data))
    cash = engine.player_view("P1")["cash"]
    assert outlay > cash
    await refused(
        world,
        "p1",
        data,
        "INVALID_DECISION",
        msg_unaffordable(outlay, cash),
        room_code=game.code,
    )


# ----------------------------------------------- ordering windows (AC 11)
@aio
async def test_closed_order_windows_are_refused_with_the_window_copy(
    world: World,
) -> None:
    game = await world.running_game(WINDOW_PATCH)
    # day 1: both windows are open, so a refusal below is not a blanket one
    start = world.mark()
    await submit(game, "P1", 1, order_spot=5, order_futures=5)
    assert world.sm.records[start][1] == "decision_submitted"
    await submit(game, "P2", 1)

    # day 2: futures closed (2 + 6 > 7), spot still open (2 + 5 <= 7)
    await refused(
        world,
        "p1",
        dec(game.code, 2, order_futures=1),
        "INVALID_DECISION",
        MSG_FUTURES_CLOSED,
        room_code=game.code,
    )
    start = world.mark()
    await submit(game, "P1", 2, order_spot=1)
    assert world.sm.records[start][1] == "decision_submitted"
    await submit(game, "P2", 2)

    # day 3: spot closed as well
    await refused(
        world,
        "p1",
        dec(game.code, 3, order_spot=1),
        "INVALID_DECISION",
        MSG_SPOT_CLOSED,
        room_code=game.code,
    )
    await refused(
        world,
        "p2",
        dec(game.code, 3, order_futures=1),
        "INVALID_DECISION",
        MSG_FUTURES_CLOSED,
        room_code=game.code,
    )
    engine = room_engine(await world.room(game.code))
    assert engine.awaiting() == ["P1", "P2"]


# ----------------------------------------- concurrency (AC 7, FM 5)
async def force_interleaving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every room read yield after reading, so an unlocked handler would
    work on a stale copy while a second handler reads the same one."""
    svc = get_state_service()
    real_get = svc.get_room

    async def slow_get(code: str) -> Any:
        room = await real_get(code)
        await asyncio.sleep(0)
        return room

    monkeypatch.setattr(svc, "get_room", slow_get)


@aio
async def test_both_players_submitting_at_once_resolve_the_day_exactly_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = await world.running_game()
    await force_interleaving(monkeypatch)
    start = world.mark()
    await asyncio.gather(
        submit(game, "P1", 1, order_spot=4), submit(game, "P2", 1, order_spot=6)
    )
    got = world.sm.records[start:]
    assert len([r for r in got if r[1] == "day_closed"]) == 1
    assert len([r for r in got if r[1] == "day_opened"]) == 1
    assert len([r for r in got if r[1] == "your_day_closed"]) == 2
    assert not [r for r in got if r[1] == "error"]
    room = await world.room(game.code)
    engine = room_engine(room)
    assert engine.day == 2
    assert len(engine.records("P1")) == 1 and len(engine.records("P2")) == 1
    assert engine.records("P1")[0].order_spot == 4
    assert engine.records("P2")[0].order_spot == 6


@aio
async def test_two_concurrent_last_submissions_produce_one_day_closed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = await world.running_game(players=3)
    await submit(game, "P1", 1)
    await force_interleaving(monkeypatch)
    start = world.mark()
    await asyncio.gather(submit(game, "P2", 1), submit(game, "P3", 1))
    got = world.sm.records[start:]
    assert len([r for r in got if r[1] == "day_closed"]) == 1
    assert len([r for r in got if r[1] == "day_opened"]) == 1
    submitted = [r[2] for r in got if r[1] == "decision_submitted"]
    assert sorted(len(s["awaiting"]) for s in submitted) == [0, 1]
    engine = room_engine(await world.room(game.code))
    assert engine.day == 2
    assert all(len(engine.records(a)) == 1 for a in ("P1", "P2", "P3"))


@aio
async def test_a_submission_after_a_reconnect_uses_the_new_sid(world: World) -> None:
    game = await world.running_game()
    await disconnect("p2")
    await world.seat(game.code, "p2b", 2, token=game.tokens["P2"])
    await submit_day("p2b", dec(game.code, 1))
    await refused(world, "p2", dec(game.code, 1), "NOT_IN_ROOM", MSG_NOT_IN_ROOM)
    assert room_engine(await world.room(game.code)).has_submitted("P2")
