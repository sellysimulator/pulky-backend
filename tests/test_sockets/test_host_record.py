"""The host record kept at ``start_game`` (spec 11.3, 13.5).

A finished game records its host only when the starting connection's handshake
verified a Firebase token: ``start_game`` keeps that uid as the room's server-only
``host_uid``, after the host secret passed, and the first start wins. A guest, a
forged guest id and a refused start keep nothing; the live host identity used to
recover the host screen is untouched."""

from typing import Any

import pytest

from app.sockets.handlers.connection import connect, disconnect
from app.sockets.handlers.lobby import join_waiting, start_game
from tests.conftest import contains_key, contains_value
from tests.fixtures.sockets import (
    World,
)

pytestmark = pytest.mark.asyncio

HOST_UID = "firebase-uid-host-5521"
OTHER_UID = "firebase-uid-other-8832"
DASHBOARD = "dashboard-1"


async def ready_room(world: World) -> tuple[str, str]:
    """A room with two seated guests, ready to start; no host screen claimed."""
    code = await world.create_room()
    secret = str((await world.room(code))["host_secret"])
    await world.seat(code, "p1", 1)
    await world.seat(code, "p2", 2)
    return code, secret


async def host_uid(world: World, code: str) -> Any:
    return (await world.room(code)).get("host_uid")


async def test_a_new_room_has_no_host_record(world: World) -> None:
    code, _ = await ready_room(world)
    assert await host_uid(world, code) is None


async def test_a_signed_in_host_screen_records_its_verified_uid(world: World) -> None:
    code, secret = await ready_room(world)
    await world.claim_host(code, "host-1", HOST_UID)
    await start_game("host-1", {"room_id": code, "host_secret": secret})
    room = await world.room(code)
    assert room["state"] == "running"
    assert room["host_uid"] == HOST_UID
    assert room["host_identity"] == HOST_UID  # live recovery is unchanged


async def test_a_signed_in_dashboard_start_records_its_uid(world: World) -> None:
    """The dashboard never runs join_waiting, so the room has no host identity."""
    code, secret = await ready_room(world)
    await world.connect(DASHBOARD, HOST_UID)
    await start_game(DASHBOARD, {"room_id": code, "host_secret": secret})
    room = await world.room(code)
    assert room["state"] == "running"
    assert room["host_identity"] is None
    assert room["host_uid"] == HOST_UID


async def test_a_guest_host_records_nothing(world: World) -> None:
    code, secret = await ready_room(world)
    await world.claim_host(code, "host-1", 900)
    await start_game("host-1", {"room_id": code, "host_secret": secret})
    assert (await world.room(code))["state"] == "running"
    assert await host_uid(world, code) is None


async def test_a_guest_dashboard_records_nothing(world: World) -> None:
    code, secret = await ready_room(world)
    await world.connect(DASHBOARD, 900)
    await start_game(DASHBOARD, {"room_id": code, "host_secret": secret})
    assert (await world.room(code))["state"] == "running"
    assert await host_uid(world, code) is None


@pytest.mark.parametrize(
    "auth",
    [
        {"guestId": HOST_UID},  # a uid-shaped guest id is not a guest id at all
        {"guestId": f"user_{'0' * 36}"},
        {"guestId": HOST_UID, "uid": HOST_UID},
    ],
)
async def test_an_unverified_handshake_never_records_a_host(
    world: World, auth: dict[str, str] | None
) -> None:
    """Only the token branch marks a uid verified: an identity that merely lacks the
    ``guest_`` prefix, or a uid the client names, is never recorded."""
    code, secret = await ready_room(world)
    assert await connect(DASHBOARD, {}, auth) is not False
    await start_game(DASHBOARD, {"room_id": code, "host_secret": secret})
    assert (await world.room(code))["state"] == "running"
    assert await host_uid(world, code) is None


@pytest.mark.parametrize("auth", [{"uid": HOST_UID}, None])
async def test_a_handshake_naming_no_identity_is_refused_and_records_nothing(
    world: World, auth: dict[str, str] | None
) -> None:
    """A uid the client names is no identity at all: the handshake is refused."""
    await ready_room(world)
    assert await connect(DASHBOARD, {}, auth) is False
    assert DASHBOARD not in world.sm.sid_to_verified_uid
    assert DASHBOARD not in world.sm.sid_to_identity


async def test_a_rejected_token_records_nothing(world: World) -> None:
    code, _ = await ready_room(world)
    assert await connect(DASHBOARD, {}, {"idToken": "not-a-token"}) is False
    assert DASHBOARD not in world.sm.sid_to_verified_uid
    assert await host_uid(world, code) is None


async def test_a_wrong_secret_records_nothing(world: World) -> None:
    code, _ = await ready_room(world)
    await world.connect(DASHBOARD, HOST_UID)
    await start_game(DASHBOARD, {"room_id": code, "host_secret": "wrong"})
    assert (await world.room(code))["state"] == "lobby"
    assert await host_uid(world, code) is None


async def test_a_refused_start_records_nothing(world: World) -> None:
    """The secret passed but the room cannot start yet (one seat short)."""
    code = await world.create_room()
    secret = str((await world.room(code))["host_secret"])
    await world.seat(code, "p1", 1)
    await world.connect(DASHBOARD, HOST_UID)
    await start_game(DASHBOARD, {"room_id": code, "host_secret": secret})
    assert (await world.room(code))["state"] == "lobby"
    assert await host_uid(world, code) is None


async def test_the_first_start_wins(world: World) -> None:
    code, secret = await ready_room(world)
    await world.connect(DASHBOARD, HOST_UID)
    await start_game(DASHBOARD, {"room_id": code, "host_secret": secret})
    await world.connect("dashboard-2", OTHER_UID)
    await start_game("dashboard-2", {"room_id": code, "host_secret": secret})
    assert await host_uid(world, code) == HOST_UID


async def test_a_guest_who_signed_in_before_starting_is_recorded(
    world: World,
) -> None:
    """Signing in reconnects the socket with a token; the start comes from that
    socket, so the uid is recorded though the host screen was claimed as a guest."""
    code, secret = await ready_room(world)
    await world.claim_host(code, "host-1", 900)
    await disconnect("host-1")
    await world.connect("host-2", HOST_UID)
    await join_waiting("host-2", {"room_id": code, "host_secret": secret})
    await start_game("host-2", {"room_id": code, "host_secret": secret})
    room = await world.room(code)
    assert room["host_uid"] == HOST_UID
    assert room["host_identity"] != HOST_UID  # the live identity stays the first


async def test_disconnect_forgets_the_verified_uid(world: World) -> None:
    await world.connect(DASHBOARD, HOST_UID)
    assert world.sm.sid_to_verified_uid[DASHBOARD] == HOST_UID
    await disconnect(DASHBOARD)
    assert DASHBOARD not in world.sm.sid_to_verified_uid


async def test_the_host_record_is_sent_to_no_one(world: World) -> None:
    code, secret = await ready_room(world)
    await world.claim_host(code, "host-1", HOST_UID)
    await start_game("host-1", {"room_id": code, "host_secret": secret})
    await world.finish_early(code, game_id="pub-game-1")
    await world.connect("host-2", HOST_UID)
    await join_waiting("host-2", {"room_id": code})
    assert await host_uid(world, code) == HOST_UID
    assert world.sm.records
    for target, event, data, _ in world.sm.records:
        assert not contains_key(data, "host_uid"), f"host_uid in {event} to {target}"
        assert not contains_value(data, HOST_UID), f"uid in {event} to {target}"
        assert HOST_UID not in str(data), f"uid in {event} to {target}"
