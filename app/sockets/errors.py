"""The ``@guarded`` decorator: no handler error reaches Socket.IO or the client."""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.sockets.manager import get_socket_manager

logger = logging.getLogger(__name__)

SERVER_ERROR_MESSAGE = "Something went wrong. Please try again."

Handler = Callable[..., Awaitable[Any]]


def guarded(event: str = "error") -> Callable[[Handler], Handler]:
    """Wrap a handler (first argument: the calling sid).

    On any exception it logs the traceback and emits a generic error to that sid.
    ``error`` carries ``{code, message}``; any other event carries ``{message}``.
    """

    def decorator(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapper(sid: str, *args: Any, **kwargs: Any) -> Any:
            try:
                return await handler(sid, *args, **kwargs)
            except Exception:
                logger.exception(
                    "Unhandled error in socket handler %s", handler.__name__
                )
                payload: dict[str, Any] = {"message": SERVER_ERROR_MESSAGE}
                if event == "error":
                    payload = {"code": "SERVER_ERROR", **payload}
                try:
                    await get_socket_manager().emit_to_sid(sid, event, payload)
                except Exception:
                    logger.exception("Could not deliver the server error to the client")
                return None

        return wrapper

    return decorator
