"""Request bodies of the room and config routes (spec 13.4)."""

from typing import Any

from pydantic import BaseModel


class CreateRoomRequest(BaseModel):
    """``host_display_name`` is sanitised by the service, whatever it holds."""

    host_display_name: Any = None


class ConfigPatchRequest(BaseModel):
    config: dict[str, Any]


class ConfigValidateRequest(BaseModel):
    config: dict[str, Any]
