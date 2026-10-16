"""Black-box tests for the handshake (section 15, spec 11.3, criterion 1)."""

import uuid

import pytest

from app.sockets.handlers.connection import GUEST_RE, connect
from tests.test_sockets.conftest import World, guest

aio = pytest.mark.asyncio


def test_guest_pattern_accepts_only_guest_ids() -> None:
    assert GUEST_RE.match(f"guest_{uuid.uuid4()}")
    assert GUEST_RE.match(guest(7))
    assert not GUEST_RE.match("u1")
    assert not GUEST_RE.match("guest_" + "G" * 36)
    assert not GUEST_RE.match("guest_short")
    assert not GUEST_RE.match(f"guest_{uuid.uuid4()}extra")
    assert not GUEST_RE.match(f"xguest_{uuid.uuid4()}")


@aio
async def test_valid_token_sets_the_firebase_uid(world: World) -> None:
    world.tokens["good"] = {"uid": "u1"}
    result = await connect("s1", {}, {"idToken": "good"})
    assert result is not False
    assert world.sm.sid_to_identity["s1"] == "u1"


@aio
async def test_failed_token_rejects_the_connection(world: World) -> None:
    result = await connect("s2", {}, {"idToken": "bad"})
    assert result is False
    assert "s2" not in world.sm.sid_to_identity


@aio
async def test_failed_token_is_never_downgraded_to_a_guest(world: World) -> None:
    # A valid guest id beside a bad token must not rescue the connection.
    result = await connect("s2", {}, {"idToken": "bad", "guestId": guest(3)})
    assert result is False
    assert "s2" not in world.sm.sid_to_identity


@aio
async def test_valid_token_wins_over_a_guest_id(world: World) -> None:
    world.tokens["good"] = {"uid": "u1"}
    result = await connect("s1", {}, {"idToken": "good", "guestId": guest(3)})
    assert result is not False
    assert world.sm.sid_to_identity["s1"] == "u1"


@aio
async def test_guest_id_is_used_as_given(world: World) -> None:
    gid = f"guest_{uuid.uuid4()}"
    result = await connect("s3", {}, {"guestId": gid})
    assert result is not False
    assert world.sm.sid_to_identity["s3"] == gid


@aio
async def test_a_guest_can_never_obtain_a_firebase_uid(world: World) -> None:
    world.tokens["good"] = {"uid": "u1"}
    result = await connect("s4", {}, {"guestId": "u1"})
    assert result is not False
    identity = world.sm.sid_to_identity["s4"]
    assert identity != "u1"
    assert GUEST_RE.match(identity)


@aio
async def test_malformed_guest_ids_get_a_fresh_guest_identity(world: World) -> None:
    for n, bad in enumerate(["", "guest_", "guest_" + "G" * 36, "someone-else"]):
        sid = f"bad{n}"
        assert await connect(sid, {}, {"guestId": bad}) is not False
        identity = world.sm.sid_to_identity[sid]
        assert identity != bad
        assert GUEST_RE.match(identity)


@aio
@pytest.mark.parametrize("auth", [None, {}, {"idToken": "", "guestId": ""}])
async def test_no_credentials_means_a_new_guest_each_time(
    world: World, auth: dict | None
) -> None:
    assert await connect("a", {}, auth) is not False
    assert await connect("b", {}, auth) is not False
    ida, idb = world.sm.sid_to_identity["a"], world.sm.sid_to_identity["b"]
    assert GUEST_RE.match(ida) and GUEST_RE.match(idb)
    assert ida != idb


@aio
async def test_identity_never_goes_onto_the_wire(world: World) -> None:
    world.tokens["good"] = {"uid": "u1"}
    await connect("s1", {}, {"idToken": "good"})
    await connect("s3", {}, {"guestId": guest(5)})
    assert world.sm.records == []
