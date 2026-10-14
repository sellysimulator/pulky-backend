"""firebase-admin initialisation: the one impure module in core."""

import json
import logging

import firebase_admin
from firebase_admin import credentials

from app.config import get_settings

logger = logging.getLogger(__name__)


def init_firebase() -> firebase_admin.App | None:
    """Initialise firebase-admin lazily, once.

    Returns ``None`` when ``FIREBASE_SERVICE_ACCOUNT_JSON`` is empty or unusable.
    """
    raw = get_settings().FIREBASE_SERVICE_ACCOUNT_JSON.strip()
    if not raw:
        return None
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass
    try:
        account = json.loads(raw)
        return firebase_admin.initialize_app(credentials.Certificate(account))
    except (ValueError, TypeError):
        logger.error("FIREBASE_SERVICE_ACCOUNT_JSON could not be parsed.")
        return None
