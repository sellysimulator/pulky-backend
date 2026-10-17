"""Live-state backends (spec 13.3): in-memory and ``redis.asyncio``."""

import asyncio
import time
from collections.abc import Callable
from typing import Protocol, Self, cast

from app.config import get_settings


class StateBackend(Protocol):
    """The operations the state service uses. A subset of redis.asyncio.Redis, so the
    real client satisfies it without an adapter."""

    async def get(self, key: str) -> str | bytes | None: ...

    async def setex(self, key: str, ttl: int, value: str) -> object: ...

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> object:
        """With ``nx``, a truthy result only when the key was absent and is now set."""
        ...

    async def delete(self, *keys: str) -> int: ...

    async def exists(self, key: str) -> int: ...

    def lock(self, key: str, timeout: int = 10) -> object: ...


class _MemoryLock:
    """An asyncio.Lock with redis.asyncio.Lock's context-manager shape. `timeout` is
    accepted and ignored: within one event loop an owner cannot die mid-operation
    without releasing, so every `async with state_svc.lock(...)` call site stays
    identical across both backends."""

    def __init__(self, lock: asyncio.Lock, timeout: int) -> None:
        self._lock = lock
        self.timeout = timeout

    async def __aenter__(self) -> Self:
        await self._lock.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


class InMemoryBackend:
    """Room documents in this process's memory, expired lazily on read (no sweeper
    task). It stores JSON strings and never live engine objects."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._store: dict[str, tuple[float | None, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _live(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and expires_at <= self._clock():
            del self._store[key]
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self._store[key] = (self._clock() + ttl if ttl else None, value)
        return True

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> bool | None:
        if nx and self._live(key) is not None:
            return None
        return await self.setex(key, ex, value)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._store.pop(key, None) is not None:
                removed += 1
            self._locks.pop(key, None)
        return removed

    async def exists(self, key: str) -> int:
        return 1 if self._live(key) is not None else 0

    def lock(self, key: str, timeout: int = 10) -> _MemoryLock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return _MemoryLock(lock, timeout)

    def purge_expired(self) -> int:
        """Drop every expired entry; return how many went."""
        now = self._clock()
        dead = [
            k for k, (exp, _) in self._store.items() if exp is not None and exp <= now
        ]
        for key in dead:
            del self._store[key]
            self._locks.pop(key, None)
        return len(dead)


def build_state_backend(enabled: bool, url: str) -> StateBackend:
    if not enabled:
        return InMemoryBackend()
    if not url:
        raise ValueError(
            "REDIS_ENABLED is true but REDIS_URL is empty. Set REDIS_URL, or "
            "set REDIS_ENABLED=false to keep room state in this process."
        )
    import redis.asyncio as aioredis

    return cast(StateBackend, aioredis.from_url(url))


_backend: StateBackend | None = None


def get_state_backend() -> StateBackend:
    """The process singleton, built from ``REDIS_ENABLED`` / ``REDIS_URL``."""
    global _backend
    if _backend is None:
        current = get_settings()
        _backend = build_state_backend(current.REDIS_ENABLED, current.REDIS_URL)
    return _backend


def reset_state_backend() -> None:
    """Tests only: drop the singleton."""
    global _backend
    _backend = None
