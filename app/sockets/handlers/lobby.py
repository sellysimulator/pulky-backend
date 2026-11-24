"""Lobby handlers: host claim, join, leave, config update, start (spec 11.3, 12)."""

import uuid

from app.core.config_models import MSG_CONFIG_LOCKED, ConfigError
from app.core.enums import RoomState
from app.services import game_service, play_service, room_service, state_service
from app.services.room_service import Emit
from app.sockets import manager as sockets_manager
from app.sockets.errors import error_emit, guarded, requested_room
from app.sockets.manager import sio


def _join_error(sid: str, message: str) -> Emit:
    return Emit(sid, "join_error", {"message": message})


def _identity(sid: str) -> str:
    identities = sockets_manager.get_socket_manager().sid_to_identity
    if sid not in identities:
        identities[sid] = f"guest_{uuid.uuid4()}"
    return identities[sid]


@sio.event
@guarded("join_error")
async def join_waiting(sid: str, data: dict | None = None) -> None:
    code = requested_room(data)
    state = state_service.get_state_service()
    sockets = sockets_manager.get_socket_manager()
    identity = _identity(sid)
    emits: list[Emit] = []
    claimed = False
    async with state.lock(code):
        room = await state.get_room(code)
        if room is None:
            emits.append(_join_error(sid, room_service.STATUS_NOT_FOUND))
        elif not (
            room_service.check_host_secret(room, data)
            or room_service.check_host_identity(room, identity)
        ):
            emits.append(_join_error(sid, room_service.MSG_HOST_ONLY))
        else:
            code = room["room_code"]
            claimed = True
            dropped = bool(room.get("host_identity")) and room.get("host_sid") is None
            old_sid = room.get("host_sid")
            room["host_sid"] = sid
            if not room.get("host_identity"):
                room["host_identity"] = identity
            if dropped:
                emits.append(
                    Emit(
                        code,
                        "host_reconnected",
                        {"seq": state.next_seq(room)},
                        to_room=True,
                    )
                )
            emits.append(
                Emit(
                    sid,
                    "host_claimed",
                    {"room_id": code, "host_secret": room["host_secret"]},
                )
            )
            emits.append(
                Emit(
                    sid,
                    "lobby_update",
                    room_service.lobby_payload(room, state.next_seq(room)),
                )
            )
            emits.append(
                Emit(
                    sid,
                    "config_updated",
                    {"seq": state.next_seq(room), "config": room["config"]},
                )
            )
            if room["state"] in (RoomState.RUNNING, RoomState.FINISHED):
                emits += game_service.resync_host(room, sid, state.next_seq)
            await state.save_room(code, room)
            if old_sid and old_sid != sid:
                sockets.sid_to_host_room.pop(old_sid, None)
    if claimed:
        await sockets.enter_room(sid, code)
    await room_service.send(emits)
    if claimed and play_service.needs_persist(room):
        await play_service.finish(code)  # its write failed earlier (spec 13.5)


def _drop_other_sids(room: dict, alias: str, keep_sid: str) -> None:
    sockets = sockets_manager.get_socket_manager()
    stale = [s for s, a in room["sid_to_alias"].items() if a == alias and s != keep_sid]
    for old in stale:
        del room["sid_to_alias"][old]
        sockets.sid_to_alias.pop(old, None)


@sio.event
@guarded("join_error")
async def join(sid: str, data: dict | None = None) -> None:
    code = requested_room(data)
    state = state_service.get_state_service()
    sockets = sockets_manager.get_socket_manager()
    identity = _identity(sid)
    session_token = (data or {}).get("session_token")
    emits: list[Emit] = []
    seated: str | None = None
    async with state.lock(code):
        room = await state.get_room(code)
        if room is None:
            emits.append(_join_error(sid, room_service.STATUS_NOT_FOUND))
        else:
            code = room["room_code"]
            alias = room_service.find_alias_for_reconnect(
                room, session_token, identity
            ) or room_service.find_alias_for_identity(room, identity)
            running = room["state"] in (RoomState.RUNNING, RoomState.FINISHED)
            if alias is not None:
                participant = room["participants"][alias]
                _drop_other_sids(room, alias, sid)
                room["sid_to_alias"][sid] = alias
                was_connected = bool(participant["connected"])
                participant["connected"] = True
                seated = alias
                emits.append(
                    Emit(
                        sid,
                        "joined",
                        {
                            "alias": alias,
                            "session_token": participant["session_token"],
                            "display_name": participant["display_name"],
                        },
                    )
                )
                if not was_connected:
                    emits.append(
                        Emit(
                            code,
                            "participant_reconnected",
                            {
                                "seq": state.next_seq(room),
                                "alias": alias,
                                "display_name": participant["display_name"],
                            },
                            to_room=True,
                        )
                    )
                if running:
                    emits += game_service.resync_player(
                        room, alias, sid, state.next_seq
                    )
                else:
                    emits.append(
                        Emit(
                            code,
                            "lobby_update",
                            room_service.lobby_payload(room, state.next_seq(room)),
                            to_room=True,
                        )
                    )
                await state.save_room(code, room)
            elif room["state"] == RoomState.RUNNING:
                emits.append(_join_error(sid, room_service.STATUS_STARTED))
            elif room["state"] == RoomState.FINISHED:
                emits.append(_join_error(sid, room_service.STATUS_FINISHED))
            elif room_service.seats_taken(room) >= int(room["config"]["max_players"]):
                emits.append(_join_error(sid, room_service.STATUS_FULL))
            else:
                alias = state_service.next_free_alias(room)
                display_name = room_service.sanitise_display_name(
                    (data or {}).get("display_name"), f"Pulquería {alias}"
                )
                token = uuid.uuid4().hex
                room["participants"][alias] = {
                    "alias": alias,
                    "identity": identity,
                    "session_token": token,
                    "display_name": display_name,
                    "connected": True,
                }
                room["sid_to_alias"][sid] = alias
                seated = alias
                emits.append(
                    Emit(
                        sid,
                        "joined",
                        {
                            "alias": alias,
                            "session_token": token,
                            "display_name": display_name,
                        },
                    )
                )
                emits.append(
                    Emit(
                        code,
                        "lobby_update",
                        room_service.lobby_payload(room, state.next_seq(room)),
                        to_room=True,
                    )
                )
                await state.save_room(code, room)
    if seated is not None:
        await sockets.join_room(sid, code, seated)
    await room_service.send(emits)
    if seated is not None and play_service.needs_persist(room):
        await play_service.finish(code)  # its write failed earlier (spec 13.5)


@sio.event
@guarded("join_error")
async def leave(sid: str, data: dict | None = None) -> None:
    code = requested_room(data)
    state = state_service.get_state_service()
    sockets = sockets_manager.get_socket_manager()
    emits: list[Emit] = []
    left = False
    async with state.lock(code):
        room = await state.get_room(code)
        alias = room["sid_to_alias"].get(sid) if room is not None else None
        if room is None or alias is None:
            emits.append(Emit(sid, "leave_ack", {"room_id": code}))
        elif room["state"] == RoomState.RUNNING:
            emits.append(_join_error(sid, room_service.STATUS_STARTED))
        elif room["state"] == RoomState.FINISHED:
            emits.append(_join_error(sid, room_service.STATUS_FINISHED))
        else:
            code = room["room_code"]
            room["participants"].pop(alias, None)
            for other in [s for s, a in room["sid_to_alias"].items() if a == alias]:
                del room["sid_to_alias"][other]
                sockets.sid_to_alias.pop(other, None)
            left = True
            emits.append(Emit(sid, "leave_ack", {"room_id": code}))
            emits.append(
                Emit(
                    code,
                    "lobby_update",
                    room_service.lobby_payload(room, state.next_seq(room)),
                    to_room=True,
                )
            )
            await state.save_room(code, room)
    if left:
        await sockets.leave_room(sid, code)
    await room_service.send(emits)


@sio.event
@guarded()
async def config_update(sid: str, data: dict | None = None) -> None:
    code = requested_room(data)
    state = state_service.get_state_service()
    emits: list[Emit] = []
    async with state.lock(code):
        room = await state.get_room(code)
        if room is None:
            emits.append(error_emit(sid, "NOT_IN_ROOM", room_service.MSG_NOT_IN_ROOM))
        elif not room_service.check_host_secret(room, data):
            emits.append(
                error_emit(sid, "INVALID_HOST_SECRET", room_service.MSG_HOST_ONLY)
            )
        else:
            patch = (data or {}).get("config")
            try:
                emits = room_service.apply_config_update(
                    room, patch if isinstance(patch, dict) else {}, state.next_seq
                )
            except room_service.ConfigLocked:
                emits = [error_emit(sid, "INVALID_CONFIG", MSG_CONFIG_LOCKED)]
            except ConfigError as exc:
                emits = [error_emit(sid, "INVALID_CONFIG", exc.message, exc.field)]
            else:
                await state.save_room(room["room_code"], room)
    await room_service.send(emits)


def _start_refusal(sid: str, code: str, message: str) -> Emit:
    """A refused ``start_game``: ``join_error`` plus the ``room_code`` it named, so the
    multi-game dashboard shows it on that game's card (spec 11.3, 12.2)."""
    return Emit(sid, "join_error", {"message": message, "room_code": code})


def _record_host_uid(room: dict, sid: str) -> None:
    """Keep the starting connection's verified uid as the game's host record (spec
    11.3, 13.5): server-only, never sent, read only at persist. A guest connection
    leaves it unset; the first start wins."""
    uid = sockets_manager.get_socket_manager().sid_to_verified_uid.get(sid)
    if uid and not room.get("host_uid"):
        room["host_uid"] = uid


@sio.event
@guarded("join_error", echo_room_code=True)
async def start_game(sid: str, data: dict | None = None) -> None:
    code = requested_room(data)
    state = state_service.get_state_service()
    emits: list[Emit] = []
    async with state.lock(code):
        room = await state.get_room(code)
        if room is None:
            emits.append(_start_refusal(sid, code, room_service.STATUS_NOT_FOUND))
        elif not room_service.check_host_secret(room, data):
            emits.append(_start_refusal(sid, code, room_service.MSG_HOST_ONLY))
        elif room["state"] in (RoomState.RUNNING, RoomState.FINISHED):
            return
        else:
            ok, reason = room_service.can_start(room)
            if not ok:
                emits.append(
                    _start_refusal(sid, code, reason or room_service.START_ALREADY)
                )
            else:
                _record_host_uid(room, sid)
                emits = game_service.start(room, state.next_seq)
                await state.save_room(room["room_code"], room)
    await room_service.send(emits)
