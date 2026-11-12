"""Room documents in the live state backend (spec 13.3)."""

import json
import random
import secrets
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import UTC, datetime
from typing import cast

from app.core.abuse import LOBBY_ROOM_TTL_SECONDS, MAX_LIVE_ROOMS
from app.core.limits import LOCK_TIMEOUT_SECONDS, ROOM_TTL_SECONDS
from app.services import state_backend
from app.services.state_backend import StateBackend

SCHEMA_VERSION: int = 1
ROOM_CODE_ALPHABET: str = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH: int = 6
_MAX_CODE_TRIES = 10


def generate_room_code(rng: random.Random | None = None) -> str:
    """A fresh room code; SystemRandom when ``rng`` is None."""
    source = rng if rng is not None else random.SystemRandom()
    return "".join(source.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))


def is_room_code(code: object) -> bool:
    """True for a string that can be a room code, in any case. Every client-supplied
    code passes here before a key or a lock is built from it."""
    if not isinstance(code, str) or len(code) != ROOM_CODE_LENGTH:
        return False
    upper = code.upper()
    return len(upper) == ROOM_CODE_LENGTH and all(
        c in ROOM_CODE_ALPHABET for c in upper
    )


def new_host_secret() -> str:
    return secrets.token_urlsafe(32)


def new_room_document(
    *,
    room_code: str,
    config: dict,
    host_display_name: str,
    host_secret: str,
    created_at: str,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "room_code": room_code,
        "state": "lobby",
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "host_secret": host_secret,
        "host_sid": None,
        "host_identity": None,
        "host_display_name": host_display_name,
        "participants": {},
        "sid_to_alias": {},
        "seed": None,
        "config": config,
        "engine": None,
        "seq": 0,
        "persisted": False,
        "game_id": None,
    }


def next_free_alias(room: dict) -> str:
    """The lowest unused ``P<n>``, n >= 1; never ``len + 1`` (spec 11.3)."""
    taken = room.get("participants", {})
    n = 1
    while f"P{n}" in taken:
        n += 1
    return f"P{n}"


def _room_key(room_code: str) -> str:
    return f"room:{room_code.upper()}"


def _lock_key(room_code: str) -> str:
    return f"lock:room:{room_code.upper()}"


def _encode(room: dict) -> str:
    return json.dumps(room, separators=(",", ":"))


class RoomLimitReached(Exception):
    """This process already holds ``MAX_LIVE_ROOMS`` live rooms."""


class StateService:
    def __init__(
        self,
        backend: StateBackend,
        *,
        ttl_seconds: int = ROOM_TTL_SECONDS,
        lobby_ttl_seconds: int = LOBBY_ROOM_TTL_SECONDS,
        lock_timeout: int = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._backend = backend
        self._ttl = ttl_seconds
        self._lobby_ttl = min(lobby_ttl_seconds, ttl_seconds)
        self._lock_timeout = lock_timeout

    def _ttl_for(self, room: dict) -> int:
        """A room that has not started expires sooner (abuse limits, spec 0.6)."""
        if room.get("state") in ("lobby", "configuring"):
            return self._lobby_ttl
        return self._ttl

    async def create_room(self, config: dict, host_display_name: str) -> dict:
        """A new room under a code no live room holds. The write is set-if-absent, so
        two creations that draw the same code at once cannot both take it. In process
        memory, past ``MAX_LIVE_ROOMS`` live rooms it raises ``RoomLimitReached``."""
        backend = self._backend
        if (
            isinstance(backend, state_backend.InMemoryBackend)
            and backend.count_live() >= MAX_LIVE_ROOMS
        ):
            raise RoomLimitReached
        host_secret = new_host_secret()
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _ in range(_MAX_CODE_TRIES):
            code = generate_room_code()
            room = new_room_document(
                room_code=code,
                config=config,
                host_display_name=host_display_name,
                host_secret=host_secret,
                created_at=created_at,
            )
            if await self._backend.set(
                _room_key(code), _encode(room), ex=self._ttl_for(room), nx=True
            ):
                return room
        raise RuntimeError("Could not find a free room code.")

    async def get_room(self, room_code: str) -> dict | None:
        if not is_room_code(room_code):
            return None
        key = _room_key(room_code)
        raw = await self._backend.get(key)
        if raw is None:
            return None
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            room = json.loads(text)
        except ValueError:
            room = None
        if (
            not isinstance(room, dict)
            or room.get("schema_version") != SCHEMA_VERSION
            or "sid_to_alias" not in room
            or "host_secret" not in room
        ):
            await self._backend.delete(key)
            return None
        return room

    async def save_room(self, room_code: str, room: dict) -> None:
        if room.get("room_code") != room_code.upper():
            raise ValueError("room_code does not match the room document.")
        await self._backend.setex(
            _room_key(room_code), self._ttl_for(room), _encode(room)
        )

    async def delete_room(self, room_code: str) -> None:
        await self._backend.delete(_room_key(room_code))

    async def room_exists(self, room_code: str) -> bool:
        if not is_room_code(room_code):
            return False
        return bool(await self._backend.exists(_room_key(room_code)))

    def lock(self, room_code: str) -> AbstractAsyncContextManager[object]:
        """The room's lock; a malformed code (no room can hold it) takes no lock."""
        if not is_room_code(room_code):
            return nullcontext()
        return cast(
            AbstractAsyncContextManager[object],
            self._backend.lock(_lock_key(room_code), timeout=self._lock_timeout),
        )

    def next_seq(self, room: dict) -> int:
        room["seq"] += 1
        return int(room["seq"])


_service: StateService | None = None


def get_state_service() -> StateService:
    global _service
    if _service is None:
        _service = StateService(state_backend.get_state_backend())
    return _service


def reset_state_service() -> None:
    """Tests only: drop the singleton."""
    global _service
    _service = None
