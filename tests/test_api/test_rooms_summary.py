"""POST /api/v1/rooms/summary: what the multi-game host dashboard polls.

Read-only, no oracle for which rooms exist, and never a secret in the answer
(spec 11.3, 13.4, 0.6)."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.limits import MAX_BATCH_ROOMS, MIN_PLAYERS_TO_START
from app.services import game_service, room_service
from app.services.state_service import get_state_service, reset_state_service
from tests.conftest import contains_key, contains_value

API = "/api/v1"
SUMMARY_KEYS = {
    "room_code",
    "exists",
    "state",
    "seats_taken",
    "seats_total",
    "day",
    "duration_days",
    "finished",
    "can_start",
    "start_blocked_reason",
}


@pytest.fixture(autouse=True)
def backend(memory_backend: Any) -> Iterator[Any]:
    reset_state_service()
    yield memory_backend
    reset_state_service()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_rooms(client: TestClient, count: int = 2) -> list[dict]:
    r = client.post(
        f"{API}/rooms/batch-create",
        json={"count": count, "config": {"max_players": 4, "duration_days": 8}},
    )
    assert r.status_code == 200, r.text
    rooms: list[dict] = r.json()["rooms"]
    return rooms


def summary(client: TestClient, pairs: list[tuple[str, str]]) -> Any:
    return client.post(
        f"{API}/rooms/summary",
        json={"rooms": [{"room_code": c, "host_secret": s} for c, s in pairs]},
    )


def add_players(code: str, n: int) -> None:
    service = get_state_service()
    room = run(service.get_room(code))
    assert room is not None
    for i in range(1, n + 1):
        alias = f"P{i}"
        room["participants"][alias] = {
            "alias": alias,
            "identity": f"identity-{i}",
            "session_token": f"token-{i}",
            "display_name": f"Pulquería {alias}",
            "connected": True,
        }
    run(service.save_room(code, room))


def start(code: str) -> None:
    service = get_state_service()
    room = run(service.get_room(code))
    assert room is not None
    game_service.start(room, service.next_seq)
    run(service.save_room(code, room))


def test_summary_reports_seats_and_can_start(client: TestClient) -> None:
    a, b = make_rooms(client)
    add_players(a["room_code"], MIN_PLAYERS_TO_START)
    add_players(b["room_code"], 1)

    r = summary(
        client,
        [(a["room_code"], a["host_secret"]), (b["room_code"], b["host_secret"])],
    )
    assert r.status_code == 200, r.text
    first, second = r.json()["rooms"]

    assert first == {
        "room_code": a["room_code"],
        "exists": True,
        "state": "lobby",
        "seats_taken": MIN_PLAYERS_TO_START,
        "seats_total": 4,
        "day": 0,
        "duration_days": 8,
        "finished": False,
        "can_start": True,
        "start_blocked_reason": None,
    }
    assert second["room_code"] == b["room_code"]
    assert second["seats_taken"] == 1
    assert second["can_start"] is False
    assert second["start_blocked_reason"] == room_service.START_NEED_PLAYERS


def test_can_start_is_the_lobby_rule(client: TestClient) -> None:
    """The dashboard's flag is `room_service.can_start`, the one `start_game` checks."""
    room, _ = make_rooms(client)
    for n in (0, 1, 2, 3):
        add_players(room["room_code"], n)
        (got,) = summary(client, [(room["room_code"], room["host_secret"])]).json()[
            "rooms"
        ]
        stored = run(get_state_service().get_room(room["room_code"]))
        assert stored is not None
        ok, reason = room_service.can_start(stored)
        assert (got["can_start"], got["start_blocked_reason"]) == (ok, reason)


def test_a_started_room_reports_its_day_and_cannot_start_again(
    client: TestClient,
) -> None:
    room, _ = make_rooms(client)
    add_players(room["room_code"], 2)
    start(room["room_code"])

    (got,) = summary(client, [(room["room_code"], room["host_secret"])]).json()["rooms"]
    assert got["state"] == "running"
    assert got["day"] == 1
    assert got["duration_days"] == 8
    assert got["finished"] is False
    assert got["can_start"] is False
    assert got["start_blocked_reason"] == room_service.START_ALREADY


def test_a_finished_room_says_so(client: TestClient) -> None:
    room, _ = make_rooms(client)
    add_players(room["room_code"], 2)
    start(room["room_code"])
    service = get_state_service()
    stored = run(service.get_room(room["room_code"]))
    assert stored is not None
    stored["state"] = "finished"
    run(service.save_room(room["room_code"], stored))

    (got,) = summary(client, [(room["room_code"], room["host_secret"])]).json()["rooms"]
    assert got["finished"] is True
    assert got["can_start"] is False


def test_wrong_secret_and_missing_room_look_the_same(client: TestClient) -> None:
    a, b = make_rooms(client)
    rooms = summary(
        client,
        [
            (a["room_code"], "wrong"),
            (b["room_code"], ""),
            ("ZZZZZZ", "anything"),
            (a["room_code"], b["host_secret"]),
            (a["room_code"], "ñ-not-ascii"),
            ("bad code!", a["host_secret"]),
        ],
    ).json()["rooms"]

    assert len(rooms) == 6
    for got in rooms:
        assert set(got) == SUMMARY_KEYS
        assert got["exists"] is False
        assert all(
            v is None for k, v in got.items() if k not in ("room_code", "exists")
        )


def test_a_room_with_no_stored_secret_fails_closed(client: TestClient) -> None:
    room, _ = make_rooms(client)
    service = get_state_service()
    stored = run(service.get_room(room["room_code"]))
    assert stored is not None
    stored["host_secret"] = ""
    run(service.save_room(room["room_code"], stored))

    (got,) = summary(client, [(room["room_code"], "")]).json()["rooms"]
    assert got["exists"] is False


def test_answers_one_entry_per_input_in_order(client: TestClient) -> None:
    rooms = make_rooms(client, count=3)
    pairs = [(r["room_code"], r["host_secret"]) for r in reversed(rooms)]
    pairs.insert(1, ("ZZZZZZ", "x"))
    got = summary(client, pairs).json()["rooms"]
    assert [g["room_code"] for g in got] == [c for c, _ in pairs]
    assert [g["exists"] for g in got] == [True, False, True, True]


def test_summary_never_contains_a_host_secret(client: TestClient) -> None:
    a, b = make_rooms(client)
    add_players(a["room_code"], 2)
    r = summary(
        client,
        [(a["room_code"], a["host_secret"]), (b["room_code"], "wrong")],
    )
    body = r.json()
    assert a["host_secret"] not in r.text
    assert b["host_secret"] not in r.text
    assert not contains_key(body, "host_secret")
    assert not contains_value(body, a["host_secret"])
    # Nor a player's identity or session token.
    assert "identity-1" not in r.text and "token-1" not in r.text


def test_summary_is_read_only(
    client: TestClient, backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling must not extend an unstarted room's lifetime: no save, no lock."""
    room, _ = make_rooms(client)
    calls: list[str] = []

    async def record_setex(*args: Any, **kwargs: Any) -> None:
        calls.append("setex")

    async def record_set(*args: Any, **kwargs: Any) -> None:
        calls.append("set")

    def record_lock(*args: Any, **kwargs: Any) -> None:
        calls.append("lock")

    monkeypatch.setattr(backend, "setex", record_setex)
    monkeypatch.setattr(backend, "set", record_set)
    monkeypatch.setattr(backend, "lock", record_lock)
    r = summary(client, [(room["room_code"], room["host_secret"])])
    assert r.status_code == 200
    assert calls == []


def test_summary_request_bounds(client: TestClient) -> None:
    assert client.post(f"{API}/rooms/summary", json={"rooms": []}).status_code == 422
    too_many = [("ZZZZZZ", "s")] * (MAX_BATCH_ROOMS + 1)
    assert summary(client, too_many).status_code == 422
    assert summary(client, [("Z" * 17, "s")]).status_code == 422
    assert summary(client, [("ZZZZZZ", "s" * 129)]).status_code == 422
    assert client.post(f"{API}/rooms/summary", json={}).status_code == 422


def test_summary_is_not_a_room_lookup_or_a_create(client: TestClient) -> None:
    from app.core import abuse

    room, _ = make_rooms(client)
    before = len(abuse.lookup_limiter)
    summary(client, [(room["room_code"], room["host_secret"])])
    assert len(abuse.lookup_limiter) == before
