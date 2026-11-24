"""The play-loop suites' shared helpers: the decision builder, the persistence
stand-in and the lock watcher.

Persistence seam: no test persists with a real session factory except the
``dbschema`` test of test_full_game. Everywhere else the module attribute
``app.services.db_service.persist_finished_game`` is replaced by a spy (the call
is made through the module at call time), so a finishing game never reaches a
developer's database.
"""

import copy
import threading
from typing import Any

import pytest

from app.services import game_service
from app.services.state_service import get_state_service
from app.sockets.handlers.play import submit_day
from tests.fixtures.sockets import Game, World

MSG_NOT_IN_ROOM = "You are not in this room."
MSG_NOT_RUNNING = "The game is not running."
HISTORY_KEYS = ("own_history", "public_history", "market_history")


def dec(code: str, day: int, **over: Any) -> dict[str, Any]:
    """A submit_day payload: the zero decision with ``over`` applied."""
    data: dict[str, Any] = {
        "room_id": code,
        "day": day,
        "order_spot": 0,
        "order_futures": 0,
        "incoterm_tier": "exw",
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
    """Stands in for ``persist_finished_game``."""

    def __init__(self, world: World) -> None:
        self.world = world
        # None makes the call fail; otherwise, like the real function, the call
        # returns the id the room was given when it finished.
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
        if self.result is None:
            return None
        self.result = str(room["game_id"])
        return self.result


@pytest.fixture
def persist(world: World, monkeypatch: pytest.MonkeyPatch) -> PersistSpy:
    """A finishing game writes to the spy, never to a database. A module whose games
    finish asks for it with ``pytest.mark.usefixtures("persist")``."""
    from app.services import db_service

    spy = PersistSpy(world)
    monkeypatch.setattr(db_service, "persist_finished_game", spy)
    return spy


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
