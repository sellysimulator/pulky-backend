"""Fail loudly when auth is unconfigured (spec 0.5 invariant 6)."""

import logging

from app.core import firebase
from app.core.checks import register_check

logger = logging.getLogger(__name__)

UNCONFIGURED_MESSAGE = (
    "FIREBASE_SERVICE_ACCOUNT_JSON is not set — signed-in users will be REJECTED "
    "at the Socket.IO handshake and /users/* will 503."
)


@register_check
def check_auth() -> None:
    if firebase.init_firebase() is None:
        logger.critical(UNCONFIGURED_MESSAGE)
    else:
        logger.info("Firebase token verification is active.")
