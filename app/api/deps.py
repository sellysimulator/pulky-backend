"""Authentication dependencies shared by REST and the socket handshake."""

import logging
from typing import Any

from fastapi import Header, HTTPException
from firebase_admin import auth

from app.core import firebase

logger = logging.getLogger(__name__)

SIGN_IN_REQUIRED = "Sign in to continue."
SIGN_IN_UNAVAILABLE = "Sign-in is not available right now."


def verify_firebase_id_token(token: str) -> dict[str, Any] | None:
    """Claims of a valid token, else ``None``. ``None`` always means reject."""
    if not token or firebase.init_firebase() is None:
        return None
    try:
        claims = auth.verify_id_token(token)
    except Exception:
        return None
    return dict(claims) if claims else None


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(status_code=401, detail=SIGN_IN_REQUIRED)
    return parts[1]


def _verified(token: str) -> dict[str, Any]:
    claims = verify_firebase_id_token(token)
    if claims is None:
        raise HTTPException(status_code=401, detail=SIGN_IN_REQUIRED)
    return claims


async def get_current_firebase_user(
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    if firebase.init_firebase() is None:
        raise HTTPException(status_code=503, detail=SIGN_IN_UNAVAILABLE)
    token = _bearer_token(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail=SIGN_IN_REQUIRED)
    return _verified(token)


async def get_optional_firebase_user(
    authorization: str | None = Header(None),
) -> dict[str, Any] | None:
    token = _bearer_token(authorization)
    if token is None:
        return None
    return _verified(token)
