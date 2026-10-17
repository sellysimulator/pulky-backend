"""Engine-backed payloads and the start/resync emit builders (spec 12.6, 11.3)."""

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from app.core.config_models import GameConfig, config_public
from app.core.engine import PulkyEngine
from app.core.enums import RoomState
from app.services.room_service import Emit

logger = logging.getLogger(__name__)


def load_engine(room: dict) -> PulkyEngine | None:
    payload = room.get("engine")
    if not payload:
        return None
    return PulkyEngine.from_payload(payload, GameConfig.from_dict(room["config"]))


def store_engine(room: dict, engine: PulkyEngine) -> None:
    room["engine"] = engine.to_payload()


def _alias_number(alias: str) -> int:
    digits = alias[1:]
    return int(digits) if digits.isdigit() else 0


def _ordered_participants(room: dict) -> list[dict]:
    return sorted(
        room.get("participants", {}).values(), key=lambda p: _alias_number(p["alias"])
    )


def display_names(room: dict) -> dict[str, str]:
    return {p["alias"]: p["display_name"] for p in _ordered_participants(room)}


def _require_engine(room: dict) -> PulkyEngine:
    engine = load_engine(room)
    if engine is None:
        raise ValueError("The room has no engine.")
    return engine


def player_state(room: dict, alias: str, seq: int, full: bool) -> dict:
    engine = _require_engine(room)
    return {"seq": seq, "full": full, **engine.player_view(alias, full)}


def host_state(room: dict, seq: int, full: bool) -> dict:
    engine = _require_engine(room)
    view = engine.host_view(full)
    for alias, entry in view["players"].items():
        participant = room["participants"].get(alias)
        entry["connected"] = bool(participant and participant["connected"])
    return {"seq": seq, "full": full, **view, "state": room["state"]}


def _game_started(room: dict, seq: int) -> dict:
    config = GameConfig.from_dict(room["config"])
    return {
        "seq": seq,
        "duration_days": config.duration_days,
        "participants": [
            {
                "alias": p["alias"],
                "display_name": p["display_name"],
                "connected": bool(p["connected"]),
            }
            for p in _ordered_participants(room)
        ],
        "config": config_public(config),
    }


def start(room: dict, next_seq: Callable[[dict], int]) -> list[Emit]:
    config = GameConfig.from_dict(room["config"])
    seed = config.random_seed
    if seed is None:
        seed = secrets.SystemRandom().randrange(2**53)
    room["seed"] = seed
    engine = PulkyEngine.start(config, display_names(room), seed)
    store_engine(room, engine)
    room["state"] = RoomState.RUNNING.value
    room["started_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("room %s started", room["room_code"])

    code = room["room_code"]
    host_sid = room.get("host_sid")
    emits = [
        Emit(code, "game_started", _game_started(room, next_seq(room)), host_sid),
        Emit(code, "day_opened", {"seq": next_seq(room), "brief": engine.brief()}),
    ]
    state_seq = next_seq(room)
    for sid, alias in room["sid_to_alias"].items():
        emits.append(
            Emit(sid, "your_state", player_state(room, alias, state_seq, True))
        )
    if host_sid:
        emits.append(
            Emit(host_sid, "host_state", host_state(room, next_seq(room), True))
        )
    return emits


def _finish_emits(room: dict, sid: str, next_seq: Callable[[dict], int]) -> list[Emit]:
    engine = _require_engine(room)
    emits = [
        Emit(
            sid,
            "game_finished",
            {"seq": next_seq(room), "game_id": room["game_id"], **engine.results()},
        )
    ]
    if room.get("persisted"):
        emits.append(
            Emit(
                sid,
                "game_persisted",
                {"seq": next_seq(room), "game_id": room["game_id"]},
            )
        )
    return emits


def resync_player(
    room: dict, alias: str, sid: str, next_seq: Callable[[dict], int]
) -> list[Emit]:
    emits = [Emit(sid, "game_started", _game_started(room, next_seq(room)))]
    if room["state"] == RoomState.FINISHED:
        emits.append(
            Emit(sid, "your_state", player_state(room, alias, next_seq(room), True))
        )
        return emits + _finish_emits(room, sid, next_seq)
    engine = _require_engine(room)
    emits.append(
        Emit(sid, "day_opened", {"seq": next_seq(room), "brief": engine.brief()})
    )
    emits.append(
        Emit(sid, "your_state", player_state(room, alias, next_seq(room), True))
    )
    return emits


def resync_host(room: dict, sid: str, next_seq: Callable[[dict], int]) -> list[Emit]:
    emits = [Emit(sid, "host_state", host_state(room, next_seq(room), True))]
    if room["state"] == RoomState.FINISHED:
        emits += _finish_emits(room, sid, next_seq)
    return emits
