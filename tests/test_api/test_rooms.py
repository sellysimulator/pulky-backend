"""Black-box tests for the room REST routes (section 14)."""

import asyncio
import copy
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config_models import MSG_CONFIG_LOCKED, GameConfig, config_public
from app.core.defaults import DEFAULT_CONFIG
from app.services.state_service import (
    ROOM_CODE_ALPHABET,
    get_state_service,
    reset_state_service,
)
from tests.conftest import FakeSocketManager, contains_value

API = "/api/v1"
HOST_SID = "sid-host-0001"
FORBIDDEN = "Only the host can do that."
NOT_FOUND = "No room with that code."


@pytest.fixture(autouse=True)
def backend(memory_backend: Any) -> Iterator[Any]:
    reset_state_service()
    yield memory_backend
    reset_state_service()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def create(client: TestClient, **body: Any) -> dict:
    response = client.post(f"{API}/rooms/create", json=body)
    assert response.status_code == 200
    return response.json()


def load(code: str) -> dict:
    room = run(get_state_service().get_room(code))
    assert room is not None
    return room


def store(room: dict) -> None:
    run(get_state_service().save_room(room["room_code"], room))


def mutate(code: str, **changes: Any) -> dict:
    room = load(code)
    room.update(changes)
    store(room)
    return room


def add_players(code: str, n: int, connected: bool = True) -> None:
    room = load(code)
    for i in range(1, n + 1):
        alias = f"P{i}"
        room["participants"][alias] = {
            "alias": alias,
            "identity": f"identity-secret-{i}",
            "session_token": f"token-secret-{i}",
            "display_name": f"Pulquería {alias}",
            "connected": connected,
        }
    store(room)


def secret_headers(secret: str) -> dict[str, str]:
    return {"X-Host-Secret": secret}


# ------------------------------------------------------------------- create


def test_create_room_response(client: TestClient) -> None:
    body = client.post(
        f"{API}/rooms/create", json={"host_display_name": "Ms. Ruiz"}
    ).json()
    assert set(body) == {"room_code", "host_secret", "state", "config"}
    assert body["state"] == "lobby"
    assert re.fullmatch(f"[{ROOM_CODE_ALPHABET}]{{6}}", body["room_code"])
    assert isinstance(body["host_secret"], str) and len(body["host_secret"]) >= 16
    assert body["config"] == GameConfig.default().to_dict() == DEFAULT_CONFIG


def test_create_room_stores_the_room(client: TestClient) -> None:
    body = create(client, host_display_name="Ms. Ruiz")
    room = load(body["room_code"])
    assert room["host_secret"] == body["host_secret"]
    assert room["host_display_name"] == "Ms. Ruiz"
    assert room["state"] == "lobby"
    assert room["config"] == DEFAULT_CONFIG


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("  Ms.\x07 Ruiz  ", "Ms. Ruiz"),
        ("h" * 40, "h" * 24),
        ("", "Host"),
        (None, "Host"),
    ],
)
def test_create_room_sanitises_the_host_name(
    client: TestClient, sent: Any, stored: str
) -> None:
    body = create(client, host_display_name=sent)
    assert load(body["room_code"])["host_display_name"] == stored


def test_create_room_without_a_body_name(client: TestClient) -> None:
    body = create(client)
    assert load(body["room_code"])["host_display_name"] == "Host"


def test_create_room_logs_at_info_without_the_secret(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        body = create(client)
    messages = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
        and r.getMessage() == f"room {body['room_code']} created"
    ]
    assert len(messages) == 1
    assert body["host_secret"] not in caplog.text


def test_two_rooms_have_different_secrets(client: TestClient) -> None:
    a, b = create(client), create(client)
    assert a["host_secret"] != b["host_secret"]
    assert a["room_code"] != b["room_code"]


# ------------------------------------------------------------------- status


def test_status_of_a_missing_room(client: TestClient) -> None:
    response = client.get(f"{API}/rooms/NOPE99/status")
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "reason": "No room with that code.",
        "state": None,
        "host_display_name": None,
        "seats_taken": 0,
        "seats_total": 0,
    }


def test_status_of_a_new_room(client: TestClient) -> None:
    room = create(client, host_display_name="Ms. Ruiz")
    response = client.get(f"{API}/rooms/{room['room_code']}/status")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "state": "lobby",
        "host_display_name": "Ms. Ruiz",
        "seats_taken": 0,
        "seats_total": 4,
    }


def test_status_code_is_case_insensitive(client: TestClient) -> None:
    room = create(client)
    lower = client.get(f"{API}/rooms/{room['room_code'].lower()}/status").json()
    assert lower["ok"] is True
    assert lower["state"] == "lobby"


def test_status_full_room_with_a_disconnected_player(client: TestClient) -> None:
    code = create(client)["room_code"]
    add_players(code, 4)
    room = load(code)
    room["participants"]["P3"]["connected"] = False
    store(room)
    body = client.get(f"{API}/rooms/{code}/status").json()
    assert body["ok"] is False
    assert body["reason"] == "This room is full."
    assert body["seats_taken"] == 4
    assert body["seats_total"] == 4


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("running", "This game has already started."),
        ("finished", "This game has finished."),
    ],
)
def test_status_of_started_and_finished_rooms(
    client: TestClient, state: str, reason: str
) -> None:
    code = create(client)["room_code"]
    add_players(code, 2)
    mutate(code, state=state)
    body = client.get(f"{API}/rooms/{code}/status").json()
    assert body["ok"] is False
    assert body["reason"] == reason
    assert body["state"] == state


def test_status_of_a_configuring_room(client: TestClient) -> None:
    code = create(client)["room_code"]
    mutate(code, state="configuring")
    body = client.get(f"{API}/rooms/{code}/status").json()
    assert body["ok"] is True
    assert body["state"] == "configuring"
    assert "reason" not in body


def test_status_never_contains_the_secret(client: TestClient) -> None:
    room = create(client)
    body = client.get(f"{API}/rooms/{room['room_code']}/status").json()
    assert not contains_value(body, room["host_secret"])
    assert (
        room["host_secret"]
        not in client.get(f"{API}/rooms/{room['room_code']}/status").text
    )


# ----------------------------------------------------- host-secret routes: auth


def test_config_routes_404_for_a_missing_room(client: TestClient) -> None:
    get = client.get(f"{API}/rooms/NOPE99/config", headers=secret_headers("x"))
    put = client.put(
        f"{API}/rooms/NOPE99/config",
        headers=secret_headers("x"),
        json={"config": {}},
    )
    for response in (get, put):
        assert response.status_code == 404
        assert response.json() == {"detail": NOT_FOUND}


def test_config_routes_403_for_a_wrong_or_missing_secret(client: TestClient) -> None:
    code = create(client)["room_code"]
    for headers in ({}, secret_headers("wrong"), secret_headers("")):
        get = client.get(f"{API}/rooms/{code}/config", headers=headers)
        put = client.put(
            f"{API}/rooms/{code}/config",
            headers=headers,
            json={"config": {"starting_cash": 1}},
        )
        for response in (get, put):
            assert response.status_code == 403
            assert response.json() == {"detail": FORBIDDEN}
    assert load(code)["config"] == DEFAULT_CONFIG


def test_missing_room_is_404_even_without_a_secret(client: TestClient) -> None:
    assert client.get(f"{API}/rooms/NOPE99/config").status_code == 404


def test_empty_stored_secret_rejects_missing_and_empty_headers(
    client: TestClient,
) -> None:
    code = create(client)["room_code"]
    mutate(code, host_secret="")
    for headers in ({}, secret_headers("")):
        get = client.get(f"{API}/rooms/{code}/config", headers=headers)
        put = client.put(
            f"{API}/rooms/{code}/config",
            headers=headers,
            json={"config": {"starting_cash": 1}},
        )
        assert get.status_code == 403
        assert put.status_code == 403
        assert put.json() == {"detail": FORBIDDEN}
    assert load(code)["config"] == DEFAULT_CONFIG
    assert load(code)["state"] == "lobby"


# --------------------------------------------------------------- GET config


def test_get_config_returns_the_full_config(client: TestClient) -> None:
    room = create(client)
    response = client.get(
        f"{API}/rooms/{room['room_code']}/config",
        headers=secret_headers(room["host_secret"]),
    )
    assert response.status_code == 200
    assert response.json() == {"config": DEFAULT_CONFIG}
    assert "random_seed" in response.json()["config"]


def test_get_config_code_is_case_insensitive(client: TestClient) -> None:
    room = create(client)
    response = client.get(
        f"{API}/rooms/{room['room_code'].lower()}/config",
        headers=secret_headers(room["host_secret"]),
    )
    assert response.status_code == 200


# --------------------------------------------------------------- PUT config


def put_config(
    client: TestClient, room: dict, patch: dict, secret: str | None = None
) -> Any:
    return client.put(
        f"{API}/rooms/{room['room_code']}/config",
        headers=secret_headers(room["host_secret"] if secret is None else secret),
        json={"config": patch},
    )


def test_put_config_stores_the_merged_config(client: TestClient) -> None:
    room = create(client)
    patch = {"demand": {"retail_price": 35}, "duration_days": 10}
    response = put_config(client, room, patch)
    assert response.status_code == 200
    expected = GameConfig.from_host_input(patch).to_dict()
    assert response.json() == {"config": expected}
    stored = load(room["room_code"])
    assert stored["config"] == expected
    assert stored["state"] == "configuring"
    got = client.get(
        f"{API}/rooms/{room['room_code']}/config",
        headers=secret_headers(room["host_secret"]),
    )
    assert got.json() == {"config": expected}


def test_put_config_patches_merge_across_calls(client: TestClient) -> None:
    room = create(client)
    put_config(client, room, {"duration_days": 10})
    response = put_config(client, room, {"starting_cash": 900})
    config = response.json()["config"]
    assert config["duration_days"] == 10
    assert config["starting_cash"] == 900


def test_put_config_sends_exactly_three_emits_in_order(
    client: TestClient, fake_socket_manager: FakeSocketManager
) -> None:
    room = create(client)
    code = room["room_code"]
    mutate(code, host_sid=HOST_SID)
    add_players(code, 2)
    seq_before = load(code)["seq"]
    patch = {"demand": {"retail_price": 35}}
    response = put_config(client, room, patch)
    assert response.status_code == 200

    full = GameConfig.from_host_input(patch).to_dict()
    public = config_public(GameConfig.from_dict(full))
    records = fake_socket_manager.records
    assert [(r[0], r[1], r[3]) for r in records] == [
        (HOST_SID, "config_updated", None),
        (code, "config_updated", HOST_SID),
        (code, "lobby_update", None),
    ]
    host, shared, lobby = (r[2] for r in records)
    assert host == {"seq": seq_before + 1, "config": full}
    assert shared == {"seq": seq_before + 1, "config": public}
    assert lobby["seq"] == seq_before + 2
    assert lobby["state"] == "configuring"
    assert lobby["can_start"] is True
    assert [p["alias"] for p in lobby["participants"]] == ["P1", "P2"]
    assert load(code)["seq"] == seq_before + 2


def test_put_config_room_emit_is_redacted(
    client: TestClient, fake_socket_manager: FakeSocketManager
) -> None:
    room = create(client)
    mutate(room["room_code"], host_sid=HOST_SID)
    put_config(client, room, {"starting_cash": 100})
    shared = fake_socket_manager.records[1]
    assert shared[3] == HOST_SID
    assert set(shared[2]["config"]) == {"demand", "stock", "peer_market", "transport"}
    assert not contains_value(shared[2], room["host_secret"])
    for record in fake_socket_manager.records:
        assert not contains_value(record[2], room["host_secret"])


def test_put_config_without_a_host_sid_sends_no_host_emit(
    client: TestClient, fake_socket_manager: FakeSocketManager
) -> None:
    room = create(client)
    put_config(client, room, {"starting_cash": 100})
    assert [(r[1], r[3]) for r in fake_socket_manager.records] == [
        ("config_updated", None),
        ("lobby_update", None),
    ]


def test_put_config_invalid_patch_answers_400(
    client: TestClient, fake_socket_manager: FakeSocketManager
) -> None:
    room = create(client)
    mutate(room["room_code"], host_sid=HOST_SID)
    before = load(room["room_code"])
    response = put_config(client, room, {"stock": {"max_days": 1}})
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Must be more than the fresh days (1).",
        "field": "stock.max_days",
    }
    assert load(room["room_code"]) == before
    assert fake_socket_manager.records == []


def test_put_config_reports_the_first_failure_only(client: TestClient) -> None:
    room = create(client)
    response = put_config(client, room, {"max_players": 1, "duration_days": 100})
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Must be between 2 and 12.",
        "field": "max_players",
    }


def test_put_config_unknown_key(client: TestClient) -> None:
    room = create(client)
    response = put_config(client, room, {"bogus": 1})
    assert response.status_code == 400
    assert response.json() == {"detail": "Unknown setting.", "field": "bogus"}
    assert load(room["room_code"])["state"] == "lobby"


@pytest.mark.parametrize("state", ["running", "finished"])
def test_put_config_on_a_locked_room_answers_409(
    client: TestClient, fake_socket_manager: FakeSocketManager, state: str
) -> None:
    room = create(client)
    code = room["room_code"]
    mutate(code, host_sid=HOST_SID, state=state)
    before = load(code)
    response = put_config(client, room, {"starting_cash": 1})
    assert response.status_code == 409
    assert response.json() == {"detail": MSG_CONFIG_LOCKED}
    assert MSG_CONFIG_LOCKED == "The settings are locked once the game starts."
    assert load(code) == before
    assert load(code)["config"] == DEFAULT_CONFIG
    assert fake_socket_manager.records == []


def test_locked_room_with_an_invalid_patch_is_still_409(client: TestClient) -> None:
    room = create(client)
    mutate(room["room_code"], state="running")
    response = put_config(client, room, {"bogus": 1})
    assert response.status_code == 409


def test_put_config_requires_the_secret_before_anything_else(
    client: TestClient, fake_socket_manager: FakeSocketManager
) -> None:
    room = create(client)
    mutate(room["room_code"], state="running")
    response = put_config(client, room, {"bogus": 1}, secret="wrong")
    assert response.status_code == 403
    assert fake_socket_manager.records == []


def test_put_config_holds_the_room_lock_while_saving(
    client: TestClient, fake_socket_manager: FakeSocketManager
) -> None:
    room = create(client)
    # sequential requests must not deadlock on the lock and both must succeed
    assert put_config(client, room, {"starting_cash": 1}).status_code == 200
    assert put_config(client, room, {"starting_cash": 2}).status_code == 200
    assert load(room["room_code"])["config"]["starting_cash"] == 2


# ------------------------------------------------------- the secret, once only


def test_the_secret_appears_only_in_the_create_response(
    client: TestClient, fake_socket_manager: FakeSocketManager
) -> None:
    room = create(client)
    code, secret = room["room_code"], room["host_secret"]
    mutate(code, host_sid=HOST_SID)
    add_players(code, 2)
    headers = secret_headers(secret)
    responses = [
        client.get(f"{API}/rooms/{code}/status"),
        client.get(f"{API}/rooms/{code}/config", headers=headers),
        put_config(client, room, {"duration_days": 12}),
        put_config(client, room, {"stock": {"max_days": 1}}),
        client.get(f"{API}/rooms/{code}/config", headers=secret_headers("wrong")),
        client.get(f"{API}/rooms/NOPE99/status"),
        client.get(f"{API}/config/defaults"),
        client.post(
            f"{API}/config/validate", json={"config": copy.deepcopy(DEFAULT_CONFIG)}
        ),
    ]
    for response in responses:
        assert secret not in response.text
        body = response.json()
        assert not contains_value(body, secret)
        assert "host_secret" not in response.text
    mutate(code, state="running")
    locked = put_config(client, room, {"duration_days": 9})
    assert secret not in locked.text
