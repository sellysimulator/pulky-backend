"""Warn when database TLS is off or unverified (spec 13.5)."""

import logging

from app.config import get_settings
from app.core.checks import register_check

logger = logging.getLogger(__name__)


@register_check
def check_db_tls() -> None:
    current = get_settings()
    if current.DB_SSL_CA:
        return  # sslmode=verify-full: encrypted, certificate and hostname checked
    if not current.DB_REQUIRE_SSL:
        logger.warning(
            "DB_REQUIRE_SSL is false and DB_SSL_CA is empty: PostgreSQL connections "
            "are not required to use TLS. Enable it in production."
        )
    else:
        logger.warning(
            "DB_SSL_CA is empty: PostgreSQL connections require TLS but do not "
            "verify the server certificate (sslmode=require). Set DB_SSL_CA to the "
            "PostgreSQL service's CA certificate in production."
        )
