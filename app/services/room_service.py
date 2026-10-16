"""Room rules shared by REST and sockets (spec 11.3, 12.2, 9.2)."""

import hmac
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core import config_models
from app.core.config_models import GameConfig
from app.core.enums import RoomState
from app.core.limits import MAX_DISPLAY_NAME, MIN_PLAYERS_TO_START
from app.services.state_service import ROOM_CODE_ALPHABET, ROOM_CODE_LENGTH
from app.sockets import manager


@dataclass(frozen=True)
class Emit:
    target: str  # a room code (room emit) or a sid
    event: str
    data: dict
    skip_sid: str | None = None  # room emits only


class ConfigLocked(Exception):
    """The config cannot change once the game has started."""

    def __init__(self) -> None:
        super().__init__(config_models.MSG_CONFIG_LOCKED)


STATUS_NOT_FOUND = "No room with that code."
STATUS_STARTED = "This game has already started."
STATUS_FINISHED = "This game has finished."
STATUS_FULL = "This room is full."
START_NEED_PLAYERS = f"Waiting for at least {MIN_PLAYERS_TO_START} players."
START_ALREADY = "The game has already started."

_ROOM_CODE_RE = re.compile(f"[{ROOM_CODE_ALPHABET}]{{{ROOM_CODE_LENGTH}}}")


def check_host_secret(room: dict, data: Mapping[str, Any] | None) -> bool:
    expected = str(room.get("host_secret") or "")
    # bool(expected) is load-bearing: compare_digest("", "") is True, so without it
    # a payload with no host_secret authorises against a room with an empty secret.
    return bool(expected) and hmac.compare_digest(
        expected.encode("utf-8"),
        str((data or {}).get("host_secret") or "").encode("utf-8"),
    )


def check_host_identity(room: dict, identity: str | None) -> bool:
    stored = room.get("host_identity")
    return bool(identity) and bool(stored) and identity == stored


def find_alias_for_reconnect(
    room: dict, session_token: str | None, identity: str | None
) -> str | None:
    if not session_token or not identity:
        return None
    for alias, participant in room.get("participants", {}).items():
        if (
            participant.get("session_token") == session_token
            and participant.get("identity") == identity
        ):
            return str(alias)
    return None


def find_alias_for_identity(room: dict, identity: str | None) -> str | None:
    if not identity:
        return None
    for alias, participant in room.get("participants", {}).items():
        if participant.get("identity") == identity:
            return str(alias)
    return None


def sanitise_display_name(raw: object, fallback: str) -> str:
    if not isinstance(raw, str):
        return fallback
    stripped = "".join(c for c in raw if not unicodedata.category(c).startswith("C"))
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return collapsed[:MAX_DISPLAY_NAME] or fallback


def seats_taken(room: dict) -> int:
    return len(room.get("participants", {}))


def _seats_total(room: dict) -> int:
    return int(room["config"]["max_players"])


def can_start(room: dict) -> tuple[bool, str | None]:
    if room.get("state") in (RoomState.RUNNING, RoomState.FINISHED):
        return False, START_ALREADY
    if seats_taken(room) < MIN_PLAYERS_TO_START:
        return False, START_NEED_PLAYERS
    return True, None


def _alias_number(alias: str) -> int:
    digits = alias[1:]
    return int(digits) if digits.isdigit() else 0


def lobby_payload(room: dict, seq: int) -> dict:
    ok, reason = can_start(room)
    participants = sorted(
        room.get("participants", {}).values(), key=lambda p: _alias_number(p["alias"])
    )
    return {
        "seq": seq,
        "state": room["state"],
        "participants": [
            {
                "alias": p["alias"],
                "display_name": p["display_name"],
                "connected": bool(p["connected"]),
            }
            for p in participants
        ],
        "seats_total": _seats_total(room),
        "config_locked": config_locked(room),
        "can_start": ok,
        "start_blocked_reason": reason,
    }


def room_status(room: dict | None) -> dict:
    if room is None:
        return {
            "ok": False,
            "reason": STATUS_NOT_FOUND,
            "state": None,
            "host_display_name": None,
            "seats_taken": 0,
            "seats_total": 0,
        }
    taken, total = seats_taken(room), _seats_total(room)
    status: dict[str, Any] = {
        "ok": False,
        "state": room["state"],
        "host_display_name": room.get("host_display_name"),
        "seats_taken": taken,
        "seats_total": total,
    }
    if room["state"] == RoomState.RUNNING:
        status["reason"] = STATUS_STARTED
    elif room["state"] == RoomState.FINISHED:
        status["reason"] = STATUS_FINISHED
    elif taken >= total:
        status["reason"] = STATUS_FULL
    else:
        status["ok"] = True
    return status


def config_locked(room: dict) -> bool:
    return room.get("state") in (RoomState.RUNNING, RoomState.FINISHED)


def apply_config_update(
    room: dict, patch: dict, next_seq: Callable[[dict], int]
) -> list[Emit]:
    """Merge and validate ``patch``; mutates ``room`` only on success."""
    if config_locked(room):
        raise ConfigLocked()
    config = GameConfig.from_host_input(patch, GameConfig.from_dict(room["config"]))
    room["config"] = config.to_dict()
    if room["state"] == RoomState.LOBBY:
        room["state"] = RoomState.CONFIGURING.value
    config_seq = next_seq(room)
    lobby_seq = next_seq(room)
    host_sid = room.get("host_sid")
    emits: list[Emit] = []
    if host_sid:
        emits.append(
            Emit(
                host_sid,
                "config_updated",
                {"seq": config_seq, "config": room["config"]},
            )
        )
    emits.append(
        Emit(
            room["room_code"],
            "config_updated",
            {"seq": config_seq, "config": config_models.config_public(config)},
            skip_sid=host_sid or None,
        )
    )
    emits.append(
        Emit(room["room_code"], "lobby_update", lobby_payload(room, lobby_seq))
    )
    return emits


async def send(emits: Sequence[Emit]) -> None:
    sockets = manager.get_socket_manager()
    for emit in emits:
        if emit.skip_sid is not None or _ROOM_CODE_RE.fullmatch(emit.target):
            await sockets.emit_to_room(
                emit.target, emit.event, emit.data, skip_sid=emit.skip_sid
            )
        else:
            await sockets.emit_to_sid(emit.target, emit.event, emit.data)
