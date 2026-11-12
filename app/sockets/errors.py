"""The ``@guarded`` decorator: no handler error reaches Socket.IO or the client."""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core import abuse
from app.sockets.manager import get_socket_manager

logger = logging.getLogger(__name__)

SERVER_ERROR_MESSAGE = "Something went wrong. Please try again."

Handler = Callable[..., Awaitable[Any]]


def _names_another_room(sid: str, data: dict | None) -> bool:
    """True when the event names a room this socket holds no seat in and does not
    host: the event is then also a room lookup (abuse limits, spec 0.6)."""
    named = str((data or {}).get("room_id") or "").strip().upper()
    sockets = get_socket_manager()
    seat = sockets.sid_to_alias.get(sid)
    return named not in (sockets.sid_to_host_room.get(sid), seat[0] if seat else None)


async def _emit_error(sid: str, event: str, code: str, message: str) -> None:
    payload: dict[str, Any] = {"message": message}
    if event == "error":
        payload = {"code": code, **payload}
    try:
        await get_socket_manager().emit_to_sid(sid, event, payload)
    except Exception:
        logger.exception("Could not deliver the error to the client")


def guarded(event: str = "error") -> Callable[[Handler], Handler]:
    """Wrap a handler (first argument: the calling sid).

    On any exception it logs the traceback and emits a generic error to that sid.
    ``error`` carries ``{code, message}``; any other event carries ``{message}``.
    A payload that is not an object reaches the handler as ``None``. An event over
    the abuse limits (spec 0.6) never reaches the handler: the sid gets
    ``RATE_LIMITED`` the same way.
    """

    def decorator(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapper(sid: str, *args: Any, **kwargs: Any) -> Any:
            args = tuple(a if isinstance(a, dict) else None for a in args)
            lookup = _names_another_room(sid, args[0] if args else None)
            if not abuse.allow_event(sid, lookup=lookup):
                await _emit_error(sid, event, "RATE_LIMITED", abuse.TOO_MANY_REQUESTS)
                return None
            try:
                return await handler(sid, *args, **kwargs)
            except Exception:
                logger.exception(
                    "Unhandled error in socket handler %s", handler.__name__
                )
                await _emit_error(sid, event, "SERVER_ERROR", SERVER_ERROR_MESSAGE)
                return None

        return wrapper

    return decorator
