"""Host handlers: end the game early (spec 4.5, 11.3, 12.1)."""

from app.core.enums import RoomState
from app.services import play_service, room_service, state_service
from app.services.room_turn import room_turn
from app.sockets.errors import error_emit, guarded, requested_room
from app.sockets.manager import sio


@sio.event
@guarded()
async def end_game_early(sid: str, data: dict | None = None) -> None:
    code = requested_room(data)
    state = state_service.get_state_service()
    ended = False
    async with room_turn(code) as emits:
        room = await state.get_room(code)
        if room is None:
            emits.append(error_emit(sid, "NOT_IN_ROOM", room_service.MSG_NOT_IN_ROOM))
        elif not room_service.check_host_secret(room, data):
            emits.append(
                error_emit(sid, "INVALID_HOST_SECRET", room_service.MSG_HOST_ONLY)
            )
        elif room["state"] != RoomState.RUNNING:
            emits.append(
                error_emit(sid, "GAME_NOT_RUNNING", play_service.MSG_GAME_NOT_RUNNING)
            )
        else:
            code = room["room_code"]
            emits += play_service.end_early(room, state.next_seq)
            await state.save_room(code, room)
            ended = True
    if ended:
        await play_service.finish(code)
