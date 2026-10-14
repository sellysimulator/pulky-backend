"""AC16: every shared fixture exists and works; the payload helpers see any depth."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    FakeRedis,
    FakeSocketManager,
    FirebaseTokens,
    contains_key,
    contains_value,
)

PAYLOAD = {
    "a": 1,
    "b": [{"c": {"secret": "s3"}}, [{"deep": [None, "needle"]}]],
    "d": {"e": {"f": [1, 2, {"g": 7}]}},
}


@pytest.mark.parametrize("key", ["a", "c", "secret", "deep", "e", "g"])
def test_contains_key_finds_keys_at_any_depth(key: str) -> None:
    assert contains_key(PAYLOAD, key) is True


@pytest.mark.parametrize("key", ["s3", "needle", "missing", "1"])
def test_contains_key_does_not_confuse_values_with_keys(key: str) -> None:
    assert contains_key(PAYLOAD, key) is False


@pytest.mark.parametrize("value", [1, "s3", "needle", 7, 2])
def test_contains_value_finds_values_at_any_depth(value: object) -> None:
    assert contains_value(PAYLOAD, value) is True


@pytest.mark.parametrize("value", ["secret", "deep", "missing", 99])
def test_contains_value_does_not_confuse_keys_with_values(value: object) -> None:
    assert contains_value(PAYLOAD, value) is False


def test_helpers_on_scalars_and_empties() -> None:
    assert contains_key({}, "a") is False
    assert contains_key([], "a") is False
    assert contains_value("needle", "needle") is False  # a bare scalar holds nothing
    assert contains_value([], "x") is False


def test_firebase_tokens_is_a_dict_subclass(firebase_tokens: FirebaseTokens) -> None:
    assert isinstance(firebase_tokens, dict)
    assert firebase_tokens == {}


def test_fake_redis_fixture_is_a_fakeredis(fake_redis: FakeRedis) -> None:
    assert isinstance(fake_redis, FakeRedis)


@pytest.mark.asyncio
async def test_fake_socket_manager_records_and_filters_emits(
    fake_socket_manager: FakeSocketManager,
) -> None:
    from app.sockets.manager import get_socket_manager

    mgr = get_socket_manager()
    await mgr.emit_to_room("ABC123", "day_closed", {"seq": 1}, skip_sid="s9")
    await mgr.emit_to_sid("s1", "error", {"code": "X"})
    assert fake_socket_manager.emits("day_closed") == [
        ("ABC123", "day_closed", {"seq": 1}, "s9")
    ]
    assert fake_socket_manager.emits_to("s1") == [("s1", "error", {"code": "X"}, None)]
    assert fake_socket_manager.emits("nothing") == []
    await mgr.join_room("s1", "ABC123", "P1")
    assert mgr.sid_to_alias["s1"] == ("ABC123", "P1")


def test_client_fixtures_serve_the_app(
    client: TestClient, lifespan_client: TestClient
) -> None:
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert lifespan_client.get("/api/v1/health").json() == {"status": "ok"}
