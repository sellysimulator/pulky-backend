"""Shared test doubles for every backend suite (section 01).

The environment block comes first and runs before anything imports ``app``:
environment variables take precedence over the dotenv file, so the suite
never sees a developer's real settings (spec section 13.12).
"""

import json
import os

_SETTINGS_DEFAULTS: dict[str, str] = {
    "APP_NAME": "Pulky",
    "VERSION": "0.1.0",
    "DEBUG": "false",
    "HOST": "0.0.0.0",
    "PORT": "8080",
    "CORS_ORIGINS": json.dumps(["https://pulkysim.web.app", "http://localhost:5173"]),
    "DB_HOST": "localhost",
    "DB_PORT": "3306",
    "DB_USER": "root",
    "DB_PASSWORD": "",
    "DB_DATABASE": "pulky",
    "DB_REQUIRE_SSL": "true",
    "DB_SSL_CA": "",
    "REDIS_ENABLED": "false",
    "REDIS_URL": "",
    "FIREBASE_SERVICE_ACCOUNT_JSON": "",
}
os.environ.update(_SETTINGS_DEFAULTS)

import asyncio  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def contains_key(payload: object, key: str) -> bool:
    """True when ``key`` is a dict key anywhere in nested dicts and lists."""
    if isinstance(payload, dict):
        if key in payload:
            return True
        return any(contains_key(v, key) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(contains_key(v, key) for v in payload)
    return False


def _same(a: object, b: object) -> bool:
    return type(a) is type(b) and a == b


def contains_value(payload: object, value: object) -> bool:
    """True when ``value`` is a dict value or list item anywhere in the payload."""
    if isinstance(payload, dict):
        return any(
            _same(v, value) or contains_value(v, value) for v in payload.values()
        )
    if isinstance(payload, (list, tuple)):
        return any(_same(v, value) or contains_value(v, value) for v in payload)
    return False


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeLock:
    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock

    async def __aenter__(self) -> "_FakeLock":
        await self._lock.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


class FakeRedis:
    """Same operations, per-key locks and TTLs as redis.asyncio.Redis."""

    def __init__(self) -> None:
        self._clock = _Clock()
        self._data: dict[str, tuple[float | None, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def advance(self, seconds: float) -> None:
        self._clock.now += seconds

    def _live(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and expires_at <= self._clock.now:
            del self._data[key]
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self._data[key] = (self._clock.now + ttl if ttl else None, value)
        return True

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> bool | None:
        if nx and self._live(key) is not None:
            return None
        return await self.setex(key, ex, value)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._data.pop(key, None) is not None:
                removed += 1
        return removed

    async def exists(self, key: str) -> int:
        return 1 if self._live(key) is not None else 0

    def lock(self, key: str, timeout: int = 10) -> _FakeLock:
        lock = self._locks.setdefault(key, asyncio.Lock())
        return _FakeLock(lock)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRedis]:
    from app.services import state_backend

    fake = FakeRedis()
    monkeypatch.setattr(state_backend, "get_state_backend", lambda: fake)
    yield fake
    state_backend.reset_state_backend()


@pytest.fixture
def memory_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from app.services import state_backend

    clock = _Clock()

    class ControlledMemoryBackend(state_backend.InMemoryBackend):
        def advance(self, seconds: float) -> None:
            clock.now += seconds

    backend = ControlledMemoryBackend(clock=clock)
    monkeypatch.setattr(state_backend, "get_state_backend", lambda: backend)
    yield backend
    state_backend.reset_state_backend()


class FirebaseTokens(dict[str, dict[str, Any]]):
    """``{token: claims}``: exactly these keys verify."""


@pytest.fixture
def firebase_tokens(monkeypatch: pytest.MonkeyPatch) -> FirebaseTokens:
    from app.api import deps
    from app.core import firebase

    tokens = FirebaseTokens()

    def verify(token: str) -> dict[str, Any] | None:
        claims = tokens.get(token)
        return dict(claims) if claims is not None else None

    monkeypatch.setattr(deps, "verify_firebase_id_token", verify)
    monkeypatch.setattr(firebase, "init_firebase", lambda: object())
    return tokens


Emit = tuple[str, str, dict[str, Any], str | None]


class FakeSocketManager:
    """Records every emit as ``(target, event, data, skip_sid)``."""

    def __init__(self) -> None:
        self.sid_to_identity: dict[str, str] = {}
        self.sid_to_alias: dict[str, tuple[str, str]] = {}
        self.sid_to_host_room: dict[str, str] = {}
        self.records: list[Emit] = []

    def emits(self, event: str) -> list[Emit]:
        return [r for r in self.records if r[1] == event]

    def emits_to(self, target: str) -> list[Emit]:
        return [r for r in self.records if r[0] == target]

    async def emit_to_room(
        self,
        room_code: str,
        event: str,
        data: dict[str, Any],
        skip_sid: str | None = None,
    ) -> None:
        self.records.append((room_code, event, data, skip_sid))

    async def emit_to_sid(self, sid: str, event: str, data: dict[str, Any]) -> None:
        self.records.append((sid, event, data, None))

    async def join_room(self, sid: str, room_code: str, alias: str) -> None:
        self.sid_to_alias[sid] = (room_code, alias)

    async def enter_room(self, sid: str, room_code: str) -> None:
        self.sid_to_host_room[sid] = room_code

    async def leave_room(self, sid: str, room_code: str) -> None:
        self.sid_to_alias.pop(sid, None)
        self.sid_to_host_room.pop(sid, None)

    def forget(self, sid: str) -> None:
        self.sid_to_identity.pop(sid, None)
        self.sid_to_alias.pop(sid, None)
        self.sid_to_host_room.pop(sid, None)


@pytest.fixture
def fake_socket_manager(monkeypatch: pytest.MonkeyPatch) -> FakeSocketManager:
    from app.sockets import manager

    fake = FakeSocketManager()
    monkeypatch.setattr(manager, "socket_manager", fake)
    return fake


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture
def lifespan_client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c
