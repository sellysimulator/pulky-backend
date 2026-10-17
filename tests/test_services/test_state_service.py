"""Black-box tests for the state service (section 13).

Every behaviour runs on both backends: ``InMemoryBackend`` and ``FakeRedis``
(spec 13.3), through the section 01 fixtures.
"""

import asyncio
import copy
import json
import random
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services import state_service
from app.services.state_service import (
    ROOM_CODE_ALPHABET,
    ROOM_CODE_LENGTH,
    SCHEMA_VERSION,
    StateService,
    generate_room_code,
    get_state_service,
    new_host_secret,
    new_room_document,
    next_free_alias,
    reset_state_service,
)

BACKEND_KINDS = ["memory", "redis"]
ROOT = Path(__file__).resolve().parents[2]
DOCUMENT_KEYS = {
    "schema_version",
    "room_code",
    "state",
    "created_at",
    "started_at",
    "finished_at",
    "host_secret",
    "host_sid",
    "host_identity",
    "host_display_name",
    "participants",
    "sid_to_alias",
    "seed",
    "config",
    "engine",
    "seq",
    "persisted",
    "game_id",
}


@pytest.fixture(params=BACKEND_KINDS)
def backend(request: pytest.FixtureRequest) -> Any:
    """The same suite over the in-memory backend and the Redis stand-in."""
    name = "memory_backend" if request.param == "memory" else "fake_redis"
    return request.getfixturevalue(name)


@pytest.fixture
def svc(backend: Any) -> StateService:
    return StateService(backend)


@pytest.fixture(autouse=True)
def _fresh_singleton() -> Any:
    reset_state_service()
    yield
    reset_state_service()


class SpyBackend:
    """Delegates to a real backend and records the lock calls it receives."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.lock_calls: list[tuple[str, int]] = []

    async def get(self, key: str) -> Any:
        return await self.inner.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> Any:
        return await self.inner.setex(key, ttl, value)

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> Any:
        return await self.inner.set(key, value, ex=ex, nx=nx)

    async def delete(self, *keys: str) -> int:
        return await self.inner.delete(*keys)

    async def exists(self, key: str) -> int:
        return await self.inner.exists(key)

    def lock(self, key: str, timeout: int = 10) -> Any:
        self.lock_calls.append((key, timeout))
        return self.inner.lock(key, timeout=timeout)


class BytesBackend(SpyBackend):
    """Like a real Redis client: values come back as bytes."""

    async def get(self, key: str) -> Any:
        value = await self.inner.get(key)
        return value.encode("utf-8") if isinstance(value, str) else value


def _doc(code: str = "ABC234", **overrides: Any) -> dict[str, Any]:
    room = new_room_document(
        room_code=code,
        config={"max_players": 4},
        host_display_name="Ms. Ruiz",
        host_secret="s" * 43,
        created_at="2026-10-12T14:03:22Z",
    )
    room.update(overrides)
    return room


def _force_codes(monkeypatch: pytest.MonkeyPatch, codes: list[str]) -> list[int]:
    """Make generate_room_code yield ``codes`` in order, then the last one forever."""
    calls: list[int] = []

    def fake(rng: random.Random | None = None) -> str:
        calls.append(1)
        return codes[min(len(calls), len(codes)) - 1]

    monkeypatch.setattr("app.services.state_service.generate_room_code", fake)
    return calls


# --- constants, code and secret generation ---------------------------------


def test_constants() -> None:
    assert SCHEMA_VERSION == 1
    assert ROOM_CODE_ALPHABET == "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    assert ROOM_CODE_LENGTH == 6
    assert not set("ILO01") & set(ROOM_CODE_ALPHABET)


def test_generate_room_code_worked_example() -> None:
    code = generate_room_code(random.Random(1))
    assert len(code) == 6
    assert all(c in ROOM_CODE_ALPHABET for c in code)
    assert generate_room_code(random.Random(1)) == code


@given(st.integers(min_value=0, max_value=10_000))
def test_generate_room_code_always_valid(seed: int) -> None:
    code = generate_room_code(random.Random(seed))
    assert len(code) == ROOM_CODE_LENGTH
    assert set(code) <= set(ROOM_CODE_ALPHABET)


def test_generate_room_code_without_rng_varies() -> None:
    codes = {generate_room_code() for _ in range(30)}
    assert all(len(c) == 6 and set(c) <= set(ROOM_CODE_ALPHABET) for c in codes)
    assert len(codes) > 1


def test_new_host_secret_is_long_url_safe_and_unique() -> None:
    secrets_ = {new_host_secret() for _ in range(20)}
    assert len(secrets_) == 20
    for s in secrets_:
        assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", s)


# --- the document ------------------------------------------------------------


def test_new_room_document_shape() -> None:
    config = {"max_players": 4}
    room = new_room_document(
        room_code="ABC234",
        config=config,
        host_display_name="Ms. Ruiz",
        host_secret="tok",
        created_at="2026-10-12T14:03:22Z",
    )
    assert room == {
        "schema_version": 1,
        "room_code": "ABC234",
        "state": "lobby",
        "created_at": "2026-10-12T14:03:22Z",
        "started_at": None,
        "finished_at": None,
        "host_secret": "tok",
        "host_sid": None,
        "host_identity": None,
        "host_display_name": "Ms. Ruiz",
        "participants": {},
        "sid_to_alias": {},
        "seed": None,
        "config": {"max_players": 4},
        "engine": None,
        "seq": 0,
        "persisted": False,
        "game_id": None,
    }


# --- AC 2: create_room -------------------------------------------------------


@pytest.mark.asyncio
async def test_create_room_document(svc: StateService, backend: Any) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    room = await svc.create_room({"max_players": 4}, "Ms. Ruiz")
    after = datetime.now(UTC)

    assert set(room) == DOCUMENT_KEYS
    code = room["room_code"]
    assert len(code) == 6 and set(code) <= set(ROOM_CODE_ALPHABET)
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", room["host_secret"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", room["created_at"])
    created = datetime.strptime(room["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert before <= created.replace(tzinfo=UTC) <= after

    expected = _doc(
        code,
        host_secret=room["host_secret"],
        created_at=room["created_at"],
    )
    assert room == expected
    assert await backend.exists(f"room:{code}") == 1
    assert await svc.get_room(code) == room
    assert await svc.room_exists(code) is True


@pytest.mark.asyncio
async def test_create_room_mints_distinct_codes_and_secrets(svc: StateService) -> None:
    rooms = [await svc.create_room({}, "H") for _ in range(5)]
    assert len({r["room_code"] for r in rooms}) == 5
    assert len({r["host_secret"] for r in rooms}) == 5


# --- AC 3: round trip --------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_is_exact(svc: StateService) -> None:
    room = _doc(
        state="running",
        started_at="2026-10-12T14:10:00Z",
        host_display_name="Señora Ruíz ñ",
        participants={
            "P1": {
                "alias": "P1",
                "identity": "guest_x",
                "session_token": "t",
                "display_name": "Pulquería \u00e1",
                "connected": True,
            }
        },
        sid_to_alias={"sid1": "P1"},
        seed=12345,
        engine={"day": 3, "players": {"P1": {"cash": 1234.56, "cellar": [{"qty": 5}]}}},
        seq=7,
        persisted=True,
        game_id="g_1",
    )
    await svc.save_room("ABC234", room)
    assert await svc.get_room("ABC234") == room


@pytest.mark.asyncio
async def test_values_returned_as_bytes_are_decoded(backend: Any) -> None:
    svc = StateService(BytesBackend(backend))
    room = _doc(host_display_name="Señora ñ")
    await svc.save_room("ABC234", room)
    assert await svc.get_room("ABC234") == room


# --- save_room ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_room_rejects_mismatched_code(svc: StateService) -> None:
    with pytest.raises(ValueError):
        await svc.save_room("XYZ789", _doc("ABC234"))
    assert await svc.room_exists("XYZ789") is False
    assert await svc.room_exists("ABC234") is False


@pytest.mark.asyncio
async def test_save_room_accepts_lowercase_code(svc: StateService) -> None:
    await svc.save_room("abc234", _doc("ABC234"))
    assert (await svc.get_room("ABC234"))["room_code"] == "ABC234"


# --- failure mode 5: case-insensitive codes ----------------------------------


@pytest.mark.asyncio
async def test_codes_are_case_insensitive(svc: StateService, backend: Any) -> None:
    room = await svc.create_room({"max_players": 4}, "Ms. Ruiz")
    code = room["room_code"]
    assert (await svc.get_room(code.lower()))["host_secret"] == room["host_secret"]
    assert await svc.room_exists(code.lower()) is True
    assert await backend.exists(f"room:{code}") == 1

    await svc.save_room(code.lower(), {**room, "seq": 3})
    assert (await svc.get_room(code))["seq"] == 3

    await svc.delete_room(code.lower())
    assert await svc.get_room(code) is None
    assert await backend.exists(f"room:{code}") == 0


@pytest.mark.asyncio
async def test_missing_room(svc: StateService) -> None:
    assert await svc.get_room("NOPE22") is None
    assert await svc.room_exists("NOPE22") is False


@pytest.mark.asyncio
async def test_delete_room(svc: StateService) -> None:
    room = await svc.create_room({}, "H")
    await svc.delete_room(room["room_code"])
    assert await svc.get_room(room["room_code"]) is None
    assert await svc.room_exists(room["room_code"]) is False


# --- AC 4 / failure mode 2: schema guard -------------------------------------


def _stale_variants() -> list[tuple[str, dict[str, Any]]]:
    good = _doc("OLD234")
    no_sid = {k: v for k, v in good.items() if k != "sid_to_alias"}
    no_secret = {k: v for k, v in good.items() if k != "host_secret"}
    no_version = {k: v for k, v in good.items() if k != "schema_version"}
    return [
        ("version0", {**good, "schema_version": 0}),
        ("version2", {**good, "schema_version": 2}),
        ("no_sid_to_alias", no_sid),
        ("no_host_secret", no_secret),
        ("no_schema_version", no_version),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,doc", _stale_variants(), ids=lambda v: v if isinstance(v, str) else ""
)
async def test_stale_document_is_deleted_and_not_returned(
    svc: StateService, backend: Any, name: str, doc: dict[str, Any]
) -> None:
    """A stale document comes back as None in no form and is gone afterwards.

    Fails against a service that migrates it in place (returns or re-saves it).
    """
    await backend.setex("room:OLD234", 60, json.dumps(doc))
    assert await svc.get_room("OLD234") is None
    assert await backend.exists("room:OLD234") == 0
    assert await backend.get("room:OLD234") is None
    assert await svc.get_room("OLD234") is None
    assert await svc.room_exists("OLD234") is False


@pytest.mark.asyncio
async def test_current_document_survives_the_guard(
    svc: StateService, backend: Any
) -> None:
    await backend.setex("room:GOOD22", 60, json.dumps(_doc("GOOD22")))
    assert await svc.get_room("GOOD22") == _doc("GOOD22")
    assert await backend.exists("room:GOOD22") == 1


# --- AC 5 / failure mode 4: TTL ----------------------------------------------


@pytest.mark.asyncio
async def test_save_sets_ttl(backend: Any) -> None:
    svc = StateService(backend, ttl_seconds=100)
    await svc.save_room("ABC234", _doc())
    backend.advance(99)
    assert await svc.get_room("ABC234") is not None
    backend.advance(2)
    assert await svc.get_room("ABC234") is None


@pytest.mark.asyncio
async def test_default_ttl_is_24_hours(svc: StateService, backend: Any) -> None:
    room = await svc.create_room({}, "H")
    backend.advance(86_400 - 1)
    assert await svc.get_room(room["room_code"]) is not None
    backend.advance(2)
    assert await svc.get_room(room["room_code"]) is None


@pytest.mark.asyncio
async def test_create_room_sets_ttl(backend: Any) -> None:
    svc = StateService(backend, ttl_seconds=100)
    room = await svc.create_room({}, "H")
    backend.advance(101)
    assert await svc.get_room(room["room_code"]) is None


@pytest.mark.asyncio
async def test_each_save_refreshes_the_ttl(backend: Any) -> None:
    """Fails against a save that keeps the first expiry (TTL not refreshed)."""
    svc = StateService(backend, ttl_seconds=100)
    room = _doc()
    await svc.save_room("ABC234", room)
    backend.advance(90)
    await svc.save_room("ABC234", {**room, "seq": 1})
    backend.advance(99)  # 189 s after the first save, 99 s after the second
    assert (await svc.get_room("ABC234"))["seq"] == 1
    backend.advance(2)
    assert await svc.get_room("ABC234") is None


# --- failure mode 6: JSON strings, not live objects --------------------------


@pytest.mark.asyncio
async def test_store_holds_json_not_the_dict(svc: StateService, backend: Any) -> None:
    room = _doc(participants={})
    await svc.save_room("ABC234", room)

    raw = await backend.get("room:ABC234")
    assert isinstance(raw, (str, bytes))
    assert json.loads(raw) == room

    room["seq"] = 99
    room["participants"]["P1"] = {"alias": "P1"}
    stored = await svc.get_room("ABC234")
    assert stored["seq"] == 0
    assert stored["participants"] == {}

    stored["participants"]["P9"] = {"alias": "P9"}
    again = await svc.get_room("ABC234")
    assert again["participants"] == {}
    assert again is not stored


# --- AC 6 / failure mode 1: the lock ------------------------------------------


async def _bump(svc: StateService, code: str) -> None:
    async with svc.lock(code):
        room = await svc.get_room(code)
        await asyncio.sleep(0)
        room["seq"] += 1
        await svc.save_room(code, room)


@pytest.mark.asyncio
async def test_lock_prevents_lost_update(svc: StateService) -> None:
    """Two (then ten) read-modify-write coroutines end at their full count.

    The same test with the ``async with svc.lock`` removed ends at 1 (and 1),
    because both coroutines read 0 before either saves; that unlocked variant
    is the bug this test exists to catch.
    """
    await svc.save_room("ABC234", _doc())
    await asyncio.gather(_bump(svc, "ABC234"), _bump(svc, "ABC234"))
    assert (await svc.get_room("ABC234"))["seq"] == 2

    await asyncio.gather(*(_bump(svc, "abc234") for _ in range(10)))
    assert (await svc.get_room("ABC234"))["seq"] == 12


@pytest.mark.asyncio
async def test_lock_excludes_the_same_room_in_any_case(svc: StateService) -> None:
    entered = asyncio.Event()

    async def contender() -> bool:
        async with svc.lock("abc234"):
            entered.set()
            return True

    async with svc.lock("ABC234"):
        task = asyncio.create_task(contender())
        await asyncio.sleep(0.05)
        assert not entered.is_set()
        assert not task.done()
    assert await asyncio.wait_for(task, timeout=2) is True


@pytest.mark.asyncio
async def test_locks_on_different_rooms_do_not_block(svc: StateService) -> None:
    async def take_b() -> bool:
        async with svc.lock("BBB222"):
            return True

    async with svc.lock("AAA222"):
        assert await asyncio.wait_for(take_b(), timeout=2) is True


@pytest.mark.asyncio
async def test_lock_is_released_after_an_exception(svc: StateService) -> None:
    with pytest.raises(RuntimeError):
        async with svc.lock("ABC234"):
            raise RuntimeError("boom")

    async def again() -> bool:
        async with svc.lock("ABC234"):
            return True

    assert await asyncio.wait_for(again(), timeout=2) is True


@pytest.mark.asyncio
async def test_lock_key_and_timeout(backend: Any) -> None:
    spy = SpyBackend(backend)
    svc = StateService(spy, lock_timeout=7)
    async with svc.lock("abc234"):
        pass
    assert spy.lock_calls == [("lock:room:ABC234", 7)]

    default = SpyBackend(backend)
    async with StateService(default).lock("ABC234"):
        pass
    assert default.lock_calls == [("lock:room:ABC234", 10)]


# --- AC 7: code collisions ----------------------------------------------------


@pytest.mark.asyncio
async def test_create_room_retries_on_collision(
    svc: StateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_codes(monkeypatch, ["ABC234"])
    first = await svc.create_room({}, "First")

    calls = _force_codes(monkeypatch, ["ABC234", "ABC234", "XYZ789"])
    second = await svc.create_room({}, "Second")
    assert second["room_code"] == "XYZ789"
    assert len(calls) == 3
    # the existing room was not overwritten
    assert (await svc.get_room("ABC234"))["host_secret"] == first["host_secret"]
    assert (await svc.get_room("XYZ789"))["host_display_name"] == "Second"


@pytest.mark.asyncio
async def test_create_room_succeeds_on_the_tenth_try(
    svc: StateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_codes(monkeypatch, ["ABC234"])
    await svc.create_room({}, "First")
    calls = _force_codes(monkeypatch, ["ABC234"] * 9 + ["XYZ789"])
    room = await svc.create_room({}, "Second")
    assert room["room_code"] == "XYZ789"
    assert len(calls) == 10


@pytest.mark.asyncio
async def test_create_room_gives_up_after_ten_collisions(
    svc: StateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_codes(monkeypatch, ["ABC234"])
    first = await svc.create_room({}, "First")
    calls = _force_codes(monkeypatch, ["ABC234"])
    with pytest.raises(RuntimeError):
        await svc.create_room({}, "Second")
    assert len(calls) == 10
    assert (await svc.get_room("ABC234"))["host_secret"] == first["host_secret"]


@pytest.mark.asyncio
async def test_simultaneous_creations_drawing_one_code_never_share_it(
    backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write is set-if-absent: an existence check that both creations pass (as two
    racing processes would) cannot let the second overwrite the first."""

    class RacingBackend(SpyBackend):
        async def exists(self, key: str) -> int:
            return 0

    svc = StateService(RacingBackend(backend))
    _force_codes(monkeypatch, ["ABC234", "ABC234", "XYZ789"])
    first, second = await asyncio.gather(
        svc.create_room({}, "First"), svc.create_room({}, "Second")
    )
    assert {first["room_code"], second["room_code"]} == {"ABC234", "XYZ789"}
    for room in (first, second):
        stored = await svc.get_room(room["room_code"])
        assert stored is not None
        assert stored["host_secret"] == room["host_secret"]


# --- next_seq ------------------------------------------------------------------


def test_next_seq_increments_in_place(svc: StateService) -> None:
    room = _doc()
    assert (svc.next_seq(room), svc.next_seq(room)) == (1, 2)
    assert room["seq"] == 2
    room["seq"] = 41
    assert svc.next_seq(room) == 42


@pytest.mark.asyncio
async def test_next_seq_persists_once_the_caller_saves(svc: StateService) -> None:
    room = await svc.create_room({}, "H")
    code = room["room_code"]
    async with svc.lock(code):
        room = await svc.get_room(code)
        svc.next_seq(room)
        await svc.save_room(code, room)
    assert (await svc.get_room(code))["seq"] == 1


# --- AC 8 / failure mode 3: aliases -------------------------------------------


def test_next_free_alias_worked_examples() -> None:
    assert next_free_alias({"participants": {}}) == "P1"
    assert next_free_alias({"participants": {"P1": {}, "P3": {}}}) == "P2"
    assert (
        next_free_alias({"participants": {f"P{i}": {} for i in range(1, 11)}}) == "P11"
    )


def test_alias_is_reissued_after_a_leave_without_touching_others() -> None:
    """P2 left: the next alias is P2, never len + 1 (which would be P3, live)."""
    room = _doc(
        participants={
            f"P{i}": {"alias": f"P{i}", "display_name": f"Player {i}"} for i in (1, 3)
        }
    )
    room["participants"]["P2"] = {"alias": "P2"}
    del room["participants"]["P2"]
    snapshot = copy.deepcopy(room)

    assert next_free_alias(room) == "P2"
    assert room == snapshot
    assert room["participants"]["P3"] == {"alias": "P3", "display_name": "Player 3"}


@pytest.mark.parametrize(
    "taken,expected",
    [
        ({2, 3}, "P1"),
        ({1, 2, 4}, "P3"),
        ({1, 2, 3}, "P4"),
        ({1, 3, 4, 5}, "P2"),
        ({2}, "P1"),
    ],
)
def test_next_free_alias_is_lowest_free(taken: set[int], expected: str) -> None:
    room = {"participants": {f"P{n}": {} for n in taken}}
    assert next_free_alias(room) == expected


@given(st.sets(st.integers(min_value=1, max_value=25)))
def test_next_free_alias_property(taken: set[int]) -> None:
    room = {"participants": {f"P{n}": {} for n in taken}}
    result = next_free_alias(room)
    n = int(result[1:])
    assert result == f"P{n}"
    assert n >= 1
    assert n not in taken
    assert all(k in taken for k in range(1, n))


# --- AC 9: the process singleton ------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_service_is_a_singleton_over_the_process_backend(
    backend: Any,
) -> None:
    reset_state_service()
    first = get_state_service()
    assert get_state_service() is first
    assert isinstance(first, StateService)

    room = await first.create_room({}, "H")
    code = room["room_code"]
    assert await backend.exists(f"room:{code}") == 1
    assert (await get_state_service().get_room(code))["host_secret"] == room[
        "host_secret"
    ]

    reset_state_service()
    assert get_state_service() is not first


# --- spec 13.3 / behaviour 8: no engine or config objects ------------------------


def test_module_does_not_import_the_engine() -> None:
    code = (
        "import sys, app.services.state_service;"
        "bad = [m for m in sys.modules"
        " if m in ('app.core.engine', 'app.core.config_models')];"
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=False)
    assert result.returncode == 0
    assert state_service.SCHEMA_VERSION == 1
