"""State backends (spec 13.3) and the two backend fixtures."""

import asyncio
from typing import Any

import pytest

from app.services import state_backend
from app.services.state_backend import InMemoryBackend, get_state_backend
from tests.conftest import FakeRedis
from tests.test_skeleton.helpers import patch_settings


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_set_get_exists_delete() -> None:
    backend = InMemoryBackend()
    assert await backend.get("k") is None
    assert await backend.exists("k") == 0
    await backend.setex("k", 60, "v")
    assert await backend.get("k") == "v"
    assert await backend.exists("k") == 1
    await backend.setex("j", 60, "w")
    assert await backend.delete("k", "j", "missing") == 2
    assert await backend.get("k") is None


@pytest.mark.asyncio
async def test_expiry_reads_only_the_injected_clock() -> None:
    clock = Clock()
    backend = InMemoryBackend(clock=clock)
    await backend.setex("k", 10, "v")
    clock.now += 9
    assert await backend.get("k") == "v"
    clock.now += 1
    assert await backend.get("k") is None
    assert await backend.exists("k") == 0


@pytest.mark.asyncio
async def test_ttl_zero_never_expires() -> None:
    clock = Clock()
    backend = InMemoryBackend(clock=clock)
    await backend.setex("k", 0, "v")
    clock.now += 10**9
    assert await backend.get("k") == "v"


@pytest.mark.asyncio
async def test_purge_expired_counts_the_dropped_entries() -> None:
    clock = Clock()
    backend = InMemoryBackend(clock=clock)
    await backend.setex("a", 5, "1")
    await backend.setex("b", 5, "2")
    await backend.setex("c", 50, "3")
    clock.now += 10
    assert backend.purge_expired() == 2
    assert await backend.get("c") == "3"


async def _exclusive(backend: Any) -> list[str]:
    order: list[str] = []

    async def worker(name: str) -> None:
        async with backend.lock("room:ABC123", timeout=5):
            order.append(f"{name}-in")
            await asyncio.sleep(0.01)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"))
    return order


@pytest.mark.asyncio
async def test_lock_is_exclusive_per_key() -> None:
    order = await _exclusive(InMemoryBackend())
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


@pytest.mark.asyncio
async def test_locks_on_different_keys_do_not_block_each_other() -> None:
    backend = InMemoryBackend()
    async with backend.lock("one"):
        async with asyncio.timeout(1):
            async with backend.lock("two"):
                pass


@pytest.mark.asyncio
async def test_memory_backend_fixture_moves_time(memory_backend: Any) -> None:
    assert state_backend.get_state_backend() is memory_backend
    await memory_backend.setex("k", 30, "v")
    memory_backend.advance(29)
    assert await memory_backend.get("k") == "v"
    memory_backend.advance(2)
    assert await memory_backend.get("k") is None


@pytest.mark.asyncio
async def test_fake_redis_fixture_ttl_and_per_key_locks(fake_redis: FakeRedis) -> None:
    assert state_backend.get_state_backend() is fake_redis
    await fake_redis.setex("k", 30, "v")
    assert await fake_redis.exists("k") == 1
    fake_redis.advance(31)
    assert await fake_redis.get("k") is None
    assert await fake_redis.exists("k") == 0
    order = await _exclusive(fake_redis)
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )
    async with fake_redis.lock("x"):
        async with asyncio.timeout(1):
            async with fake_redis.lock("y"):
                pass


def test_get_state_backend_is_a_process_singleton() -> None:
    state_backend.reset_state_backend()
    try:
        first = get_state_backend()
        assert isinstance(first, InMemoryBackend)
        assert get_state_backend() is first
        state_backend.reset_state_backend()
        assert get_state_backend() is not first
    finally:
        state_backend.reset_state_backend()


def test_get_state_backend_follows_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    state_backend.reset_state_backend()
    try:
        patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="")
        with pytest.raises(ValueError):
            get_state_backend()
        patch_settings(
            monkeypatch, REDIS_ENABLED=True, REDIS_URL="redis://localhost:6379/0"
        )
        backend = get_state_backend()
        assert not isinstance(backend, InMemoryBackend)
    finally:
        state_backend.reset_state_backend()
