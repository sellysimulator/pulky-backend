"""Warn when database TLS is not required (spec 13.5)."""

import logging

from app.config import get_settings
from app.core.checks import register_check

logger = logging.getLogger(__name__)


@register_check
def check_db_tls() -> None:
    if not get_settings().DB_REQUIRE_SSL:
        logger.warning(
            "DB_REQUIRE_SSL is false: database connections are not required to "
            "use TLS. Enable it in production."
        )
