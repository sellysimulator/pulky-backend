"""Startup checks (spec 13.2, 13.3): AC5, AC6, AC7, failure mode 7."""

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core import firebase
from app.core.checks import register_check, run_startup_checks
from app.services.state_backend import InMemoryBackend, build_state_backend
from tests.conftest import FakeRedis, FirebaseTokens
from tests.test_skeleton.helpers import patch_settings

CRITICAL_TEXT = (
    "FIREBASE_SERVICE_ACCOUNT_JSON is not set — signed-in users will be REJECTED "
    "at the Socket.IO handshake and /users/* will 503."
)
ACTIVE_TEXT = "Firebase token verification is active."


def _records(
    caplog: pytest.LogCaptureFixture, level: int, needle: str
) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == level and needle.lower() in r.getMessage().lower()
    ]


def test_unconfigured_auth_logs_the_critical_message_once(
    lifespan_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    run_startup_checks()
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert [r.getMessage() for r in critical] == [CRITICAL_TEXT]


def test_lifespan_runs_the_auth_check_exactly_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.main import app

    caplog.set_level(logging.DEBUG)
    with TestClient(app):
        pass
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert [r.getMessage() for r in critical] == [CRITICAL_TEXT]


def test_configured_auth_logs_active_and_no_critical(
    caplog: pytest.LogCaptureFixture, firebase_tokens: FirebaseTokens
) -> None:
    from app.main import app

    caplog.set_level(logging.DEBUG)
    with TestClient(app):
        pass
    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert _records(caplog, logging.INFO, ACTIVE_TEXT)


def test_lifespan_client_observes_the_auth_check_but_plain_client_does_not(
    caplog: pytest.LogCaptureFixture, client: TestClient
) -> None:
    """Failure mode 7: the bare client skips startup."""
    caplog.set_level(logging.DEBUG)
    client.get("/api/v1/health")
    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]


def test_memory_backend_warns_about_single_instance(
    caplog: pytest.LogCaptureFixture, lifespan_client: TestClient
) -> None:
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    run_startup_checks()
    assert _records(caplog, logging.INFO, "memory")
    warnings = _records(caplog, logging.WARNING, "single")
    assert warnings
    assert any("restart" in r.getMessage().lower() for r in warnings)


def test_redis_backend_names_redis_and_has_no_single_instance_warning(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
) -> None:
    patch_settings(monkeypatch, REDIS_ENABLED=True, REDIS_URL="redis://fake/0")
    caplog.set_level(logging.DEBUG)
    run_startup_checks()
    assert _records(caplog, logging.INFO, "redis")
    assert not _records(caplog, logging.WARNING, "single")


def test_redis_enabled_with_empty_url_names_both_settings() -> None:
    with pytest.raises(ValueError) as error:
        build_state_backend(True, "")
    assert "REDIS_ENABLED" in str(error.value)
    assert "REDIS_URL" in str(error.value)


def test_build_state_backend_returns_the_right_kind() -> None:
    assert isinstance(build_state_backend(False, ""), InMemoryBackend)
    redis_backend = build_state_backend(True, "redis://localhost:6379/0")
    assert not isinstance(redis_backend, InMemoryBackend)
    assert callable(redis_backend.get) and callable(redis_backend.lock)


def test_db_tls_check_warns_only_when_ssl_is_off(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    patch_settings(monkeypatch, DB_REQUIRE_SSL=True)
    run_startup_checks()
    assert not _records(caplog, logging.WARNING, "ssl") + _records(
        caplog, logging.WARNING, "tls"
    )
    caplog.clear()
    patch_settings(monkeypatch, DB_REQUIRE_SSL=False)
    run_startup_checks()
    assert _records(caplog, logging.WARNING, "ssl") or _records(
        caplog, logging.WARNING, "tls"
    )


@pytest.fixture
def live_flag() -> Iterator[dict[str, bool]]:
    flag = {"on": True}
    yield flag
    flag["on"] = False  # registered checks outlive the test; make them inert


def test_a_raising_check_is_logged_and_the_rest_still_run(
    caplog: pytest.LogCaptureFixture, live_flag: dict[str, bool]
) -> None:
    ran: list[str] = []

    @register_check
    def a_before() -> None:
        if live_flag["on"]:
            ran.append("before")

    @register_check
    def a_boom() -> None:
        if live_flag["on"]:
            raise RuntimeError("check exploded")

    @register_check
    def a_after() -> None:
        if live_flag["on"]:
            ran.append("after")

    caplog.set_level(logging.DEBUG)
    run_startup_checks()  # must not raise
    assert ran == ["before", "after"]
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_the_builtin_checks_still_run_after_one_raises(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("firebase init exploded")

    monkeypatch.setattr(firebase, "init_firebase", boom)
    caplog.set_level(logging.DEBUG)
    run_startup_checks()
    assert _records(caplog, logging.INFO, "memory")
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_register_check_is_idempotent_and_returns_the_function(
    live_flag: dict[str, bool],
) -> None:
    calls: list[int] = []

    def once() -> None:
        if live_flag["on"]:
            calls.append(1)

    assert register_check(once) is once
    register_check(once)
    run_startup_checks()
    assert calls == [1]
