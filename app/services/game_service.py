"""Engine-backed payloads and the start/resync emit builders (spec 12.6, 11.3)."""

import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime

from app.core.config_models import GameConfig, config_public
from app.core.engine import PulkyEngine
from app.core.enums import RoomState
from app.core.limits import ROOM_TTL_SECONDS
from app.services.room_service import Emit

logger = logging.getLogger(__name__)


def load_engine(room: dict) -> PulkyEngine | None:
    payload = room.get("engine")
    if not payload:
        return None
    return PulkyEngine.from_payload(
        payload, GameConfig.from_host_config(room["config"])
    )


def store_engine(room: dict, engine: PulkyEngine) -> None:
    room["engine"] = engine.to_payload()


# preview_decision runs after every pause in a player's typing and changes nothing, so
# it reuses the engine it built while the room is unchanged: keyed by the room's code,
# its seq (every change of the room bumps it) and its host secret (a reissued code is
# another room). Bounded; a room that changed is rebuilt and replaces its entry. A room
# that is no longer running (finished, ended early) or is gone drops its entry, and an
# entry unused for a whole room lifetime (its room may have expired) is dropped too.
PREVIEW_ENGINES_MAX = 64
PREVIEW_ENGINE_IDLE_SECONDS = float(ROOM_TTL_SECONDS)
_preview_engines: OrderedDict[str, tuple[tuple[int, str], PulkyEngine, float]] = (
    OrderedDict()
)


def preview_engine(room: dict) -> PulkyEngine | None:
    """The room's engine for a read-only preview: ``load_engine``, cached."""
    now = time.monotonic()
    _drop_idle_preview_engines(now)
    code = room["room_code"]
    if room.get("state") != RoomState.RUNNING:
        _preview_engines.pop(code, None)
        return None
    version = (int(room["seq"]), str(room["host_secret"]))
    cached = _preview_engines.get(code)
    if cached is not None and cached[0] == version:
        _preview_engines[code] = (version, cached[1], now)
        _preview_engines.move_to_end(code)
        return cached[1]
    engine = load_engine(room)
    if engine is None:
        _preview_engines.pop(code, None)
        return None
    _preview_engines[code] = (version, engine, now)
    _preview_engines.move_to_end(code)
    while len(_preview_engines) > PREVIEW_ENGINES_MAX:
        _preview_engines.popitem(last=False)
    return engine


def _drop_idle_preview_engines(now: float) -> None:
    # Oldest use first: stop at the first entry still in use.
    while _preview_engines:
        code, (_, _, used) = next(iter(_preview_engines.items()))
        if now - used < PREVIEW_ENGINE_IDLE_SECONDS:
            return
        del _preview_engines[code]


def forget_preview_engine(room_code: str) -> None:
    """Drop the room's cached preview engine: its game is over or the room is gone."""
    _preview_engines.pop(room_code, None)


def clear_preview_engines() -> None:
    """Tests only: forget every cached preview engine."""
    _preview_engines.clear()


def _alias_number(alias: str) -> int:
    digits = alias[1:]
    return int(digits) if digits.isdigit() else 0


def _ordered_participants(room: dict) -> list[dict]:
    return sorted(
        room.get("participants", {}).values(), key=lambda p: _alias_number(p["alias"])
    )


def display_names(room: dict) -> dict[str, str]:
    return {p["alias"]: p["display_name"] for p in _ordered_participants(room)}


def require_engine(room: dict) -> PulkyEngine:
    engine = load_engine(room)
    if engine is None:
        raise ValueError("The room has no engine.")
    return engine


def player_state(
    room: dict, engine: PulkyEngine, alias: str, seq: int, full: bool
) -> dict:
    """``engine`` is the room's engine, loaded once by the handler and shared by
    every payload it builds (spec 12.4)."""
    return {"seq": seq, "full": full, **engine.player_view(alias, full)}


def host_state(room: dict, engine: PulkyEngine, seq: int, full: bool) -> dict:
    view = engine.host_view(full)
    for alias, entry in view["players"].items():
        participant = room["participants"].get(alias)
        entry["connected"] = bool(participant and participant["connected"])
    # The host's config, never the engine's: a hidden default stays null (spec 9.3).
    return {
        "seq": seq,
        "full": full,
        **view,
        "config": room["config"],
        "state": room["state"],
    }


def _game_started(room: dict, seq: int) -> dict:
    config = GameConfig.from_host_config(room["config"])
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
    # The engine runs on the full config: the hidden defaults filled in (spec 9.3).
    config = GameConfig.from_host_config(room["config"])
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
            Emit(sid, "your_state", player_state(room, engine, alias, state_seq, True))
        )
    if host_sid:
        emits.append(
            Emit(host_sid, "host_state", host_state(room, engine, next_seq(room), True))
        )
    return emits


def _finish_emits(
    room: dict, engine: PulkyEngine, sid: str, next_seq: Callable[[dict], int]
) -> list[Emit]:
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
    engine = require_engine(room)
    emits = [Emit(sid, "game_started", _game_started(room, next_seq(room)))]
    if room["state"] == RoomState.FINISHED:
        emits.append(
            Emit(
                sid,
                "your_state",
                player_state(room, engine, alias, next_seq(room), True),
            )
        )
        return emits + _finish_emits(room, engine, sid, next_seq)
    emits.append(
        Emit(sid, "day_opened", {"seq": next_seq(room), "brief": engine.brief()})
    )
    emits.append(
        Emit(sid, "your_state", player_state(room, engine, alias, next_seq(room), True))
    )
    return emits


def resync_host(room: dict, sid: str, next_seq: Callable[[dict], int]) -> list[Emit]:
    engine = require_engine(room)
    emits = [Emit(sid, "host_state", host_state(room, engine, next_seq(room), True))]
    if room["state"] == RoomState.FINISHED:
        emits += _finish_emits(room, engine, sid, next_seq)
    return emits
