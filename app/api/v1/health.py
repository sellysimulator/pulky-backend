"""Health routes (spec 13.4)."""

import asyncio
import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings
from app.db import session as db_session
from app.services import state_backend

logger = logging.getLogger(__name__)

router = APIRouter()

PROBE_TIMEOUT_SECONDS = 2.0


@router.get("/health")
async def health() -> dict[str, str]:
    """Wake-up probe: touches no database, no Redis, no Firebase."""
    return {"status": "ok"}


def _probe_database() -> None:
    with db_session.get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))


async def _database_status() -> str:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_probe_database), timeout=PROBE_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning("Database health probe failed.")
        return "error"
    return "ok"


async def _redis_status() -> str:
    if not get_settings().REDIS_ENABLED:
        return "disabled"
    try:
        backend = state_backend.get_state_backend()
        await asyncio.wait_for(
            backend.exists("health:probe"), timeout=PROBE_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning("Redis health probe failed.")
        return "error"
    return "ok"


@router.get("/health/deep")
async def health_deep() -> dict[str, str]:
    database, redis = await asyncio.gather(_database_status(), _redis_status())
    degraded = "error" in (database, redis)
    return {
        "status": "degraded" if degraded else "ok",
        "database": database,
        "redis": redis,
        "state_backend": "redis" if get_settings().REDIS_ENABLED else "memory",
    }
