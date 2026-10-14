"""Socket handshake and disconnect bookkeeping (spec 11.3, 13.3)."""

import logging
import re
import uuid

from app.api import deps
from app.services import room_service, state_service
from app.sockets import manager as sockets_manager
from app.sockets.manager import sio

logger = logging.getLogger(__name__)

GUEST_RE = re.compile(r"^guest_[0-9a-f-]{36}$")


@sio.event
async def connect(sid: str, environ: dict, auth: dict | None = None) -> bool | None:
    """Set the identity once; a token that fails verification rejects the socket."""
    claims = auth if isinstance(auth, dict) else {}
    id_token = claims.get("idToken")
    guest_id = claims.get("guestId")
    if id_token:
        decoded = deps.verify_firebase_id_token(id_token)
        if not decoded:
            return False  # reject; never downgrade to guest
        identity = str(decoded["uid"])
    elif isinstance(guest_id, str) and GUEST_RE.match(guest_id):
        identity = guest_id
    else:
        identity = f"guest_{uuid.uuid4()}"
    sockets_manager.get_socket_manager().sid_to_identity[sid] = identity
    return None


@sio.event
async def disconnect(sid: str) -> None:
    """Mark the seat or the host as gone; nothing blocks or pauses play."""
    sockets = sockets_manager.get_socket_manager()
    try:
        player = sockets.sid_to_alias.get(sid)
        host_code = sockets.sid_to_host_room.get(sid)
        code = player[0] if player else host_code
        if not code:
            return
        state = state_service.get_state_service()
        emits: list[room_service.Emit] = []
        async with state.lock(code):
            room = await state.get_room(code)
            if room is None:
                return
            alias = room["sid_to_alias"].pop(sid, None)
            if alias is not None:
                participant = room["participants"].get(alias)
                if participant is not None:
                    participant["connected"] = False
                    emits.append(
                        room_service.Emit(
                            room["room_code"],
                            "participant_disconnected",
                            {
                                "seq": state.next_seq(room),
                                "alias": alias,
                                "display_name": participant["display_name"],
                            },
                        )
                    )
                await state.save_room(code, room)
            elif room.get("host_sid") == sid:
                room["host_sid"] = None
                emits.append(
                    room_service.Emit(
                        room["room_code"],
                        "host_disconnected",
                        {"seq": state.next_seq(room)},
                    )
                )
                await state.save_room(code, room)
        await room_service.send(emits)
    finally:
        sockets.forget(sid)
