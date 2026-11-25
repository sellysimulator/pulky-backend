"""Every page gets a room's events in ``seq`` order (spec 12, 13.3), so it drops none.

The wire here takes a different time for every send, as a real one does, and a page
applies an event only when its ``seq`` is newer than the last one it applied.
"""

import asyncio
import random
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

import pytest

from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import join, join_waiting
from tests.conftest import FakeSocketManager
from tests.fixtures.play_loop import submit
from tests.fixtures.sockets import Game, World

pytestmark = pytest.mark.asyncio
SEEDS = range(12)
PLAYERS = 4


class Wire:
    """Delivers each emit, after a random number of loop turns, to the pages that were
    in the room when it was sent."""

    def __init__(self, sockets: FakeSocketManager) -> None:
        self.sockets = sockets
        self.random = random.Random(0)
        self.rooms: dict[str, set[str]] = defaultdict(set)
        self.arrived: dict[str, list[tuple[str, int]]] = defaultdict(list)
        self.broadcast: list[tuple[str, int]] = []
        self.dead: set[str] = set()

    async def _deliver(
        self, target: str, sids: list[str], event: str, data: dict
    ) -> None:
        for _ in range(self.random.randrange(6)):
            await asyncio.sleep(0)
        self.sockets.records.append((target, event, data, None))
        for sid in sids:
            if "seq" in data:
                self.arrived[sid].append((event, data["seq"]))

    async def emit_to_room(
        self, room_code: str, event: str, data: dict, skip_sid: str | None = None
    ) -> None:
        sids = [s for s in self.rooms[room_code] if s != skip_sid]
        for sid in [s for s in sids if s in self.dead]:
            self.dead.discard(sid)
            self.rooms[room_code].discard(sid)
            sids.remove(sid)
            await disconnect(sid)  # as Socket.IO does, inside the send
        if skip_sid is None:
            self.broadcast.append((event, data["seq"]))
        await self._deliver(room_code, sids, event, data)

    async def emit_to_sid(self, sid: str, event: str, data: dict) -> None:
        await self._deliver(sid, [sid], event, data)

    async def join_room(self, sid: str, room_code: str, alias: str) -> None:
        self.rooms[room_code].add(sid)
        self.sockets.sid_to_alias[sid] = (room_code, alias)

    async def enter_room(self, sid: str, room_code: str) -> None:
        self.rooms[room_code].add(sid)
        self.sockets.sid_to_host_room[sid] = room_code

    def out_of_order(self) -> dict[str, list[tuple[str, int]]]:
        """Per page, the events its ``seq`` gate would drop."""
        dropped: dict[str, list[tuple[str, int]]] = {}
        for sid, events in self.arrived.items():
            last = 0
            for event, seq in events:
                if seq <= last:
                    dropped.setdefault(sid, []).append((event, seq))
                else:
                    last = seq
        return dropped

    def missed(self) -> dict[str, list[tuple[str, int]]]:
        """Per page, the room events later than its first event that never came."""
        missed: dict[str, list[tuple[str, int]]] = {}
        for sid, events in self.arrived.items():
            first = min(seq for _, seq in events)
            for event, seq in self.broadcast:
                if seq > first and (event, seq) not in events:
                    missed.setdefault(sid, []).append((event, seq))
        return missed

    def forget(self) -> None:
        self.arrived.clear()
        self.broadcast.clear()

    def got(self, sid: str) -> set[str]:
        return {event for event, _ in self.arrived[sid]}


@pytest.fixture
def wire(world: World, monkeypatch: pytest.MonkeyPatch) -> Iterator[Wire]:
    wire = Wire(world.sm)
    for name in ("emit_to_room", "emit_to_sid", "join_room", "enter_room"):
        monkeypatch.setattr(world.sm, name, getattr(wire, name))
    yield wire


async def new_pages(world: World, game: Game) -> dict[str, str]:
    """Everyone's connection dropped and a new one is open: alias -> its sid."""
    pages: dict[str, str] = {}
    for n, (alias, sid) in enumerate(list(game.sids.items()), start=1):
        await disconnect(sid)
        pages[alias] = f"again-{alias}"
        await world.connect(pages[alias], n)
    return pages


def rejoin(game: Game, alias: str, sid: str) -> Any:
    return join(sid, {"room_id": game.code, "session_token": game.tokens[alias]})


@pytest.mark.parametrize("seed", SEEDS)
async def test_players_whose_pages_rejoin_together_all_get_their_state(
    world: World, wire: Wire, seed: int
) -> None:
    """Fails when the events are sent in any order: a later ``participant_reconnected``
    reaches a page before its own ``your_state``, which the page then drops."""
    game = await world.running_game(players=PLAYERS)
    pages = await new_pages(world, game)
    wire.forget()
    wire.random.seed(seed)
    await asyncio.gather(*(rejoin(game, a, s) for a, s in pages.items()))
    assert wire.out_of_order() == {} and wire.missed() == {}
    assert all("your_state" in wire.got(sid) for sid in pages.values())


@pytest.mark.parametrize("seed", SEEDS)
async def test_players_joining_the_lobby_together_all_see_who_is_in_it(
    world: World, wire: Wire, seed: int
) -> None:
    """Fails when a page enters the room after its join's turn: an update in between,
    its own included, never reaches it."""
    code = await world.create_room()
    pages = [f"p{n}" for n in range(1, PLAYERS + 1)]
    for n, sid in enumerate(pages, start=1):
        await world.connect(sid, n)
    wire.random.seed(seed)
    await asyncio.gather(*(join(sid, {"room_id": code}) for sid in pages))
    assert wire.out_of_order() == {} and wire.missed() == {}
    last = (await world.room(code))["seq"]
    assert all(wire.arrived[sid][-1] == ("lobby_update", last) for sid in pages)


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_host_claiming_a_finished_game_among_rejoins_gets_its_view(
    world: World, wire: Wire, seed: int
) -> None:
    game = await world.running_game(players=PLAYERS)
    await world.finish_early(game.code, game_id="a" * 32)
    pages = await new_pages(world, game)
    await world.connect("host-2", 900)
    wire.forget()
    wire.random.seed(seed)
    await asyncio.gather(
        join_waiting("host-2", game.start_data()),
        *(rejoin(game, a, s) for a, s in pages.items()),
    )
    assert wire.out_of_order() == {} and wire.missed() == {}
    assert {"host_state", "game_finished"} <= wire.got("host-2")


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_day_closing_among_other_events_reaches_every_page(
    world: World, wire: Wire, seed: int
) -> None:
    game = await world.running_game(players=2)
    first, second = game.sids.values()
    await world.connect("host-2", 900)
    wire.forget()
    wire.random.seed(seed)
    await asyncio.gather(
        *(submit(game, alias, 1) for alias in game.sids),
        join_waiting("host-2", game.start_data()),
        disconnect(game.host_sid),
    )
    assert wire.out_of_order() == {}
    for sid in (first, second):
        assert {"day_closed", "your_day_closed", "your_state"} <= wire.got(sid)


async def test_a_connection_found_dead_during_a_send_does_not_block_the_room(
    world: World, wire: Wire
) -> None:
    game = await world.running_game(players=2)
    first, second = game.sids.values()
    wire.forget()
    wire.dead.add(second)
    async with asyncio.timeout(1):
        await submit(game, next(iter(game.sids)), 1)
    assert wire.out_of_order() == {}
    assert "participant_disconnected" in wire.got(first)
