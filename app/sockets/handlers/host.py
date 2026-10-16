"""Host handlers: end the game early (spec 4.5, 11.3, 12.1)."""

from app.core.enums import RoomState
from app.services import play_service, room_service, state_service
from app.services.room_service import Emit
from app.sockets.errors import guarded
from app.sockets.manager import sio


def _error(sid: str, code: str, message: str) -> Emit:
    return Emit(sid, "error", {"code": code, "message": message})


@sio.event
@guarded()
async def end_game_early(sid: str, data: dict | None = None) -> None:
    code = str((data or {}).get("room_id") or "").strip().upper()
    state = state_service.get_state_service()
    emits: list[Emit] = []
    ended = False
    async with state.lock(code):
        room = await state.get_room(code)
        if room is None:
            emits.append(_error(sid, "NOT_IN_ROOM", play_service.MSG_NOT_IN_ROOM))
        elif not room_service.check_host_secret(room, data):
            emits.append(_error(sid, "INVALID_HOST_SECRET", play_service.MSG_HOST_ONLY))
        elif room["state"] != RoomState.RUNNING:
            emits.append(
                _error(sid, "GAME_NOT_RUNNING", play_service.MSG_GAME_NOT_RUNNING)
            )
        else:
            code = room["room_code"]
            emits = play_service.end_early(room, state.next_seq)
            await state.save_room(code, room)
            ended = True
    await room_service.send(emits)
    if ended:
        await play_service.finish(code)
