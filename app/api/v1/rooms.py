"""Room routes: create, status, config read and patch (spec 11.3, 13.4)."""

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from app.core.config_models import ConfigError, GameConfig
from app.schemas.room import ConfigPatchRequest, CreateRoomRequest
from app.services import room_service, state_service

logger = logging.getLogger(__name__)

router = APIRouter()

FORBIDDEN_MESSAGE = "Only the host can do that."


async def _authorised_room(code: str, secret: str | None) -> dict:
    room = await state_service.get_state_service().get_room(code)
    if room is None:
        raise HTTPException(status_code=404, detail=room_service.STATUS_NOT_FOUND)
    if not room_service.check_host_secret(room, {"host_secret": secret}):
        raise HTTPException(status_code=403, detail=FORBIDDEN_MESSAGE)
    return room


@router.post("/rooms/create")
async def create_room(body: CreateRoomRequest | None = None) -> dict[str, Any]:
    raw_name = body.host_display_name if body is not None else None
    name = room_service.sanitise_display_name(raw_name, "Host")
    config = GameConfig.default().to_dict()
    room = await state_service.get_state_service().create_room(config, name)
    logger.info("room %s created", room["room_code"])
    return {
        "room_code": room["room_code"],
        "host_secret": room["host_secret"],
        "state": room["state"],
        "config": room["config"],
    }


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
    async with service.lock(code):
        room = await _authorised_room(code, x_host_secret)
        try:
            emits = room_service.apply_config_update(
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
    await room_service.send(emits)
    return {"config": room["config"]}
