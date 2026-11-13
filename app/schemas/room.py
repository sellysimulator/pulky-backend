"""Request bodies of the room and config routes (spec 13.4)."""

from typing import Any

from pydantic import BaseModel, Field

from app.core.limits import MAX_BATCH_ROOMS, MIN_BATCH_ROOMS


class CreateRoomRequest(BaseModel):
    """``host_display_name`` is sanitised by the service, whatever it holds."""

    host_display_name: Any = None


class BatchCreateRequest(BaseModel):
    """``POST /rooms/batch-create``: ``count`` rooms that all start with ``config``,
    a patch over the defaults merged and validated like a host's config save. Codes
    are always generated."""

    count: int = Field(ge=MIN_BATCH_ROOMS, le=MAX_BATCH_ROOMS)
    config: dict[str, Any] = Field(default_factory=dict)
    host_display_name: Any = None


class HostedRoom(BaseModel):
    """One room the caller hosts, named with the secret that proves it."""

    room_code: str = Field(max_length=16)
    host_secret: str = Field(max_length=128)


class RoomsSummaryRequest(BaseModel):
    """``POST /rooms/summary``. A POST so the secrets never reach a URL or a log."""

    rooms: list[HostedRoom] = Field(min_length=1, max_length=MAX_BATCH_ROOMS)


class ConfigPatchRequest(BaseModel):
    config: dict[str, Any]


class ConfigValidateRequest(BaseModel):
    config: dict[str, Any]
