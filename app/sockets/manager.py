"""The Socket.IO server and the process-local socket manager (spec 13.2, 13.3)."""

from typing import Any

import socketio

from app.config import settings

sio = socketio.AsyncServer(
    async_mode="asgi",
    client_manager=(
        socketio.AsyncRedisManager(settings.REDIS_URL)
        if settings.REDIS_ENABLED
        else None
    ),
    cors_allowed_origins=settings.CORS_ORIGINS,
    logger=settings.DEBUG,
    engineio_logger=settings.DEBUG,
)


class SocketManager:
    """Emits, room membership and the in-process caches of one worker's sockets."""

    def __init__(self) -> None:
        self.sid_to_identity: dict[str, str] = {}
        self.sid_to_alias: dict[str, tuple[str, str]] = {}
        self.sid_to_host_room: dict[str, str] = {}

    async def emit_to_room(
        self,
        room_code: str,
        event: str,
        data: dict[str, Any],
        skip_sid: str | None = None,
    ) -> None:
        await sio.emit(event, data, room=room_code, skip_sid=skip_sid)

    async def emit_to_sid(self, sid: str, event: str, data: dict[str, Any]) -> None:
        await sio.emit(event, data, to=sid)

    async def join_room(self, sid: str, room_code: str, alias: str) -> None:
        await sio.enter_room(sid, room_code)
        self.sid_to_alias[sid] = (room_code, alias)

    async def enter_room(self, sid: str, room_code: str) -> None:
        await sio.enter_room(sid, room_code)
        self.sid_to_host_room[sid] = room_code

    async def leave_room(self, sid: str, room_code: str) -> None:
        await sio.leave_room(sid, room_code)
        self.sid_to_alias.pop(sid, None)
        self.sid_to_host_room.pop(sid, None)

    def forget(self, sid: str) -> None:
        self.sid_to_identity.pop(sid, None)
        self.sid_to_alias.pop(sid, None)
        self.sid_to_host_room.pop(sid, None)


socket_manager = SocketManager()


def get_socket_manager() -> SocketManager:
    """The module's current manager; call at use time so tests can replace it."""
    return socket_manager
