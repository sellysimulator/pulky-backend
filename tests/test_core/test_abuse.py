"""Abuse limits (spec 0.6): the rate limiter, the client IP and the REST verdicts."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import abuse
from app.core.abuse import RateLimiter, client_ip, rest_verdict
from app.services.state_service import reset_state_service


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def scope(
    path: str = "/api/v1/x",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("10.0.0.1", 5000),
) -> dict[str, Any]:
    return {
        "type": "http",
        "path": path,
        "method": method,
        "headers": headers or [],
        "client": client,
    }


# --- RateLimiter -----------------------------------------------------------------


def test_limiter_allows_the_limit_then_refuses_until_the_window_ends() -> None:
    clock = Clock()
    limiter = RateLimiter(3, 10, clock=clock)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("b")  # keys are independent
    clock.now += 9.9
    assert not limiter.allow("a")
    clock.now += 0.1
    assert limiter.allow("a")


def test_limiter_forgets_ended_windows_and_never_exceeds_max_keys() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 10, max_keys=3, clock=clock)
    for key in "abc":
        limiter.allow(key)
    clock.now += 10
    limiter.allow("d")
    assert len(limiter) == 1  # a, b and c had ended
    for key in "efgh":
        limiter.allow(key)
    assert len(limiter) == 3  # full: the oldest went
    limiter.clear()
    assert len(limiter) == 0


# --- client_ip -------------------------------------------------------------------


def test_client_ip_is_the_rightmost_forwarded_entry() -> None:
    forged = [(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")]
    assert client_ip(scope(headers=forged)) == "1.2.3.4"
    two = [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"5.5.5.5")]
    assert client_ip(scope(headers=two)) == "5.5.5.5"


def test_client_ip_falls_back_to_the_peer_then_unknown() -> None:
    assert client_ip(scope()) == "10.0.0.1"
    assert client_ip(scope(headers=[(b"x-forwarded-for", b" ")])) == "10.0.0.1"
    assert client_ip(scope(client=None)) == abuse.UNKNOWN_IP
    assert client_ip({}) == abuse.UNKNOWN_IP


# --- rest_verdict ----------------------------------------------------------------


def test_create_is_limited_per_ip() -> None:
    create = scope("/api/v1/rooms/create", "POST")
    limit = abuse.ROUTE_LIMITS["POST /api/v1/rooms/create"]
    assert [rest_verdict(create) for _ in range(limit)] == [None] * limit
    assert rest_verdict(create) == 429
    other = scope("/api/v1/rooms/create", "POST", client=("9.9.9.9", 1))
    assert rest_verdict(other) is None


def test_room_routes_share_one_lookup_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(abuse.lookup_limiter, "limit", 2)
    assert rest_verdict(scope("/api/v1/rooms/ABC234/status")) is None
    assert rest_verdict(scope("/api/v1/rooms/XYZ789/config", "PUT")) is None
    assert rest_verdict(scope("/api/v1/rooms/QQQ222/status")) == 429
    assert rest_verdict(scope("/api/v1/config/defaults")) is None


def test_every_route_counts_against_the_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(abuse.rest_limiter, "limit", 1)
    assert rest_verdict(scope()) is None
    assert rest_verdict(scope()) == 429
    for _ in range(5):
        assert rest_verdict(scope("/api/v1/health")) is None  # never limited


@pytest.mark.parametrize("length", [b"65537", b"abc"])
def test_an_oversized_or_unreadable_body_length_is_413(length: bytes) -> None:
    assert rest_verdict(scope(headers=[(b"content-length", length)])) == 413
    assert rest_verdict(scope(headers=[(b"content-length", b"65536")])) is None


@pytest.mark.asyncio
async def test_middleware_passes_other_scopes_through() -> None:
    seen: list[str] = []

    async def inner(s: Any, receive: Any, send: Any) -> None:
        seen.append(s["type"])

    async def no_receive() -> dict[str, Any]:
        raise AssertionError("not called")

    async def no_send(message: Any) -> None:
        raise AssertionError("not called")

    await abuse.AbuseMiddleware(inner)({"type": "lifespan"}, no_receive, no_send)
    assert seen == ["lifespan"]


def test_app_answers_413_for_a_large_chunked_body(client: TestClient) -> None:
    """Fails against a cap that only reads Content-Length (a chunked body has none)."""

    def chunks() -> Iterator[bytes]:
        yield b'{"config": {"x": "'
        for _ in range(4):
            yield b"y" * abuse.MAX_BODY_BYTES
        yield b'"}}'

    response = client.post(
        "/api/v1/config/validate",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": abuse.TOO_LARGE}


def test_a_small_chunked_body_still_reaches_the_route(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"config": '
        yield b"{}}"

    response = client.post(
        "/api/v1/config/validate",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_middleware_replays_the_body_then_hands_over_receive() -> None:
    incoming: list[dict[str, Any]] = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.disconnect"},
        {"type": "http.disconnect"},
    ]
    seen: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return incoming.pop(0)

    async def inner(s: Any, receive: Any, send: Any) -> None:
        for _ in range(3):
            seen.append(await receive())

    async def no_send(message: Any) -> None:
        raise AssertionError("not called")

    await abuse.AbuseMiddleware(inner)(scope("/api/v1/x", "POST"), receive, no_send)
    assert [m["type"] for m in seen] == ["http.request"] + ["http.disconnect"] * 2


@pytest.fixture
def rooms(memory_backend: Any) -> Iterator[Any]:
    reset_state_service()
    yield memory_backend
    reset_state_service()


def test_app_answers_429_with_detail_retry_after_and_cors(
    client: TestClient, rooms: Any
) -> None:
    origin = {"Origin": "https://pulkysim.web.app"}
    limit = abuse.ROUTE_LIMITS["POST /api/v1/rooms/create"]
    for _ in range(limit):
        assert client.post("/api/v1/rooms/create", headers=origin).status_code == 200
    refused = client.post("/api/v1/rooms/create", headers=origin)
    assert refused.status_code == 429
    assert refused.json() == {"detail": abuse.TOO_MANY_REQUESTS}
    assert refused.headers["retry-after"] == "60"
    assert refused.headers["access-control-allow-origin"] == origin["Origin"]


def test_app_answers_413_for_a_large_body(client: TestClient) -> None:
    body = {"config": {"x": "y" * abuse.MAX_BODY_BYTES}}
    response = client.post("/api/v1/config/validate", json=body)
    assert response.status_code == 413
    assert response.json() == {"detail": abuse.TOO_LARGE}


# --- sockets ---------------------------------------------------------------------


def environ(ip: str) -> dict[str, Any]:
    return {"asgi.scope": scope(headers=[(b"x-forwarded-for", ip.encode())])}


def test_open_sockets_are_capped_per_ip_and_in_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(abuse, "SOCKETS_PER_IP", 2)
    monkeypatch.setattr(abuse, "MAX_SOCKETS", 3)
    assert abuse.open_socket("s1", environ("1.1.1.1"))
    assert abuse.open_socket("s2", environ("1.1.1.1"))
    assert not abuse.open_socket("s3", environ("1.1.1.1"))
    abuse.close_socket("s1")
    assert abuse.open_socket("s3", environ("1.1.1.1"))
    assert abuse.open_socket("s4", environ("2.2.2.2"))
    assert not abuse.open_socket("s5", environ("3.3.3.3"))  # 3 open in total
    for sid in ("s2", "s3", "s4", "never-opened"):
        abuse.close_socket(sid)
    assert abuse.open_socket("s6", {})  # no scope: the unknown IP


def test_events_count_per_socket_per_ip_and_as_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(abuse.sid_event_limiter, "limit", 3)
    monkeypatch.setattr(abuse.lookup_limiter, "limit", 1)
    abuse.open_socket("s1", environ("1.1.1.1"))
    abuse.open_socket("s2", environ("1.1.1.1"))
    assert abuse.allow_event("s1", lookup=True)
    assert not abuse.allow_event("s2", lookup=True)  # the IP's one lookup is spent
    assert abuse.allow_event("s2", lookup=False)
    assert abuse.allow_event("s1", lookup=False)
    assert abuse.allow_event("s1", lookup=False)
    assert not abuse.allow_event("s1", lookup=False)  # s1's three events are spent
    monkeypatch.setattr(abuse.ip_event_limiter, "limit", 5)
    assert not abuse.allow_event("s2", lookup=False)  # the IP's five events are spent


# --- amendments: Cloudflare header, discard, amortised pruning -------------------


def test_client_ip_prefers_cf_connecting_ip_over_forwarded_for() -> None:
    headers = [
        (b"x-forwarded-for", b"6.6.6.6, 1.2.3.4"),
        (b"cf-connecting-ip", b" 7.7.7.7 "),
    ]
    assert client_ip(scope(headers=headers)) == "7.7.7.7"
    blank = [(b"cf-connecting-ip", b" "), (b"x-forwarded-for", b"1.2.3.4")]
    assert client_ip(scope(headers=blank)) == "1.2.3.4"
    assert client_ip({"asgi.scope": {}}) == abuse.UNKNOWN_IP
    sockets = {"asgi.scope": scope(headers=[(b"cf-connecting-ip", b"8.8.8.8")])}
    assert abuse.open_socket("c1", sockets)
    assert abuse._sid_ip["c1"] == "8.8.8.8"


def test_close_socket_forgets_the_sockets_event_counter() -> None:
    abuse.open_socket("s1", environ("1.1.1.1"))
    abuse.allow_event("s1", lookup=False)
    assert len(abuse.sid_event_limiter) == 1
    abuse.close_socket("s1")
    assert len(abuse.sid_event_limiter) == 0


def test_limiter_trims_a_tenth_at_a_time_not_one_key_per_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails against a limiter that trims to exactly max_keys on every new key."""
    limiter = RateLimiter(1, 1000, max_keys=100, clock=Clock())
    trims = 0
    original = limiter._trim

    def counting() -> None:
        nonlocal trims
        trims += 1
        original()

    monkeypatch.setattr(limiter, "_trim", counting)
    for n in range(100):
        limiter.allow(f"live{n}")
    assert trims == 0 and len(limiter) == 100
    for n in range(1000):
        limiter.allow(f"new{n}")
        assert len(limiter) <= 100
    assert 0 < trims <= 1000 // 10
    assert limiter.allow("new999") is False  # recent keys survive
    limiter.discard("new999")
    limiter.discard("never-seen")
    assert limiter.allow("new999")


def test_a_malformed_room_code_in_a_path_takes_no_lock(
    client: TestClient, rooms: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_lock(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a malformed code must not take a lock")

    monkeypatch.setattr(rooms, "lock", no_lock)
    response = client.put(
        "/api/v1/rooms/not%20a%20code/config",
        json={"config": {}},
        headers={"x-host-secret": "x"},
    )
    assert response.status_code == 404
