"""Play-loop service: submissions, day resolution emits, early end, persistence."""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.core.engine import DayOutcome
from app.core.enums import RoomState
from app.core.records import parse_decision
from app.services import db_service, game_service, state_service
from app.services.room_service import Emit, send

logger = logging.getLogger(__name__)

MSG_NOT_IN_ROOM = "You are not in this room."
MSG_HOST_ONLY = "Only the host can do that."
MSG_GAME_NOT_RUNNING = "The game is not running."
MSG_GAME_FINISHED = "The game is over."


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark_finished(room: dict) -> None:
    """Finish the room and give the game its id: the only key of the stored game, its
    results link and its export (spec 11.3), since a room code is reissued later."""
    room["state"] = RoomState.FINISHED.value
    room["finished_at"] = _now()
    room["game_id"] = room.get("game_id") or uuid.uuid4().hex


def _game_finished(room: dict, results: dict, seq: int) -> dict:
    return {"seq": seq, "game_id": room["game_id"], **results}


def _sids_for(room: dict, alias: str) -> list[str]:
    return [s for s, a in room["sid_to_alias"].items() if a == alias]


def _daily_state_emits(room: dict, next_seq: Callable[[dict], int]) -> list[Emit]:
    """Daily ``your_state`` to every seated sid (shared seq), then ``host_state``."""
    emits: list[Emit] = []
    state_seq = next_seq(room)
    for sid, alias in room["sid_to_alias"].items():
        emits.append(
            Emit(
                sid,
                "your_state",
                game_service.player_state(room, alias, state_seq, False),
            )
        )
    host_sid = room.get("host_sid")
    if host_sid:
        emits.append(
            Emit(
                host_sid,
                "host_state",
                game_service.host_state(room, next_seq(room), False),
            )
        )
    return emits


def apply_submission(
    room: dict, alias: str, data: dict, next_seq: Callable[[dict], int]
) -> tuple[list[Emit], bool]:
    """Apply one decision; resolve the day when everyone is in.

    Raises ``DecisionError`` for a refused decision; the room is untouched then.
    """
    engine = game_service.load_engine(room)
    if engine is None:
        raise ValueError("The room has no engine.")
    decision = parse_decision(data)
    engine.submit(alias, decision)
    code = room["room_code"]
    emits = [
        Emit(
            code,
            "decision_submitted",
            {
                "seq": next_seq(room),
                "day": decision.day,
                "alias": alias,
                "awaiting": engine.awaiting(),
            },
        )
    ]
    if engine.all_in():
        started = time.perf_counter()
        outcome = engine.resolve_day()
        game_service.store_engine(room, engine)
        emits += resolution_emits(room, outcome, next_seq)
        logger.info(
            "room %s day %d resolved in %d ms",
            code,
            outcome.day,
            int((time.perf_counter() - started) * 1000),
        )
        return emits, outcome.finished
    game_service.store_engine(room, engine)
    state_seq = next_seq(room)
    for sid in _sids_for(room, alias):
        emits.append(
            Emit(
                sid,
                "your_state",
                game_service.player_state(room, alias, state_seq, False),
            )
        )
    host_sid = room.get("host_sid")
    if host_sid:
        emits.append(
            Emit(
                host_sid,
                "host_state",
                game_service.host_state(room, next_seq(room), False),
            )
        )
    return emits, False


def resolution_emits(
    room: dict, outcome: DayOutcome, next_seq: Callable[[dict], int]
) -> list[Emit]:
    """The spec 12.3 sequence for a resolved day. The engine must already be stored."""
    engine = game_service.load_engine(room)
    if engine is None:
        raise ValueError("The room has no engine.")
    code = room["room_code"]
    if outcome.finished:
        _mark_finished(room)
    emits = [
        Emit(
            code,
            "day_closed",
            {
                "seq": next_seq(room),
                "day": outcome.day,
                "public": engine.public_results(outcome.day),
            },
        )
    ]
    record_seq = next_seq(room)
    for sid, alias in room["sid_to_alias"].items():
        record = outcome.records.get(alias)
        if record is None:
            continue
        emits.append(
            Emit(
                sid,
                "your_day_closed",
                {"seq": record_seq, "day": outcome.day, "record": record.to_payload()},
            )
        )
    emits += _daily_state_emits(room, next_seq)
    if outcome.finished:
        emits.append(
            Emit(
                code,
                "game_finished",
                _game_finished(room, engine.results(), next_seq(room)),
            )
        )
        logger.info("room %s finished", code)
    else:
        emits.append(
            Emit(code, "day_opened", {"seq": next_seq(room), "brief": engine.brief()})
        )
    return emits


def end_early(room: dict, next_seq: Callable[[dict], int]) -> list[Emit]:
    engine = game_service.load_engine(room)
    if engine is None:
        raise ValueError("The room has no engine.")
    engine.end_early()
    game_service.store_engine(room, engine)
    _mark_finished(room)
    emits = _daily_state_emits(room, next_seq)
    emits.append(
        Emit(
            room["room_code"],
            "game_finished",
            _game_finished(room, engine.results(), next_seq(room)),
        )
    )
    logger.info("room %s finished", room["room_code"])
    return emits


async def finish(room_code: str) -> None:
    """Persist a finished room outside any lock, then announce the game id."""
    state = state_service.get_state_service()
    try:
        async with state.lock(room_code):
            snapshot = await state.get_room(room_code)
        if snapshot is None or snapshot.get("persisted"):
            return
        game_id = await asyncio.to_thread(db_service.persist_finished_game, snapshot)
        if game_id is None:
            return
        emits: list[Emit] = []
        async with state.lock(room_code):
            room = await state.get_room(room_code)
            if room is None:
                return
            room["persisted"] = True
            emits.append(
                Emit(
                    room["room_code"],
                    "game_persisted",
                    {"seq": state.next_seq(room), "game_id": game_id},
                )
            )
            await state.save_room(room["room_code"], room)
        await send(emits)
    except Exception:
        logger.exception("room %s could not be persisted", room_code)
