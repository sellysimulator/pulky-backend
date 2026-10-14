"""Health routes (spec 13.4): AC3, AC4, failure mode 1."""

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.db import session as db_session
from app.services import state_backend
from tests.test_skeleton.helpers import patch_settings

DEEP_KEYS = {"status", "database", "redis", "state_backend"}


def _db_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise RuntimeError("database is down")

    monkeypatch.setattr(db_session, "get_engine", boom)


def _db_up(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(db_session, "get_engine", lambda: engine)


class _BrokenBackend:
    async def get(self, key: str) -> None:
        raise ConnectionError("redis down")

    async def setex(self, key: str, ttl: int, value: str) -> None:
        raise ConnectionError("redis down")

    async def delete(self, *keys: str) -> None:
        raise ConnectionError("redis down")

    async def exists(self, key: str) -> None:
        raise ConnectionError("redis down")

    async def ping(self) -> None:
        raise ConnectionError("redis down")


class _HangingBackend(_BrokenBackend):
    async def exists(self, key: str) -> None:
        await asyncio.sleep(30)

    async def ping(self) -> None:
        await asyncio.sleep(30)

    async def get(self, key: str) -> None:
        await asyncio.sleep(30)


def test_health_is_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_answers_ok_with_database_and_redis_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3 and failure mode 1: the wake-up probe touches no dependency."""
    _db_down(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="redis://127.0.0.1:1/0")
    monkeypatch.setattr(state_backend, "get_state_backend", lambda: _BrokenBackend())
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_deep_worked_example_database_down_redis_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db_down(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=False)
    assert client.get("/api/v1/health/deep").json() == {
        "status": "degraded",
        "database": "error",
        "redis": "disabled",
        "state_backend": "memory",
    }


def test_deep_database_up_redis_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db_up(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=False)
    response = client.get("/api/v1/health/deep")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "redis": "disabled",
        "state_backend": "memory",
    }


def test_deep_database_up_redis_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_redis: Any
) -> None:
    _db_up(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="redis://fake/0")
    assert client.get("/api/v1/health/deep").json() == {
        "status": "ok",
        "database": "ok",
        "redis": "ok",
        "state_backend": "redis",
    }


def test_deep_database_up_redis_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db_up(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="redis://fake/0")
    monkeypatch.setattr(state_backend, "get_state_backend", lambda: _BrokenBackend())
    assert client.get("/api/v1/health/deep").json() == {
        "status": "degraded",
        "database": "ok",
        "redis": "error",
        "state_backend": "redis",
    }


def test_deep_database_down_redis_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db_down(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="redis://fake/0")
    monkeypatch.setattr(state_backend, "get_state_backend", lambda: _BrokenBackend())
    body = client.get("/api/v1/health/deep").json()
    assert set(body) == DEEP_KEYS
    assert body["status"] == "degraded"
    assert body["database"] == "error"
    assert body["redis"] == "error"


def test_deep_database_down_redis_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_redis: Any
) -> None:
    _db_down(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="redis://fake/0")
    body = client.get("/api/v1/health/deep").json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"
    assert body["redis"] == "ok"


def test_deep_reports_redis_error_when_enabled_without_a_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's failure is reported, never raised."""
    _db_up(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="")
    state_backend.reset_state_backend()
    try:
        response = client.get("/api/v1/health/deep")
    finally:
        state_backend.reset_state_backend()
    assert response.status_code == 200
    assert response.json()["redis"] == "error"
    assert response.json()["status"] == "degraded"


def test_deep_probe_times_out_instead_of_hanging(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db_up(monkeypatch)
    patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="redis://fake/0")
    monkeypatch.setattr(state_backend, "get_state_backend", lambda: _HangingBackend())
    started = time.monotonic()
    body = client.get("/api/v1/health/deep").json()
    assert time.monotonic() - started < 6
    assert body["redis"] == "error"
    assert body["status"] == "degraded"
