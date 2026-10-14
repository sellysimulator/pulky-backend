"""Play handlers: submit a day's decision, ask for the state again (spec 4.3, 12)."""

from app.core.enums import RoomState
from app.core.records import DecisionError, StaleDay
from app.services import game_service, play_service, room_service, state_service
from app.services.room_service import Emit
from app.sockets.errors import guarded
from app.sockets.manager import sio


def _code(data: dict | None) -> str:
    return str((data or {}).get("room_id") or "").strip().upper()


def _error(sid: str, code: str, message: str) -> Emit:
    return Emit(sid, "error", {"code": code, "message": message})


@sio.event
@guarded()
async def submit_day(sid: str, data: dict | None = None) -> None:
    code = _code(data)
    state = state_service.get_state_service()
    emits: list[Emit] = []
    finished = False
    async with state.lock(code):
        room = await state.get_room(code)
        alias = room["sid_to_alias"].get(sid) if room is not None else None
        if room is None or alias is None:
            emits.append(_error(sid, "NOT_IN_ROOM", play_service.MSG_NOT_IN_ROOM))
        elif room["state"] == RoomState.FINISHED:
            emits.append(_error(sid, "GAME_FINISHED", play_service.MSG_GAME_FINISHED))
        elif room["state"] != RoomState.RUNNING:
            emits.append(
                _error(sid, "GAME_NOT_RUNNING", play_service.MSG_GAME_NOT_RUNNING)
            )
        else:
            code = room["room_code"]
            try:
                emits, finished = play_service.apply_submission(
                    room, alias, data or {}, state.next_seq
                )
            except StaleDay as exc:
                emits = [
                    _error(sid, exc.code, exc.message),
                    Emit(
                        sid,
                        "your_state",
                        game_service.player_state(
                            room, alias, state.next_seq(room), True
                        ),
                    ),
                ]
                await state.save_room(code, room)
            except DecisionError as exc:
                emits = [_error(sid, exc.code, exc.message)]
            else:
                await state.save_room(code, room)
    await room_service.send(emits)
    if finished:
        await play_service.finish(code)


@sio.event
@guarded()
async def request_state(sid: str, data: dict | None = None) -> None:
    code = _code(data)
    state = state_service.get_state_service()
    emits: list[Emit] = []
    async with state.lock(code):
        room = await state.get_room(code)
        if room is None:
            emits.append(_error(sid, "NOT_IN_ROOM", play_service.MSG_NOT_IN_ROOM))
        else:
            code = room["room_code"]
            started = room["state"] in (RoomState.RUNNING, RoomState.FINISHED)
            alias = room["sid_to_alias"].get(sid)
            if sid == room.get("host_sid"):
                if started:
                    payload = game_service.host_state(room, state.next_seq(room), True)
                    emits.append(Emit(sid, "host_state", payload))
                else:
                    payload = room_service.lobby_payload(room, state.next_seq(room))
                    emits.append(Emit(sid, "lobby_update", payload))
            elif alias is not None:
                if started:
                    payload = game_service.player_state(
                        room, alias, state.next_seq(room), True
                    )
                    emits.append(Emit(sid, "your_state", payload))
                else:
                    payload = room_service.lobby_payload(room, state.next_seq(room))
                    emits.append(Emit(sid, "lobby_update", payload))
            else:
                emits.append(_error(sid, "NOT_IN_ROOM", play_service.MSG_NOT_IN_ROOM))
            if not (emits and emits[0].event == "error"):
                await state.save_room(code, room)
    await room_service.send(emits)
