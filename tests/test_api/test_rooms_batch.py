"""POST /api/v1/rooms/batch-create: N rooms with one config, made at once for the
multi-game host dashboard (spec 11.3, 13.4, 0.6)."""

import asyncio
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import abuse
from app.core.config_models import host_default_config
from app.core.limits import MAX_BATCH_ROOMS, MIN_BATCH_ROOMS
from app.services import state_service
from app.services.state_service import (
    ROOM_CODE_ALPHABET,
    get_state_service,
    reset_state_service,
)

API = "/api/v1"
TOO_MANY_ROOMS = "Too many games are open right now. Try again in a few minutes."


@pytest.fixture(autouse=True)
def backend(memory_backend: Any) -> Iterator[Any]:
    reset_state_service()
    yield memory_backend
    reset_state_service()


def batch(client: TestClient, count: int = 3, config: Any = None, **extra: Any) -> Any:
    body = {"count": count, "config": config if config is not None else {}, **extra}
    return client.post(f"{API}/rooms/batch-create", json=body)


def stored_rooms(backend: Any) -> dict[str, dict]:
    return {
        key.removeprefix("room:"): json.loads(value)
        for key, (_, value) in backend._store.items()
        if key.startswith("room:")
    }


def create_limit(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    limiter = abuse.RateLimiter(limit, 3600.0)
    monkeypatch.setitem(abuse.route_limiters, abuse.CREATE_ROUTE, limiter)


def test_batch_creates_count_rooms_with_the_config_applied(
    client: TestClient, backend: Any
) -> None:
    r = batch(client, count=4, config={"max_players": 3, "duration_days": 9})
    assert r.status_code == 200, r.text
    rooms = r.json()["rooms"]

    assert len(rooms) == 4
    assert len({room["room_code"] for room in rooms}) == 4
    assert len({room["host_secret"] for room in rooms}) == 4
    stored = stored_rooms(backend)
    for room in rooms:
        assert set(room) == {"room_code", "host_secret", "state", "config"}
        assert re.fullmatch(f"[{ROOM_CODE_ALPHABET}]{{6}}", room["room_code"])
        assert room["state"] == "lobby"
        assert room["config"]["max_players"] == 3
        assert room["config"]["duration_days"] == 9
        saved = stored[room["room_code"]]
        assert saved["config"]["max_players"] == 3
        assert saved["host_secret"] == room["host_secret"]
        assert saved["participants"] == {}


def test_batch_keeps_the_hidden_defaults_null(client: TestClient) -> None:
    rooms = batch(client, count=2, config={"duration_days": 10}).json()["rooms"]
    for room in rooms:
        assert room["config"]["transport"]["failure_probability"] is None


def test_batch_rooms_do_not_share_a_config(client: TestClient, backend: Any) -> None:
    rooms = batch(client, count=2, config={"duration_days": 9}).json()["rooms"]
    first, second = rooms
    r = client.put(
        f"{API}/rooms/{first['room_code']}/config",
        json={"config": {"duration_days": 12}},
        headers={"X-Host-Secret": first["host_secret"]},
    )
    assert r.status_code == 200, r.text
    stored = stored_rooms(backend)
    assert stored[first["room_code"]]["config"]["duration_days"] == 12
    assert stored[second["room_code"]]["config"]["duration_days"] == 9


def test_batch_with_an_empty_config_uses_the_defaults(client: TestClient) -> None:
    single = client.post(f"{API}/rooms/create", json={}).json()["config"]
    rooms = batch(client, count=2).json()["rooms"]
    assert single == host_default_config()
    assert all(room["config"] == single for room in rooms)


def test_batch_names_the_host_like_create(client: TestClient, backend: Any) -> None:
    rooms = batch(client, count=2, host_display_name="  Ms.\x07 Ruiz  ").json()
    for room in rooms["rooms"]:
        assert stored_rooms(backend)[room["room_code"]]["host_display_name"] == (
            "Ms. Ruiz"
        )
    unnamed = batch(client, count=2).json()["rooms"][0]["room_code"]
    assert stored_rooms(backend)[unnamed]["host_display_name"] == "Host"


@pytest.mark.parametrize(
    "config",
    [
        {"max_players": 999},
        {"no_such_setting": 1},
        {"demand": {"weekday_peak": {"from": "fri", "to": "mon"}}},
    ],
)
def test_a_bad_config_is_the_config_save_400_and_creates_nothing(
    client: TestClient, backend: Any, config: dict
) -> None:
    r = batch(client, count=3, config=config)
    assert r.status_code == 400
    body = r.json()
    assert set(body) == {"detail", "field"}
    assert isinstance(body["detail"], str) and body["detail"]
    assert stored_rooms(backend) == {}

    # The same patch saved in a lobby gets the same answer.
    room = client.post(f"{API}/rooms/create", json={}).json()
    saved = client.put(
        f"{API}/rooms/{room['room_code']}/config",
        json={"config": config},
        headers={"X-Host-Secret": room["host_secret"]},
    )
    assert (saved.status_code, saved.json()) == (400, body)


def test_a_bad_config_does_not_use_up_the_create_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_limit(monkeypatch, 3)
    assert batch(client, count=3, config={"max_players": 999}).status_code == 400
    assert batch(client, count=3).status_code == 200


def test_count_outside_the_bounds_is_a_422(client: TestClient, backend: Any) -> None:
    assert batch(client, count=MIN_BATCH_ROOMS - 1).status_code == 422
    assert batch(client, count=MAX_BATCH_ROOMS + 1).status_code == 422
    assert client.post(f"{API}/rooms/batch-create", json={}).status_code == 422
    assert stored_rooms(backend) == {}


def test_each_room_counts_against_the_create_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    create_limit(monkeypatch, 5)
    origin = {"Origin": "https://pulkysim.web.app"}

    assert batch(client, count=3).status_code == 200
    r = client.post(
        f"{API}/rooms/batch-create", json={"count": 3, "config": {}}, headers=origin
    )  # 3 + 3 > 5: all or nothing
    assert r.status_code == 429
    assert r.json() == {"detail": abuse.TOO_MANY_REQUESTS}
    assert r.headers["retry-after"] == "3600"
    assert "Retry-After" in r.headers["access-control-expose-headers"]
    assert len(stored_rooms(backend)) == 3

    # The refused batch used none of what was left.
    assert batch(client, count=2).status_code == 200
    assert client.post(f"{API}/rooms/create", json={}).status_code == 429


def test_single_creates_and_batches_share_one_budget(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_limit(monkeypatch, 3)
    assert client.post(f"{API}/rooms/create", json={}).status_code == 200
    assert client.post(f"{API}/rooms/create", json={}).status_code == 200
    assert batch(client, count=2).status_code == 429


def test_the_middleware_leaves_batch_create_on_the_rest_limit(
    client: TestClient,
) -> None:
    """Only the route charges the create budget; a batch is not also one create."""
    limit = abuse.ROUTE_LIMITS[abuse.CREATE_ROUTE]
    assert "POST /api/v1/rooms/batch-create" not in abuse.ROUTE_LIMITS
    assert batch(client, count=limit).status_code == 200
    assert batch(client, count=2).status_code == 429


def test_batch_takes_no_custom_room_code(client: TestClient) -> None:
    r = batch(client, count=2, room_code="MINE22")
    assert r.status_code == 200
    assert all(room["room_code"] != "MINE22" for room in r.json()["rooms"])


def test_a_batch_that_cannot_fit_under_the_room_cap_is_refused_whole(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    monkeypatch.setattr(state_service, "MAX_LIVE_ROOMS", 4)
    create_limit(monkeypatch, 10)
    assert client.post(f"{API}/rooms/create", json={}).status_code == 200
    assert client.post(f"{API}/rooms/create", json={}).status_code == 200

    r = batch(client, count=3)
    assert r.status_code == 503
    assert r.json() == {"detail": TOO_MANY_ROOMS}
    assert len(stored_rooms(backend)) == 2
    # Refused before it was charged: the 8 creations left are all still there.
    assert batch(client, count=2).status_code == 200
    assert len(stored_rooms(backend)) == 4


def test_the_room_cap_reached_partway_deletes_the_rooms_made(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    """Another create can take the last places between the check and the batch."""
    real_create = state_service.StateService.create_room
    made = 0

    async def create_twice(self: Any, config: dict, name: str) -> dict:
        nonlocal made
        if made == 2:
            raise state_service.RoomLimitReached
        made += 1
        return await real_create(self, config, name)

    monkeypatch.setattr(state_service.StateService, "create_room", create_twice)
    r = batch(client, count=4)
    assert r.status_code == 503
    assert r.json() == {"detail": TOO_MANY_ROOMS}
    assert made == 2
    assert stored_rooms(backend) == {}


def test_any_failure_partway_deletes_the_rooms_made(
    monkeypatch: pytest.MonkeyPatch, backend: Any
) -> None:
    from app.main import app

    real_create = state_service.StateService.create_room
    calls = 0

    async def fail_third(self: Any, config: dict, name: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("Could not find a free room code.")
        return await real_create(self, config, name)

    monkeypatch.setattr(state_service.StateService, "create_room", fail_third)
    r = TestClient(app, raise_server_exceptions=False).post(
        f"{API}/rooms/batch-create", json={"count": 4}
    )
    assert r.status_code == 500
    assert stored_rooms(backend) == {}


def test_the_rooms_are_real_rooms(client: TestClient) -> None:
    rooms = batch(client, count=2).json()["rooms"]
    for room in rooms:
        loaded = asyncio.run(get_state_service().get_room(room["room_code"]))
        assert loaded is not None and loaded["host_secret"] == room["host_secret"]
        status = client.get(f"{API}/rooms/{room['room_code']}/status").json()
        assert status["ok"] is True and status["seats_taken"] == 0
