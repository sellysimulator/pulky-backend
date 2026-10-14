"""Log the active live-state backend and its constraints (spec 13.3)."""

import logging

from app.config import get_settings
from app.core.checks import register_check

logger = logging.getLogger(__name__)


@register_check
def check_state_backend() -> None:
    if get_settings().REDIS_ENABLED:
        logger.info("Live room state backend: redis.")
        return
    logger.info("Live room state backend: memory.")
    logger.warning(
        "REDIS_ENABLED is false: the app must run as a single instance, and a "
        "restart loses running rooms."
    )
