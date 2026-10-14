"""Config defaults and dry-run validation for the host screen (spec 13.4)."""

from typing import Any

from fastapi import APIRouter

from app.core import config_models
from app.schemas.room import ConfigValidateRequest

router = APIRouter()


@router.get("/config/defaults")
async def config_defaults() -> dict[str, Any]:
    return config_models.config_schema()


def _issues(items: list[config_models.FieldIssue]) -> list[dict[str, str | None]]:
    return [{"field": i.field, "message": i.message} for i in items]


@router.post("/config/validate")
async def config_validate(body: ConfigValidateRequest) -> dict[str, Any]:
    result = config_models.validate_config(body.config, None)
    return {
        "ok": len(result.errors) == 0,
        "errors": _issues(result.errors),
        "warnings": _issues(result.warnings),
    }
