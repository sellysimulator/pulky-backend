"""The multi-game dashboard's "Start all ready games" (spec 11.3, 12.1).

The dashboard's socket never claims a room: it sends the ordinary `start_game` with
the room code and host secret. `start_game` checks only the secret, so the start is
safe from any socket that holds it, and the host screen (claimed or not) still gets
the host's state."""

import pytest

from app.core import abuse
from app.services.room_service import STATUS_NOT_FOUND
from app.sockets.handlers.lobby import join_waiting, start_game
from tests.fixtures.sockets import (
    World,
)

pytestmark = pytest.mark.asyncio

DASHBOARD = "dashboard-1"


async def seated_room(world: World, *, host_open: bool) -> tuple[str, str]:
    code = await world.create_room()
    secret = str((await world.room(code))["host_secret"])
    if host_open:
        await world.claim_host(code, "host-1")
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    await world.connect(DASHBOARD, 900)
    return code, secret


async def test_the_dashboard_starts_a_room_whose_host_screen_is_open(
    world: World,
) -> None:
    code, secret = await seated_room(world, host_open=True)
    since = world.mark()
    await start_game(DASHBOARD, {"room_id": code, "host_secret": secret})

    assert (await world.room(code))["state"] == "running"
    assert world.sent("host-1", "host_state", since)
    assert world.sent("p1", "your_state", since)
    # The dashboard gets nothing back, and joins no room.
    assert world.sent(DASHBOARD, since=since) == []
    assert DASHBOARD not in world.sm.sid_to_host_room
    assert DASHBOARD not in world.sm.sid_to_alias


async def test_a_host_screen_opened_after_the_start_catches_up(world: World) -> None:
    code, secret = await seated_room(world, host_open=False)
    await start_game(DASHBOARD, {"room_id": code, "host_secret": secret})
    assert (await world.room(code))["state"] == "running"

    since = world.mark()
    await world.connect("host-2", 900)
    await join_waiting("host-2", {"room_id": code, "host_secret": secret})
    assert world.sent("host-2", "host_claimed", since)
    assert world.sent("host-2", "host_state", since)


async def test_a_wrong_secret_starts_nothing(world: World) -> None:
    code, _ = await seated_room(world, host_open=True)
    await start_game(DASHBOARD, {"room_id": code, "host_secret": "wrong"})
    assert (await world.room(code))["state"] == "lobby"
    assert world.last(DASHBOARD, "join_error") == {
        "message": "Only the host can do that.",
        "room_code": code,
    }


async def test_every_start_refusal_names_its_room_for_the_dashboard_card(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 11.3: each refusal of ``start_game`` carries ``room_code`` next to
    ``message``, so "Start all" can put it on that game's card. Additive: other
    ``join_error`` senders are unchanged."""
    code, secret = await seated_room(world, host_open=False)
    await start_game(DASHBOARD, {"room_id": "nope99", "host_secret": secret})
    assert world.last(DASHBOARD, "join_error") == {
        "message": STATUS_NOT_FOUND,
        "room_code": "NOPE99",
    }
    await start_game(DASHBOARD, {"room_id": code.lower(), "host_secret": "x"})
    assert world.last(DASHBOARD, "join_error")["room_code"] == code
    monkeypatch.setattr(abuse, "allow_event", lambda sid, lookup: False)
    await start_game(DASHBOARD, {"room_id": code, "host_secret": secret})
    assert world.last(DASHBOARD, "join_error") == {
        "message": abuse.TOO_MANY_REQUESTS,
        "room_code": code,
    }
    assert (await world.room(code))["state"] == "lobby"
