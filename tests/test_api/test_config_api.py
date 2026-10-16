"""Black-box tests for the config defaults and dry-run validation routes."""

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config_models import GameConfig, config_schema, validate_config
from app.core.defaults import DEFAULT_CONFIG
from app.services import state_service

DEFAULTS = "/api/v1/config/defaults"
VALIDATE = "/api/v1/config/validate"


def full_config(**patch: Any) -> dict:
    """The default config with a nested patch applied."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    for path, value in patch.items():
        node = config
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return config


def issues(items: list) -> list[dict[str, str | None]]:
    return [{"field": i.field, "message": i.message} for i in items]


def test_defaults_equal_config_schema(client: TestClient) -> None:
    response = client.get(DEFAULTS)
    assert response.status_code == 200
    assert response.json() == config_schema()


def test_defaults_carry_levels_and_limits(client: TestClient) -> None:
    body = client.get(DEFAULTS).json()
    assert body["config"] == DEFAULT_CONFIG
    assert body["fields"]["demand.retail_price"]["players"] == "shown"
    assert body["fields"]["random_seed"]["players"] == "secret"
    assert body["limits"]["MAX_PLAYERS"] == 12


def test_validate_default_config_is_ok(client: TestClient) -> None:
    response = client.post(VALIDATE, json={"config": full_config()})
    assert response.status_code == 200
    result = validate_config(full_config(), None)
    assert response.json() == {
        "ok": True,
        "errors": [],
        "warnings": issues(result.warnings),
    }


def test_validate_returns_the_documented_example_error(client: TestClient) -> None:
    config = full_config(stock__fresh_days=3)
    body = client.post(VALIDATE, json={"config": config}).json()
    assert body["ok"] is False
    assert body["errors"] == [
        {"field": "stock.max_days", "message": "Must be more than the fresh days (3)."}
    ]
    assert body["warnings"] == []


def test_validate_returns_every_error_in_order(client: TestClient) -> None:
    config = full_config(
        max_players=1, duration_days=100, demand__retail_price=0, stock__fresh_days=3
    )
    body = client.post(VALIDATE, json={"config": config}).json()
    expected = validate_config(config, None)
    assert len(expected.errors) >= 4
    assert body["ok"] is False
    assert body["errors"] == issues(expected.errors)
    fields = [e["field"] for e in body["errors"]]
    assert fields[:3] == ["max_players", "duration_days", "demand.retail_price"]
    assert body["errors"][0]["message"] == "Must be between 2 and 12."
    assert body["errors"][1]["message"] == "Must be between 7 and 60."
    assert body["errors"][2]["message"] == "Must be more than 0 and at most 10,000."
    for entry in body["errors"]:
        assert set(entry) == {"field", "message"}
        assert entry["message"]


def test_validate_with_errors_has_no_warnings(client: TestClient) -> None:
    config = full_config(demand__retail_price=1, max_players=1)
    body = client.post(VALIDATE, json={"config": config}).json()
    assert body["ok"] is False
    assert body["warnings"] == []


def test_validate_returns_warnings_without_errors(client: TestClient) -> None:
    config = full_config(demand__retail_price=5)
    body = client.post(VALIDATE, json={"config": config}).json()
    assert body["ok"] is True
    assert body["errors"] == []
    assert {
        "field": "demand.retail_price",
        "message": "On the dearest spot days a unit can lose money.",
    } in body["warnings"]
    expected = validate_config(config, None)
    assert body["warnings"] == issues(expected.warnings)


def test_validate_insurance_fee_warning(client: TestClient) -> None:
    config = full_config(insurance__tiers={"cip": {"enabled": True, "fee": 9000.0}})
    config["insurance"]["tiers"]["dap"] = {"enabled": True, "fee": 0.4}
    config["insurance"]["tiers"]["ddp"] = {"enabled": True, "fee": 0.99}
    body = client.post(VALIDATE, json={"config": config}).json()
    assert body["ok"] is True
    cip = [w for w in body["warnings"] if w["field"] == "insurance.tiers.cip.fee"]
    assert len(cip) == 1
    assert cip[0]["message"].startswith("Far from break-even (ratio ")
    assert body["warnings"] == issues(validate_config(config, None).warnings)


def test_validate_unknown_key(client: TestClient) -> None:
    config = full_config()
    config["bogus"] = 1
    body = client.post(VALIDATE, json={"config": config}).json()
    assert body["ok"] is False
    assert body["errors"][0] == {"field": "bogus", "message": "Unknown setting."}


def test_validate_ok_matches_error_count(client: TestClient) -> None:
    good = client.post(VALIDATE, json={"config": full_config()}).json()
    bad = client.post(VALIDATE, json={"config": full_config(starting_cash=-1)}).json()
    assert good["ok"] is (len(good["errors"]) == 0) is True
    assert bad["ok"] is (len(bad["errors"]) == 0) is False


def test_validate_does_not_touch_rooms(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("validate must not touch the state service")

    monkeypatch.setattr(state_service, "get_state_service", boom)
    assert client.post(VALIDATE, json={"config": full_config()}).status_code == 200
    assert client.get(DEFAULTS).status_code == 200


def test_validate_result_is_the_merge_of_nothing(client: TestClient) -> None:
    # a partial config is validated against the defaults, never against a room
    body = client.post(VALIDATE, json={"config": {"stock": {"fresh_days": 3}}}).json()
    assert body["errors"] == [
        {"field": "stock.max_days", "message": "Must be more than the fresh days (3)."}
    ]
    assert GameConfig.default().to_dict() == DEFAULT_CONFIG
