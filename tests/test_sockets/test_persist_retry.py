"""A failed end-of-game write is tried again (spec 13.5): in the background after a
failure, and when someone rejoins or resyncs a finished game that is not stored."""

import asyncio
from typing import Any

import pytest

from app.services import db_service, play_service
from app.sockets.handlers.lobby import join, join_waiting
from app.sockets.handlers.play import request_state
from tests.test_sockets.conftest import World

aio = pytest.mark.asyncio


class FlakyStore:
    """Stands in for ``persist_finished_game``: fails ``failures`` times, then stores
    the game under the id it was given when it finished, like the real function."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.stored: list[str] = []

    def __call__(self, room: dict[str, Any], session_factory: Any = None) -> str | None:
        self.calls += 1
        if self.calls <= self.failures:
            return None
        game_id = str(room["game_id"])
        if game_id not in self.stored:
            self.stored.append(game_id)
        return game_id


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FlakyStore:
    flaky = FlakyStore(failures=1)
    monkeypatch.setattr(db_service, "persist_finished_game", flaky)
    return flaky


async def settle() -> None:
    """Let the background retries run (their delays are 0 in these tests)."""
    for _ in range(200):
        await asyncio.sleep(0)


def persisted_ids(world: World, code: str, since: int = 0) -> list[str]:
    return [p["game_id"] for p in world.sent(code, "game_persisted", since)]


@aio
async def test_a_failed_write_is_retried_stored_once_and_announced(
    world: World, store: FlakyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails against a finish() that gives up after one failed write."""
    monkeypatch.setattr(play_service, "PERSIST_RETRY_DELAYS", (0.0, 0.0, 0.0))
    game = await world.running_game()
    await world.finish_early(game.code)
    game_id = (await world.room(game.code))["game_id"]
    start = world.mark()

    await play_service.finish(game.code)
    assert store.calls == 1 and store.stored == []
    await settle()

    assert store.calls == 2
    assert store.stored == [game_id]
    assert persisted_ids(world, game.code, start) == [game_id]
    room = await world.room(game.code)
    assert room["persisted"] is True
    # stored: neither another finish() nor a rejoin writes it again
    await play_service.finish(game.code)
    await world.connect("p1b", 1)
    await join("p1b", {"room_id": game.code, "session_token": game.tokens["P1"]})
    await settle()
    assert store.calls == 2
    assert persisted_ids(world, game.code, start) == [game_id]


@aio
async def test_retries_stop_after_three_and_a_rejoin_tries_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = FlakyStore(failures=4)
    monkeypatch.setattr(db_service, "persist_finished_game", flaky)
    monkeypatch.setattr(play_service, "PERSIST_RETRY_DELAYS", (0.0, 0.0, 0.0))
    game = await world.running_game()
    await world.finish_early(game.code)
    game_id = (await world.room(game.code))["game_id"]

    await play_service.finish(game.code)
    await settle()
    assert flaky.calls == 4  # the first write and three retries
    assert (await world.room(game.code))["persisted"] is False

    await world.connect("p1b", 1)
    start = world.mark()
    await join("p1b", {"room_id": game.code, "session_token": game.tokens["P1"]})
    assert flaky.calls == 5
    assert flaky.stored == [game_id]
    seen = world.reaching_events("p1b", game.code, start)
    assert seen.index("game_finished") < seen.index("game_persisted")


@aio
async def test_a_host_rejoin_or_a_resync_of_an_unstored_game_stores_it(
    world: World, store: FlakyStore
) -> None:
    """Retries are off here (the socket suites' default): only the rejoin retries."""
    game = await world.running_game()
    await world.finish_early(game.code)
    game_id = (await world.room(game.code))["game_id"]
    await play_service.finish(game.code)
    assert store.calls == 1 and store.stored == []

    await world.connect("host-2", 900)
    start = world.mark()
    await join_waiting("host-2", {"room_id": game.code, "host_secret": game.secret})
    assert store.stored == [game_id]
    assert "game_persisted" in world.reaching_events("host-2", game.code, start)

    await request_state("p1", {"room_id": game.code})  # stored now: no write
    assert store.calls == 2


@aio
async def test_a_resync_of_an_unstored_game_stores_it(
    world: World, store: FlakyStore
) -> None:
    game = await world.running_game()
    await world.finish_early(game.code)
    await play_service.finish(game.code)  # fails
    start = world.mark()
    await request_state("p1", {"room_id": game.code})
    assert store.calls == 2 and len(store.stored) == 1
    assert "game_persisted" in world.reaching_events("p1", game.code, start)


@aio
async def test_two_finishes_at_once_write_the_game_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails against a finish() that starts a second write while one is in flight."""
    flaky = FlakyStore(failures=0)
    monkeypatch.setattr(db_service, "persist_finished_game", flaky)
    game = await world.running_game()
    await world.finish_early(game.code)
    start = world.mark()
    await asyncio.gather(play_service.finish(game.code), play_service.finish(game.code))
    assert flaky.calls == 1
    assert len(world.sent(game.code, "game_persisted", start)) == 1
