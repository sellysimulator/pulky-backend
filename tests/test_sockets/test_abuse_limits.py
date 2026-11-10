"""Abuse limits at the socket layer (spec 0.6): the handshake cap and @guarded."""

import inspect
from typing import Any

import pytest

from app.api.v1 import config as config_routes
from app.core import abuse
from app.services.state_service import StateService
from app.sockets.handlers.connection import connect, disconnect
from app.sockets.handlers.lobby import join
from app.sockets.handlers.play import preview_decision, request_state
from tests.test_sockets.conftest import World, guest

aio = pytest.mark.asyncio


def environ(ip: str) -> dict[str, Any]:
    return {"asgi.scope": {"headers": [(b"x-forwarded-for", ip.encode())]}}


@aio
async def test_handshake_refuses_sockets_past_the_ip_cap(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(abuse, "SOCKETS_PER_IP", 2)
    assert await connect("s1", environ("1.1.1.1"), {"guestId": guest(1)}) is None
    assert await connect("s2", environ("1.1.1.1"), None) is None
    assert await connect("s3", environ("1.1.1.1"), None) is False
    assert "s3" not in world.sm.sid_to_identity
    assert await connect("t1", environ("2.2.2.2"), None) is None
    await disconnect("s1")
    assert await connect("s3", environ("1.1.1.1"), None) is None


@aio
async def test_a_rejected_token_frees_its_place(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(abuse, "SOCKETS_PER_IP", 1)
    assert await connect("s1", environ("1.1.1.1"), {"idToken": "bad"}) is False
    assert await connect("s2", environ("1.1.1.1"), None) is None


@aio
async def test_joins_naming_unknown_rooms_run_out_per_ip(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails against a server that lets one IP guess room codes without limit."""
    monkeypatch.setattr(abuse.lookup_limiter, "limit", 3)
    for n in range(3):
        await connect(f"s{n}", environ("1.1.1.1"), None)
        await join(f"s{n}", {"room_id": "ZZZ222"})
    assert world.last("s2", "join_error") == {"message": "No room with that code."}
    await connect("s9", environ("1.1.1.1"), None)
    await join("s9", {"room_id": "ZZZ223"})
    assert world.last("s9", "join_error") == {"message": abuse.TOO_MANY_REQUESTS}


@aio
async def test_events_in_the_sockets_own_room_are_not_lookups(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    monkeypatch.setattr(abuse.lookup_limiter, "limit", 1)  # the seat's join spent it
    start = world.mark()
    await request_state("p1", {"room_id": code})
    assert world.sent("p1", "lobby_update", since=start)
    await request_state("p1", {"room_id": "ZZZ222"})
    assert world.sent("p1", "error", since=start) == [
        {"code": "RATE_LIMITED", "message": abuse.TOO_MANY_REQUESTS}
    ]


@aio
async def test_a_flooding_socket_is_cut_off_before_its_handler(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    monkeypatch.setattr(abuse.sid_event_limiter, "limit", 2)  # the join was one
    start = world.mark()
    for _ in range(3):
        await request_state("p1", {"room_id": code})
    assert len(world.sent("p1", "lobby_update", since=start)) == 1
    assert (
        world.sent("p1", "error", since=start)
        == [{"code": "RATE_LIMITED", "message": abuse.TOO_MANY_REQUESTS}] * 2
    )


@aio
async def test_a_failing_cleanup_still_releases_the_socket_slot(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails against a disconnect that releases the slot only after the cleanup."""
    monkeypatch.setattr(abuse, "SOCKETS_PER_IP", 1)
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    assert abuse._open_per_ip and "p1" in abuse._sid_ip

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(StateService, "get_room", boom)
    with pytest.raises(RuntimeError):
        await disconnect("p1")
    assert not abuse._open_per_ip and "p1" not in abuse._sid_ip
    assert "p1" not in world.sm.sid_to_identity
    assert await connect("p2", {}, None) is None


@aio
async def test_a_flood_of_previews_is_cut_off_before_the_engine(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = await world.create_room()
    await world.seat(code, "p1", 1)
    monkeypatch.setattr(abuse.sid_event_limiter, "limit", 2)  # the join was one
    start = world.mark()
    answers = [await preview_decision("p1", {"room_id": code}) for _ in range(2)]
    assert answers[0] is not None and answers[0].get("code") == "GAME_NOT_RUNNING"
    assert answers[1] is None
    assert world.sent("p1", "error", since=start) == [
        {"code": "RATE_LIMITED", "message": abuse.TOO_MANY_REQUESTS}
    ]


def test_cpu_bound_config_routes_run_off_the_event_loop() -> None:
    """Plain defs: FastAPI runs them in its threadpool (spec 0.6)."""
    for route in (config_routes.config_validate, config_routes.config_preview):
        assert not inspect.iscoroutinefunction(route)
