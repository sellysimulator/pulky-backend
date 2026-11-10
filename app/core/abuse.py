"""Abuse limits (spec 0.6): request and event rates, open sockets, live rooms.

Every counter lives in this process (render.yaml runs one instance), in dicts whose
size is bounded. The limits are constants, not settings. Standard library only.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

# Rates are per fixed window of WINDOW_SECONDS. A classroom often shares one IP.
WINDOW_SECONDS = 60.0
REST_PER_IP = 600  # every REST request, per IP
ROOM_LOOKUPS_PER_IP = 120  # requests that name a room code the caller is not in
ROUTE_LIMITS: dict[str, int] = {  # costly public routes, per IP, on top of REST_PER_IP
    "POST /api/v1/rooms/create": 10,
    "POST /api/v1/config/validate": 300,
    "POST /api/v1/config/preview": 300,
}
ROOM_LOOKUP_ROUTES: frozenset[str] = frozenset(
    {
        "GET /api/v1/rooms/{code}/status",
        "GET /api/v1/rooms/{code}/config",
        "PUT /api/v1/rooms/{code}/config",
    }
)
UNLIMITED_PATHS: frozenset[str] = frozenset({"/api/v1/health"})  # Render's probe
MAX_BODY_BYTES = 65_536  # a REST body or a Socket.IO message; a full config is ~1 KB
EVENTS_PER_SID = 300  # Socket.IO events per socket
EVENTS_PER_IP = 3000  # Socket.IO events per IP, all its sockets together
SOCKETS_PER_IP = 100  # open sockets per IP
MAX_SOCKETS = 5000  # open sockets in the process
MAX_LIVE_ROOMS = 500  # live rooms in process memory
LOBBY_ROOM_TTL_SECONDS = 7200  # a room that has not started, since its last save
MAX_TRACKED_KEYS = 10_000  # keys one RateLimiter remembers
UNKNOWN_IP = "unknown"

TOO_MANY_REQUESTS = "Too many requests. Wait a minute and try again."
TOO_LARGE = "That request is too large."


class RateLimiter:
    """At most ``limit`` hits per key in each fixed window of ``window`` seconds.

    Keys sit in the order their window opened, so ended windows are dropped from the
    front in amortised O(1). Past ``max_keys`` the oldest keys are dropped down to 90 %
    of it, so that trim runs at most once per ``max_keys // 10`` new keys."""

    def __init__(
        self,
        limit: int,
        window: float = WINDOW_SECONDS,
        *,
        max_keys: int = MAX_TRACKED_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._clock = clock
        self._hits: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        entry = self._hits.get(key)
        if entry is not None and now - entry[0] < self.window:
            if entry[1] >= self.limit:
                return False
            self._hits[key] = (entry[0], entry[1] + 1)
            return True
        self._hits.pop(key, None)
        while self._hits:
            oldest, (start, _) = next(iter(self._hits.items()))
            if now - start < self.window:
                break
            del self._hits[oldest]
        self._hits[key] = (now, 1)
        if len(self._hits) > self.max_keys:
            self._trim()
        return True

    def _trim(self) -> None:
        keep = self.max_keys * 9 // 10
        while len(self._hits) > keep:
            del self._hits[next(iter(self._hits))]

    def discard(self, key: str) -> None:
        self._hits.pop(key, None)

    def __len__(self) -> int:
        return len(self._hits)

    def clear(self) -> None:
        self._hits.clear()


rest_limiter = RateLimiter(REST_PER_IP)
lookup_limiter = RateLimiter(ROOM_LOOKUPS_PER_IP)
route_limiters: dict[str, RateLimiter] = {
    route: RateLimiter(limit) for route, limit in ROUTE_LIMITS.items()
}
sid_event_limiter = RateLimiter(EVENTS_PER_SID)
ip_event_limiter = RateLimiter(EVENTS_PER_IP)

_open_per_ip: dict[str, int] = {}
_sid_ip: dict[str, str] = {}


_logged_ip_sources: set[str] = set()


def _note_ip_source(source: str) -> None:
    """Log, once per process, which branch of ``client_ip`` answered (never the IP)."""
    if source not in _logged_ip_sources:
        _logged_ip_sources.add(source)
        logger.info("client ip source: %s", source)


def client_ip(scope: Mapping[str, Any]) -> str:
    """The caller's address, from an ASGI scope (a Socket.IO ``environ`` carries its
    scope under ``"asgi.scope"``). Order: ``CF-Connecting-IP`` (Cloudflare, in front of
    Render, overwrites it), the rightmost ``X-Forwarded-For`` entry (the proxy appends
    the address it saw; the rest is the client's to choose), the peer address."""
    headers = scope.get("headers") or ()
    for k, v in headers:
        if k == b"cf-connecting-ip" and v.strip():
            _note_ip_source("cf-connecting-ip")
            return v.decode("latin-1").strip()[:64]
    forwarded = [v for k, v in headers if k == b"x-forwarded-for"]
    if forwarded:
        parts = forwarded[-1].decode("latin-1").split(",")
        last = parts[-1].strip()
        if last:
            n = sum(1 for part in parts if part.strip())
            n_label = str(n) if n <= 3 else "3+"
            _note_ip_source(f"x-forwarded-for ({n_label} entries)")
            return last[:64]
    peer = scope.get("client")
    _note_ip_source("peer" if peer else "unknown")
    return str(peer[0]) if peer else UNKNOWN_IP


def _route(method: str, path: str) -> str:
    parts = path.split("/")
    if len(parts) == 6 and parts[1:4] == ["api", "v1", "rooms"]:
        parts[4] = "{code}"
    return f"{method} {'/'.join(parts)}"


def _too_large(scope: Mapping[str, Any]) -> bool:
    for key, value in scope.get("headers") or ():
        if key == b"content-length":
            return not value.isdigit() or int(value) > MAX_BODY_BYTES
    return False


def rest_verdict(scope: Mapping[str, Any]) -> int | None:
    """``None`` to let a REST request through, else the status that refuses it."""
    path = str(scope.get("path", ""))
    if path in UNLIMITED_PATHS:
        return None
    if _too_large(scope):
        return 413
    ip = client_ip(scope)
    if not rest_limiter.allow(ip):
        return 429
    route = _route(str(scope.get("method", "")), path)
    limiter = (
        lookup_limiter if route in ROOM_LOOKUP_ROUTES else route_limiters.get(route)
    )
    if limiter is not None and not limiter.allow(ip):
        return 429
    return None


Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


async def _read_body(receive: Receive) -> tuple[list[Message], bool]:
    """The request's messages up to the end of its body, and whether that body is over
    ``MAX_BODY_BYTES``. A chunked body has no ``Content-Length``: its cap holds here."""
    messages: list[Message] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return messages, False
        size += len(message.get("body", b""))
        if size > MAX_BODY_BYTES:
            return messages, True
        if not message.get("more_body", False):
            return messages, False


class AbuseMiddleware:
    """Pure ASGI: ``rest_verdict`` in front of every HTTP request to the REST app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = rest_verdict(scope)
        if status is None:
            messages, too_large = await _read_body(receive)
            if not too_large:

                async def replay() -> Message:
                    return messages.pop(0) if messages else await receive()

                await self.app(scope, replay, send)
                return
            status = 413
        detail = TOO_MANY_REQUESTS if status == 429 else TOO_LARGE
        body = json.dumps({"detail": detail}).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if status == 429:
            headers.append((b"retry-after", str(int(WINDOW_SECONDS)).encode()))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})


def open_socket(sid: str, environ: Mapping[str, Any]) -> bool:
    """Count a new socket against its IP; ``False`` (refuse it) past the caps."""
    ip = client_ip(environ.get("asgi.scope") or {})
    if len(_sid_ip) >= MAX_SOCKETS or _open_per_ip.get(ip, 0) >= SOCKETS_PER_IP:
        return False
    _open_per_ip[ip] = _open_per_ip.get(ip, 0) + 1
    _sid_ip[sid] = ip
    return True


def close_socket(sid: str) -> None:
    """Undo ``open_socket``; safe to call for a sid it never counted."""
    sid_event_limiter.discard(sid)
    ip = _sid_ip.pop(sid, None)
    if ip is None:
        return
    left = _open_per_ip.get(ip, 1) - 1
    if left > 0:
        _open_per_ip[ip] = left
    else:
        _open_per_ip.pop(ip, None)


def allow_event(sid: str, *, lookup: bool) -> bool:
    """Every event counts against its socket and its IP; a ``lookup`` (an event that
    names a room the socket is not in) also counts as a room lookup of that IP."""
    ip = _sid_ip.get(sid, UNKNOWN_IP)
    if not (sid_event_limiter.allow(sid) and ip_event_limiter.allow(ip)):
        return False
    return not lookup or lookup_limiter.allow(ip)


def reset() -> None:
    """Tests only: forget every counter."""
    for limiter in (
        rest_limiter,
        lookup_limiter,
        sid_event_limiter,
        ip_event_limiter,
        *route_limiters.values(),
    ):
        limiter.clear()
    _open_per_ip.clear()
    _sid_ip.clear()
    _logged_ip_sources.clear()
