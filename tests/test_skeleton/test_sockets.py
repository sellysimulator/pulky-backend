"""Socket manager, @guarded and the Socket.IO server (AC11, AC12, FM3-5)."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.sockets import manager
from app.sockets.errors import SERVER_ERROR_MESSAGE, guarded
from app.sockets.manager import SocketManager, get_socket_manager
from tests.conftest import FakeSocketManager

ROOT = Path(__file__).resolve().parents[2]
SECRET_ERROR = "mysql://root:secret@db"


def _run(code: str, **env: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), **env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    return result.stdout.strip()


def test_server_error_message_is_the_spec_text() -> None:
    assert SERVER_ERROR_MESSAGE == "Something went wrong. Please try again."


@pytest.mark.asyncio
async def test_guarded_emits_server_error_to_the_calling_sid(
    fake_socket_manager: FakeSocketManager,
) -> None:
    @guarded()
    async def handler(sid: str, data: dict | None = None) -> None:
        raise RuntimeError(SECRET_ERROR)

    await handler("sid-1", {"x": 1})  # never re-raised
    records = fake_socket_manager.emits("error")
    assert len(records) == 1
    target, event, data, _ = records[0]
    assert target == "sid-1" and event == "error"
    assert data == {"code": "SERVER_ERROR", "message": SERVER_ERROR_MESSAGE}
    assert len(fake_socket_manager.records) == 1


@pytest.mark.asyncio
async def test_guarded_message_never_leaks_exception_text(
    fake_socket_manager: FakeSocketManager,
) -> None:
    """Failure mode 4."""

    @guarded()
    async def handler(sid: str) -> None:
        raise RuntimeError(SECRET_ERROR)

    await handler("sid-1")
    message = fake_socket_manager.emits("error")[0][2]["message"]
    assert "secret" not in message and "mysql" not in message


@pytest.mark.asyncio
async def test_guarded_logs_the_traceback_at_error(
    fake_socket_manager: FakeSocketManager, caplog: pytest.LogCaptureFixture
) -> None:
    """Failure mode 5."""

    @guarded()
    async def handler(sid: str) -> None:
        raise RuntimeError(SECRET_ERROR)

    caplog.set_level(logging.DEBUG)
    await handler("sid-1")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in errors)


@pytest.mark.asyncio
async def test_guarded_join_error_variant(
    fake_socket_manager: FakeSocketManager,
) -> None:
    @guarded("join_error")
    async def handler(sid: str, data: dict) -> None:
        raise RuntimeError(SECRET_ERROR)

    await handler("sid-9", {})
    assert fake_socket_manager.emits("error") == []
    records = fake_socket_manager.emits("join_error")
    assert len(records) == 1
    assert records[0][0] == "sid-9"
    assert records[0][2] == {"message": SERVER_ERROR_MESSAGE}


@pytest.mark.asyncio
async def test_guarded_passes_through_a_normal_handler(
    fake_socket_manager: FakeSocketManager,
) -> None:
    @guarded()
    async def handler(sid: str, data: dict) -> str:
        return f"{sid}:{data['k']}"

    assert await handler("s", {"k": "v"}) == "s:v"
    assert fake_socket_manager.records == []


def test_logger_follows_debug_false() -> None:
    """Failure mode 3 / AC12: no hardcoded engine.io payload logging."""
    out = _run(
        "import logging, app.sockets.manager as m;"
        "print(logging.getLogger('socketio.server').level,"
        " logging.getLogger('engineio.server').level)",
        DEBUG="false",
    )
    socketio_level, engineio_level = (int(x) for x in out.split())
    assert socketio_level >= logging.ERROR
    assert engineio_level >= logging.ERROR


def test_logger_follows_debug_true() -> None:
    out = _run(
        "import logging, app.sockets.manager as m;"
        "print(logging.getLogger('socketio.server').level,"
        " logging.getLogger('engineio.server').level)",
        DEBUG="true",
    )
    socketio_level, engineio_level = (int(x) for x in out.split())
    assert socketio_level <= logging.INFO
    assert engineio_level <= logging.INFO


def test_redis_manager_only_when_redis_is_enabled() -> None:
    code = "from app.sockets.manager import sio; print(type(sio.manager).__name__)"
    assert _run(code, REDIS_ENABLED="false") != "AsyncRedisManager"
    assert (
        _run(code, REDIS_ENABLED="true", REDIS_URL="redis://localhost:6379/0")
        == "AsyncRedisManager"
    )


def test_sio_allows_the_configured_cors_origins() -> None:
    out = _run(
        "from app.sockets.manager import sio;"
        "print(sorted(sio.eio.cors_allowed_origins))",
        CORS_ORIGINS='["https://a.example"]',
    )
    assert out == "['https://a.example']"


def test_get_socket_manager_returns_the_module_instance() -> None:
    assert get_socket_manager() is manager.socket_manager
    assert isinstance(manager.socket_manager, SocketManager)


def test_fake_socket_manager_replaces_the_module_instance(
    fake_socket_manager: FakeSocketManager,
) -> None:
    assert get_socket_manager() is fake_socket_manager


class _FakeSio:
    def __init__(self) -> None:
        self.entered: list[tuple[str, str]] = []
        self.left: list[tuple[str, str]] = []
        self.emitted: list[tuple[str, Any, Any, Any]] = []

    async def enter_room(self, sid: str, room: str, *args: Any, **kw: Any) -> None:
        self.entered.append((sid, room))

    async def leave_room(self, sid: str, room: str, *args: Any, **kw: Any) -> None:
        self.left.append((sid, room))

    async def emit(self, event: str, data: Any = None, *args: Any, **kw: Any) -> None:
        target = kw.get("to") or kw.get("room") or (args[0] if args else None)
        self.emitted.append((event, data, target, kw.get("skip_sid")))


@pytest.fixture
def fake_sio(monkeypatch: pytest.MonkeyPatch) -> _FakeSio:
    fake = _FakeSio()
    monkeypatch.setattr(manager, "sio", fake)
    return fake


@pytest.mark.asyncio
async def test_join_room_enters_and_caches_the_alias(fake_sio: _FakeSio) -> None:
    mgr = SocketManager()
    await mgr.join_room("sid1", "ABC123", "P1")
    assert fake_sio.entered == [("sid1", "ABC123")]
    assert mgr.sid_to_alias["sid1"] == ("ABC123", "P1")


@pytest.mark.asyncio
async def test_enter_room_caches_the_host_room(fake_sio: _FakeSio) -> None:
    mgr = SocketManager()
    await mgr.enter_room("host1", "ABC123")
    assert fake_sio.entered == [("host1", "ABC123")]
    assert mgr.sid_to_host_room["host1"] == "ABC123"
    assert "host1" not in mgr.sid_to_alias


@pytest.mark.asyncio
async def test_leave_room_drops_the_cache_entries(fake_sio: _FakeSio) -> None:
    mgr = SocketManager()
    await mgr.join_room("sid1", "ABC123", "P1")
    await mgr.enter_room("host1", "ABC123")
    await mgr.leave_room("sid1", "ABC123")
    await mgr.leave_room("host1", "ABC123")
    assert fake_sio.left == [("sid1", "ABC123"), ("host1", "ABC123")]
    assert "sid1" not in mgr.sid_to_alias
    assert "host1" not in mgr.sid_to_host_room


@pytest.mark.asyncio
async def test_forget_drops_every_map_entry(fake_sio: _FakeSio) -> None:
    mgr = SocketManager()
    mgr.sid_to_identity["sid1"] = "guest_x"
    await mgr.join_room("sid1", "ABC123", "P1")
    mgr.sid_to_host_room["sid1"] = "ABC123"
    mgr.forget("sid1")
    assert "sid1" not in mgr.sid_to_identity
    assert "sid1" not in mgr.sid_to_alias
    assert "sid1" not in mgr.sid_to_host_room
    mgr.forget("never-seen")  # idempotent


@pytest.mark.asyncio
async def test_emit_helpers_forward_event_data_target_and_skip(
    fake_sio: _FakeSio,
) -> None:
    mgr = SocketManager()
    await mgr.emit_to_room("ABC123", "day_closed", {"seq": 1}, skip_sid="sid1")
    await mgr.emit_to_sid("sid2", "error", {"code": "X"})
    assert fake_sio.emitted == [
        ("day_closed", {"seq": 1}, "ABC123", "sid1"),
        ("error", {"code": "X"}, "sid2", None),
    ]
