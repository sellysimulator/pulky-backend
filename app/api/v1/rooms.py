"""Room routes: create, batch create, summary, status, config read and patch (spec
11.3, 13.4)."""

import copy
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core import abuse
from app.core.config_models import ConfigError, host_default_config
from app.schemas.room import (
    BatchCreateRequest,
    ConfigPatchRequest,
    CreateRoomRequest,
    RoomsSummaryRequest,
)
from app.services import room_service, state_service
from app.services.room_turn import room_turn

logger = logging.getLogger(__name__)

router = APIRouter()

TOO_MANY_ROOMS = "Too many games are open right now. Try again in a few minutes."


async def _authorised_room(code: str, secret: str | None) -> dict:
    room = await state_service.get_state_service().get_room(code)
    if room is None:
        raise HTTPException(status_code=404, detail=room_service.STATUS_NOT_FOUND)
    if not room_service.check_host_secret(room, {"host_secret": secret}):
        raise HTTPException(status_code=403, detail=room_service.MSG_HOST_ONLY)
    return room


@router.post("/rooms/create")
async def create_room(body: CreateRoomRequest | None = None) -> dict[str, Any]:
    raw_name = body.host_display_name if body is not None else None
    name = room_service.sanitise_display_name(raw_name, "Host")
    # The host's config: null at each hidden default (spec 9.3).
    config = host_default_config()
    try:
        room = await state_service.get_state_service().create_room(config, name)
    except state_service.RoomLimitReached:
        raise _too_many_rooms() from None
    logger.info("room %s created", room["room_code"])
    return _created(room)


def _created(room: dict) -> dict[str, Any]:
    """The answer for a room just made. Only the create routes send it: the host
    secret goes to the creator, once."""
    return {
        "room_code": room["room_code"],
        "host_secret": room["host_secret"],
        "state": room["state"],
        "config": room["config"],
    }


def _too_many_rooms() -> HTTPException:
    logger.warning("room creation refused: live room limit reached")
    return HTTPException(status_code=503, detail=TOO_MANY_ROOMS)


@router.post("/rooms/batch-create", response_model=None)
async def create_room_batch(body: BatchCreateRequest, request: Request) -> Any:
    """``count`` rooms that all start with one config: the multi-game host dashboard.

    The config is validated once, before any room exists; a bad one is the 400 a
    config save gives and makes nothing. The rooms are charged to the caller's
    ``POST /rooms/create`` budget, all or nothing, only after that. If the live room
    cap is reached partway, the rooms this batch made are deleted again."""
    try:
        config = room_service.new_room_config(body.config)
    except ConfigError as error:
        return JSONResponse(
            status_code=400, content={"detail": error.message, "field": error.field}
        )
    service = state_service.get_state_service()
    if not service.has_room_for(body.count):
        raise _too_many_rooms()
    if not abuse.allow_room_creates(request.scope, body.count):
        raise HTTPException(
            status_code=429,
            detail=abuse.TOO_MANY_REQUESTS,
            headers={"Retry-After": abuse.create_retry_after()},
        )
    name = room_service.sanitise_display_name(body.host_display_name, "Host")
    made: list[dict] = []
    try:
        for _ in range(body.count):
            made.append(await service.create_room(copy.deepcopy(config), name))
    except Exception as error:
        # All or nothing: a batch that cannot be made whole leaves no room behind.
        for room in made:
            await service.delete_room(room["room_code"])
        if isinstance(error, state_service.RoomLimitReached):
            raise _too_many_rooms() from None
        raise
    logger.info("batch of %d rooms created", len(made))
    return {"rooms": [_created(room) for room in made]}


@router.post("/rooms/summary")
async def summarise_rooms(body: RoomsSummaryRequest) -> dict[str, Any]:
    """State, seats, day and ``can_start`` of each room the caller hosts, one entry
    per input in order (``room_service.room_summary``). Read-only: no lock, no save,
    so polling never extends an unstarted room's lifetime."""
    service = state_service.get_state_service()
    rooms = []
    for hosted in body.rooms:
        room = await service.get_room(hosted.room_code)
        rooms.append(
            room_service.room_summary(hosted.room_code, room, hosted.host_secret)
        )
    return {"rooms": rooms}


@router.get("/rooms/{code}/status")
async def get_status(code: str) -> dict[str, Any]:
    room = await state_service.get_state_service().get_room(code)
    return room_service.room_status(room)


@router.get("/rooms/{code}/config")
async def get_config(
    code: str, x_host_secret: str | None = Header(None)
) -> dict[str, Any]:
    room = await _authorised_room(code, x_host_secret)
    return {"config": room["config"]}


@router.put("/rooms/{code}/config", response_model=None)
async def put_config(
    code: str, body: ConfigPatchRequest, x_host_secret: str | None = Header(None)
) -> Any:
    service = state_service.get_state_service()
    async with room_turn(code) as emits:
        room = await _authorised_room(code, x_host_secret)
        try:
            emits += room_service.apply_config_update(
                room, body.config, service.next_seq
            )
        except room_service.ConfigLocked as locked:
            return JSONResponse(status_code=409, content={"detail": str(locked)})
        except ConfigError as error:
            return JSONResponse(
                status_code=400,
                content={"detail": error.message, "field": error.field},
            )
        await service.save_room(room["room_code"], room)
    return {"config": room["config"]}
