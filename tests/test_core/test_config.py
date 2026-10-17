"""Black-box tests for app.core.defaults and config_models (plan 04, spec 9)."""

import ast
import copy
import importlib.util
import json
import sys
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.config_models import (
    FIELD_SPECS,
    MSG_CONFIG_LOCKED,
    SECRET_KEYS,
    ConfigError,
    DemandConfig,
    FieldIssue,
    FieldSpec,
    FuturesConfig,
    GameConfig,
    InsuranceConfig,
    InsuredTierConfig,
    InsuredTiersConfig,
    PeakWindow,
    PeerMarketConfig,
    ReputationConfig,
    StockConfig,
    SupplierConfig,
    TransportConfig,
    ValidationResult,
    config_public,
    config_schema,
    config_warnings,
    validate_config,
)
from app.core.defaults import DEFAULT_CONFIG, PLAYER_CONFIG_KEYS
from app.core.enums import IncotermTier
from app.core.insurance import InsuredTierTerms, break_even_fee, fee_ratio
from app.core.limits import (
    LIMITS,
    MAX_DAYS,
    MAX_HORIZON,
    MAX_LAMBDA,
    MAX_LEAD_TIME,
    MAX_MONEY,
    MAX_PLAYERS,
    MAX_SHELF_DAYS,
    MIN_DAYS,
)
from app.core.money import format_money, format_number

CIP, DAP, DDP = IncotermTier.CIP, IncotermTier.DAP, IncotermTier.DDP

# --------------------------------------------------------------------------- #
# Helpers and the spec's tables, written out independently of the code.
# --------------------------------------------------------------------------- #

SPEC_DEFAULTS: dict[str, Any] = {
    "max_players": 4,
    "duration_days": 14,
    "starting_cash": 5000,
    "random_seed": None,
    "demand": {
        "per_player_peak": 25,
        "weekday_peak": {"from": "thu", "to": "sat"},
        "retail_price": 30,
        "backlog_penalty": 1.5,
    },
    "reputation": {"starting": 0.5, "smoothing": 0.3, "waste_weight": 0.3},
    "supplier": {
        "base_price": 12,
        "spot_spread": 0.25,
        "capacity_mean_per_player": 22,
        "capacity_sd_per_player": 5,
        "disruption_probability": 0.05,
        "disruption_factor": 0.4,
        "lead_time_days": 1,
        "initial_pipeline_qty": 0,
        "quality_min": 80,
        "quality_max": 100,
    },
    "futures": {"horizon_days": 7},
    "stock": {
        "fresh_days": 0,
        "max_days": 3,
        "holding_cost": 0.2,
        "initial_stock": 30,
        "salvage_value": 2,
        "conservation_extra_fresh_days": 2,
        "conservation_cost_per_unit": 1.0,
    },
    "peer_market": {"enabled": True},
    "transport": {
        "unit_cost": 1.0,
        "shared": False,
        "failure_probability": 0.14,
        "loss_fraction": 0.30,
    },
    "insurance": {
        "tiers": {
            "cip": {"enabled": True, "fee": 0.55},
            "dap": {"enabled": True, "fee": 0.55},
            "ddp": {"enabled": True, "fee": 1.39},
        },
        "target_margin": 0.10,
        "reference_shipment": 20,
    },
}

SEED_MAX = 2**53 - 1

# (path, type, min, max, min_exclusive, nullable, players): plan 04 section 3.1
EXPECTED_SPECS: list[tuple[str, str, Any, Any, bool, bool, str]] = [
    ("max_players", "int", 2, MAX_PLAYERS, False, False, "not_shown"),
    ("duration_days", "int", MIN_DAYS, MAX_DAYS, False, False, "not_shown"),
    ("starting_cash", "float", 0, MAX_MONEY, False, False, "not_shown"),
    ("random_seed", "int", 0, SEED_MAX, False, True, "secret"),
    ("demand.per_player_peak", "float", 1, MAX_LAMBDA, False, False, "not_shown"),
    ("demand.weekday_peak.from", "weekday", None, None, False, False, "not_shown"),
    ("demand.weekday_peak.to", "weekday", None, None, False, False, "not_shown"),
    ("demand.retail_price", "float", 0, 10000, True, False, "shown"),
    ("demand.backlog_penalty", "float", 0, 10000, False, False, "not_shown"),
    ("reputation.starting", "float", 0, 1, False, False, "not_shown"),
    ("reputation.smoothing", "float", 0, 1, False, False, "not_shown"),
    ("reputation.waste_weight", "float", 0, 1, False, False, "not_shown"),
    ("supplier.base_price", "float", 0, 10000, True, False, "not_shown"),
    ("supplier.spot_spread", "float", 0, 0.9, False, False, "not_shown"),
    ("supplier.capacity_mean_per_player", "float", 0, 9999, False, False, "not_shown"),
    ("supplier.capacity_sd_per_player", "float", 0, 9999, False, False, "secret"),
    ("supplier.disruption_probability", "float", 0, 1, False, False, "secret"),
    ("supplier.disruption_factor", "float", 0, 1, False, False, "not_shown"),
    ("supplier.lead_time_days", "int", 0, MAX_LEAD_TIME, False, False, "not_shown"),
    ("supplier.initial_pipeline_qty", "int", 0, 9999, False, False, "not_shown"),
    ("supplier.quality_min", "int", 1, 100, False, False, "not_shown"),
    ("supplier.quality_max", "int", 1, 100, False, False, "not_shown"),
    ("futures.horizon_days", "int", 1, MAX_HORIZON, False, False, "not_shown"),
    ("stock.fresh_days", "int", 0, MAX_SHELF_DAYS, False, False, "not_shown"),
    ("stock.max_days", "int", 1, MAX_SHELF_DAYS, False, False, "not_shown"),
    ("stock.holding_cost", "float", 0, 10000, False, False, "not_shown"),
    ("stock.initial_stock", "int", 0, 9999, False, False, "not_shown"),
    ("stock.salvage_value", "float", 0, 10000, False, False, "not_shown"),
    (
        "stock.conservation_extra_fresh_days",
        "int",
        1,
        MAX_SHELF_DAYS,
        False,
        False,
        "shown",
    ),
    ("stock.conservation_cost_per_unit", "float", 0, 10000, False, False, "shown"),
    ("peer_market.enabled", "bool", None, None, False, False, "shown"),
    ("transport.unit_cost", "float", 0, 10000, False, False, "shown"),
    ("transport.shared", "bool", None, None, False, False, "not_shown"),
    ("transport.failure_probability", "float", 0, 1, False, False, "secret"),
    ("transport.loss_fraction", "float", 0, 1, False, False, "secret"),
    ("insurance.tiers.cip.enabled", "bool", None, None, False, False, "not_shown"),
    ("insurance.tiers.cip.fee", "float", 0, 10000, False, False, "not_shown"),
    ("insurance.tiers.dap.enabled", "bool", None, None, False, False, "not_shown"),
    ("insurance.tiers.dap.fee", "float", 0, 10000, False, False, "not_shown"),
    ("insurance.tiers.ddp.enabled", "bool", None, None, False, False, "not_shown"),
    ("insurance.tiers.ddp.fee", "float", 0, 10000, False, False, "not_shown"),
    ("insurance.target_margin", "float", -0.5, 2.0, False, False, "secret"),
    ("insurance.reference_shipment", "int", 1, 9999, False, False, "secret"),
]
EXPECTED_PATHS = [row[0] for row in EXPECTED_SPECS]
NUMERIC_SPECS = [row for row in EXPECTED_SPECS if row[1] in ("int", "float")]
BOOL_PATHS = [row[0] for row in EXPECTED_SPECS if row[1] == "bool"]

SHOWN = [
    "demand.retail_price",
    "stock.conservation_extra_fresh_days",
    "stock.conservation_cost_per_unit",
    "peer_market.enabled",
    "transport.unit_cost",
]
SECRETS = [
    "random_seed",
    "supplier.capacity_sd_per_player",
    "supplier.disruption_probability",
    "transport.failure_probability",
    "transport.loss_fraction",
    "insurance.target_margin",
    "insurance.reference_shipment",
]


def nest(path: str, value: Any) -> dict:
    """Build the nested patch that sets one dot path."""
    out: Any = value
    for key in reversed(path.split(".")):
        out = {key: out}
    return out


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def get_path(tree: dict, path: str) -> Any:
    node: Any = tree
    for key in path.split("."):
        node = node[key]
    return node


def leaves(tree: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(leaves(value, path))
        else:
            out[path] = value
    return out


def ordered(tree: Any) -> Any:
    """Turn nested dicts into lists of pairs so key order takes part in ==."""
    if isinstance(tree, dict):
        return [(k, ordered(v)) for k, v in tree.items()]
    return tree


def all_values(tree: Any) -> list[Any]:
    if isinstance(tree, dict):
        out: list[Any] = []
        for value in tree.values():
            out.extend(all_values(value))
        return out
    return [tree]


def all_keys(tree: Any) -> list[str]:
    if isinstance(tree, dict):
        out: list[str] = []
        for key, value in tree.items():
            out.append(key)
            out.extend(all_keys(value))
        return out
    return []


def issue_pairs(issues: list[FieldIssue]) -> list[tuple[str | None, str]]:
    return [(i.field, i.message) for i in issues]


def raised(patch: dict, current: GameConfig | None = None) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        GameConfig.from_host_input(patch, current)
    return info.value


def between(lo: Any, hi: Any) -> str:
    return f"Must be between {format_number(lo)} and {format_number(hi)}."


def above(lo: Any, hi: Any) -> str:
    return f"Must be more than {format_number(lo)} and at most {format_number(hi)}."


def expected_fee_warnings(cfg: GameConfig) -> list[tuple[str, str]]:
    """Plan 04 section 3.3 written from the spec, using section 08's functions."""
    d = cfg.to_dict()
    out = []
    for tier in (CIP, DAP, DDP):
        terms = d["insurance"]["tiers"][tier.value]
        if not terms["enabled"]:
            continue
        be = break_even_fee(
            tier,
            failure_probability=d["transport"]["failure_probability"],
            loss_fraction=d["transport"]["loss_fraction"],
            shared=d["transport"]["shared"],
            n_ref=d["max_players"],
            reference_shipment=d["insurance"]["reference_shipment"],
            base_price=d["supplier"]["base_price"],
            retail_price=d["demand"]["retail_price"],
        )
        if be == 0:
            continue
        ratio = fee_ratio(terms["fee"], be)
        assert ratio is not None
        if ratio > 3 or ratio < 0.5:
            out.append(
                (
                    f"insurance.tiers.{tier.value}.fee",
                    f"Far from break-even (ratio {ratio:.2f}).",
                )
            )
    return out


def custom_config() -> GameConfig:
    """A valid config in which every single field differs from the default."""
    return GameConfig.from_host_input(
        {
            "max_players": 7,
            "duration_days": 20,
            "starting_cash": 1234.5,
            "random_seed": 42,
            "demand": {
                "per_player_peak": 30.5,
                "weekday_peak": {"from": "mon", "to": "wed"},
                "retail_price": 35,
                "backlog_penalty": 2,
            },
            "reputation": {"starting": 0.4, "smoothing": 0.2, "waste_weight": 0.1},
            "supplier": {
                "base_price": 10,
                "spot_spread": 0.2,
                "capacity_mean_per_player": 20,
                "capacity_sd_per_player": 4,
                "disruption_probability": 0.1,
                "disruption_factor": 0.5,
                "lead_time_days": 2,
                "initial_pipeline_qty": 5,
                "quality_min": 70,
                "quality_max": 95,
            },
            "futures": {"horizon_days": 6},
            "stock": {
                "fresh_days": 2,
                "max_days": 6,
                "holding_cost": 0.3,
                "initial_stock": 10,
                "salvage_value": 1,
                "conservation_extra_fresh_days": 3,
                "conservation_cost_per_unit": 0.5,
            },
            "peer_market": {"enabled": False},
            "transport": {
                "unit_cost": 0.5,
                "shared": True,
                "failure_probability": 0.2,
                "loss_fraction": 0.4,
            },
            "insurance": {
                "tiers": {
                    "cip": {"enabled": False, "fee": 0.3},
                    "dap": {"enabled": False, "fee": 0.5},
                    "ddp": {"enabled": False, "fee": 1.2},
                },
                "target_margin": 0.2,
                "reference_shipment": 25,
            },
        }
    )


# --------------------------------------------------------------------------- #
# Criterion 1: defaults
# --------------------------------------------------------------------------- #


def test_default_config_equals_spec_defaults_key_for_key() -> None:
    assert DEFAULT_CONFIG == SPEC_DEFAULTS
    assert DEFAULT_CONFIG["random_seed"] is None


def test_default_config_key_order_follows_spec_table_order() -> None:
    assert ordered(DEFAULT_CONFIG) == ordered(SPEC_DEFAULTS)
    top = list(DEFAULT_CONFIG)
    assert top.index("stock") < top.index("peer_market") < top.index("transport")


def test_default_to_dict_equals_default_config() -> None:
    assert GameConfig.default().to_dict() == DEFAULT_CONFIG
    assert GameConfig.default().to_dict() == SPEC_DEFAULTS
    assert ordered(GameConfig.default().to_dict()) == ordered(DEFAULT_CONFIG)


def test_default_to_dict_uses_wire_keys_and_json_safe_values() -> None:
    d = GameConfig.default().to_dict()
    assert set(d["demand"]["weekday_peak"]) == {"from", "to"}
    assert json.loads(json.dumps(d)) == d


def test_to_dict_result_is_a_copy_not_shared_state() -> None:
    cfg = GameConfig.default()
    d = cfg.to_dict()
    d["demand"]["retail_price"] = 999
    d["insurance"]["tiers"]["cip"]["fee"] = 999
    assert cfg.to_dict() == SPEC_DEFAULTS
    assert DEFAULT_CONFIG == SPEC_DEFAULTS


def test_from_host_input_does_not_mutate_default_config() -> None:
    GameConfig.from_host_input(
        {"demand": {"retail_price": 40, "weekday_peak": {"from": "mon"}}}
    )
    validate_config({"stock": {"max_days": 9}, "insurance": {"tiers": {}}})
    assert DEFAULT_CONFIG == SPEC_DEFAULTS


def test_player_config_keys_exact() -> None:
    assert PLAYER_CONFIG_KEYS == (
        "demand.retail_price",
        "stock.conservation_extra_fresh_days",
        "stock.conservation_cost_per_unit",
        "peer_market.enabled",
        "transport.unit_cost",
    )


def test_from_host_input_empty_patch_is_the_defaults() -> None:
    assert GameConfig.from_host_input({}) == GameConfig.default()
    result = validate_config({})
    assert result.errors == []
    assert result.warnings == []
    assert result.config == GameConfig.default()


# --------------------------------------------------------------------------- #
# Criterion 2: frozen models
# --------------------------------------------------------------------------- #


def test_every_model_is_frozen() -> None:
    cfg = GameConfig.default()
    targets: list[tuple[Any, str, Any]] = [
        (cfg, "max_players", 5),
        (cfg.demand, "retail_price", 1.0),
        (cfg.demand.weekday_peak, "to", "sun"),
        (cfg.reputation, "starting", 0.1),
        (cfg.supplier, "base_price", 1.0),
        (cfg.futures, "horizon_days", 2),
        (cfg.stock, "max_days", 9),
        (cfg.peer_market, "enabled", False),
        (cfg.transport, "unit_cost", 2.0),
        (cfg.insurance, "target_margin", 0.5),
        (cfg.insurance.tiers, "cip", cfg.insurance.tiers.dap),
        (cfg.insurance.tiers.cip, "fee", 9.0),
    ]
    for obj, name, value in targets:
        with pytest.raises((TypeError, ValueError)):
            setattr(obj, name, value)
    assert cfg == GameConfig.default()


def test_model_classes_are_exposed() -> None:
    cfg = GameConfig.default()
    assert isinstance(cfg.demand, DemandConfig)
    assert isinstance(cfg.demand.weekday_peak, PeakWindow)
    assert isinstance(cfg.reputation, ReputationConfig)
    assert isinstance(cfg.supplier, SupplierConfig)
    assert isinstance(cfg.futures, FuturesConfig)
    assert isinstance(cfg.stock, StockConfig)
    assert isinstance(cfg.peer_market, PeerMarketConfig)
    assert isinstance(cfg.transport, TransportConfig)
    assert isinstance(cfg.insurance, InsuranceConfig)
    assert isinstance(cfg.insurance.tiers, InsuredTiersConfig)
    assert isinstance(cfg.insurance.tiers.cip, InsuredTierConfig)


def test_peak_window_accepts_alias_and_field_name() -> None:
    by_alias = PeakWindow(**{"from": "mon", "to": "tue"})
    by_name = PeakWindow(from_="mon", to="tue")
    assert by_alias == by_name
    assert by_alias.from_ == "mon"


def test_extra_keys_are_forbidden_in_from_dict() -> None:
    data = GameConfig.default().to_dict()
    data["foo"] = 1
    with pytest.raises((TypeError, ValueError)):
        GameConfig.from_dict(data)
    data = GameConfig.default().to_dict()
    data["demand"]["foo"] = 1
    with pytest.raises((TypeError, ValueError)):
        GameConfig.from_dict(data)


# --------------------------------------------------------------------------- #
# Criterion 3: worked examples
# --------------------------------------------------------------------------- #

EXAMPLE_ERRORS: list[tuple[dict, str, str]] = [
    (
        {"stock": {"fresh_days": 3}},
        "stock.max_days",
        "Must be more than the fresh days (3).",
    ),
    (
        {"demand": {"weekday_peak": {"from": "sun"}}},
        "demand.weekday_peak.to",
        "The peak window cannot end before it starts.",
    ),
    (
        {"stock": {"salvage_value": 13}},
        "stock.salvage_value",
        "Must not exceed the base price ($12.00).",
    ),
    (
        {"futures": {"horizon_days": 1}},
        "futures.horizon_days",
        "Must be more days than the lead time (1).",
    ),
    (
        {"duration_days": 7},
        "futures.horizon_days",
        "Must be fewer days than the game (7).",
    ),
    (
        {"supplier": {"spot_spread": 0.95}},
        "supplier.spot_spread",
        "Must be between 0 and 0.9.",
    ),
    (
        {"demand": {"retail_price": 0}},
        "demand.retail_price",
        "Must be more than 0 and at most 10,000.",
    ),
    ({"max_players": True}, "max_players", "Must be a whole number."),
    ({"max_players": 4.0}, "max_players", "Must be a whole number."),
    ({"starting_cash": "5000"}, "starting_cash", "Must be a number."),
    ({"transport": {"shared": "yes"}}, "transport.shared", "Must be true or false."),
    (
        {"demand": {"weekday_peak": {"from": "THU"}}},
        "demand.weekday_peak.from",
        "Must be one of mon, tue, wed, thu, fri, sat, sun.",
    ),
    (
        {"supplier": {"quality_min": 90, "quality_max": 85}},
        "supplier.quality_max",
        "Must be at least the minimum quality (90%).",
    ),
    (
        {"supplier": {"quality_min": 0}},
        "supplier.quality_min",
        "Must be between 1 and 100.",
    ),
    (
        {"supplier": {"quality_max": 99.5}},
        "supplier.quality_max",
        "Must be a whole number.",
    ),
    (
        {"peer_market": {"enabled": "yes"}},
        "peer_market.enabled",
        "Must be true or false.",
    ),
    ({"foo": 1}, "foo", "Unknown setting."),
    ({"demand": 5}, "demand", "Unknown setting."),
]


@pytest.mark.parametrize("patch,field,message", EXAMPLE_ERRORS)
def test_worked_example_errors_from_host_input(
    patch: dict, field: str, message: str
) -> None:
    err = raised(patch)
    assert err.field == field
    assert err.message == message


@pytest.mark.parametrize("patch,field,message", EXAMPLE_ERRORS)
def test_worked_example_errors_from_validate_config(
    patch: dict, field: str, message: str
) -> None:
    result = validate_config(patch)
    assert result.config is None
    assert result.warnings == []
    assert (field, message) in issue_pairs(result.errors)
    assert result.errors[0] == FieldIssue(field, message)


def test_worked_example_positive_cases() -> None:
    assert GameConfig.from_host_input({}) == GameConfig.default()
    assert (
        GameConfig.from_host_input(
            {"supplier": {"quality_min": 100}}
        ).supplier.quality_min
        == 100
    )
    assert (
        GameConfig.from_host_input(
            {"peer_market": {"enabled": False}}
        ).peer_market.enabled
        is False
    )
    cash = GameConfig.from_host_input({"starting_cash": 7500}).starting_cash
    assert cash == 7500.0
    assert isinstance(cash, float)


def test_worked_example_two_errors_in_order() -> None:
    result = validate_config({"max_players": 1, "stock": {"max_days": 0}})
    assert issue_pairs(result.errors) == [
        ("max_players", "Must be between 2 and 12."),
        ("stock.max_days", "Must be between 1 and 14."),
    ]
    assert result.config is None


def test_worked_example_shared_transport_warnings() -> None:
    result = validate_config({"transport": {"shared": True}})
    assert result.errors == []
    assert issue_pairs(result.warnings) == [
        ("insurance.tiers.cip.fee", "Far from break-even (ratio 6.55)."),
        ("insurance.tiers.dap.fee", "Far from break-even (ratio 6.55)."),
        ("insurance.tiers.ddp.fee", "Far from break-even (ratio 6.62)."),
    ]
    assert result.config is not None
    assert result.config.transport.shared is True


def test_worked_example_retail_price_warning() -> None:
    result = validate_config({"demand": {"retail_price": 15}})
    assert result.errors == []
    assert issue_pairs(result.warnings) == [
        ("demand.retail_price", "On the dearest spot days a unit can lose money.")
    ]


def test_worked_example_secret_keys_and_public_config() -> None:
    assert sorted(SECRET_KEYS) == sorted(SECRETS)
    assert isinstance(SECRET_KEYS, frozenset)
    public = config_public(GameConfig.default())
    assert public == {
        "demand": {"retail_price": 30},
        "stock": {
            "conservation_extra_fresh_days": 2,
            "conservation_cost_per_unit": 1.0,
        },
        "peer_market": {"enabled": True},
        "transport": {"unit_cost": 1.0},
    }
    assert "supplier" not in public
    assert "insurance" not in public


def test_worked_example_schema_players() -> None:
    fields = config_schema()["fields"]
    assert fields["demand.retail_price"]["players"] == "shown"
    assert fields["stock.holding_cost"]["players"] == "not_shown"
    assert fields["transport.loss_fraction"]["players"] == "secret"


def test_message_config_locked_copy() -> None:
    assert MSG_CONFIG_LOCKED == "The settings are locked once the game starts."


def test_config_error_is_an_exception_with_field_and_message() -> None:
    err = raised({"foo": 1})
    assert isinstance(err, Exception)
    assert isinstance(err.message, str) and err.message
    assert FieldIssue(None, MSG_CONFIG_LOCKED).field is None


# --------------------------------------------------------------------------- #
# Criterion 4/7: bounds and types; failure modes 1, 5, 10
# --------------------------------------------------------------------------- #


def test_field_specs_match_the_section_table() -> None:
    assert all(isinstance(s, FieldSpec) for s in FIELD_SPECS)
    assert [s.path for s in FIELD_SPECS] == EXPECTED_PATHS
    for spec, row in zip(FIELD_SPECS, EXPECTED_SPECS, strict=True):
        path, type_, lo, hi, excl, nullable, players = row
        assert (spec.type, spec.min, spec.max) == (type_, lo, hi), path
        assert (spec.min_exclusive, spec.nullable, spec.players) == (
            excl,
            nullable,
            players,
        ), path


def test_field_specs_cover_exactly_the_leaves_of_the_defaults() -> None:
    assert list(leaves(DEFAULT_CONFIG)) == EXPECTED_PATHS


def _epsilon(type_: str) -> float:
    return 1 if type_ == "int" else 1e-6


@pytest.mark.parametrize("row", NUMERIC_SPECS, ids=lambda r: r[0])
def test_below_minimum_is_rejected_with_range_message(row: tuple) -> None:
    path, type_, lo, hi, excl, _, _ = row
    bad = lo if excl else lo - _epsilon(type_)
    expected = above(lo, hi) if excl else between(lo, hi)
    patch = nest(path, bad)
    err = raised(patch)
    assert (err.field, err.message) == (path, expected)
    result = validate_config(patch)
    assert result.config is None
    assert issue_pairs(result.errors) == [(path, expected)]


@pytest.mark.parametrize("row", NUMERIC_SPECS, ids=lambda r: r[0])
def test_above_maximum_is_rejected_with_range_message(row: tuple) -> None:
    path, type_, lo, hi, excl, _, _ = row
    bad = hi + _epsilon(type_)
    expected = above(lo, hi) if excl else between(lo, hi)
    patch = nest(path, bad)
    err = raised(patch)
    assert (err.field, err.message) == (path, expected)
    assert issue_pairs(validate_config(patch).errors) == [(path, expected)]


# Companion changes that keep a boundary value clear of the cross-field rules.
COMPANIONS: dict[str, dict] = {
    "supplier.quality_max": {"supplier": {"quality_min": 1}},
    "stock.max_days": {"stock": {"fresh_days": 0}},
    "stock.salvage_value": {"supplier": {"base_price": 10000}},
    "futures.horizon_days": {"supplier": {"lead_time_days": 0}, "duration_days": 60},
}


@pytest.mark.parametrize("row", NUMERIC_SPECS, ids=lambda r: r[0])
def test_boundary_values_are_not_rejected_as_out_of_range(row: tuple) -> None:
    path, type_, lo, hi, excl, _, _ = row
    ok_values = [hi, 0.01 if excl else lo]
    for value in ok_values:
        patch = deep_merge(COMPANIONS.get(path, {}), nest(path, value))
        if path == "futures.horizon_days" and value == hi:
            patch = deep_merge(patch, {"duration_days": 60})
        fields = [i.field for i in validate_config(patch).errors]
        assert path not in fields, (path, value)


@pytest.mark.parametrize("row", NUMERIC_SPECS, ids=lambda r: r[0])
def test_boolean_is_never_a_number(row: tuple) -> None:
    path, type_, *_ = row
    expected = "Must be a whole number." if type_ == "int" else "Must be a number."
    for value in (True, False):
        err = raised(nest(path, value))
        assert (err.field, err.message) == (path, expected)


@pytest.mark.parametrize("row", NUMERIC_SPECS, ids=lambda r: r[0])
def test_string_is_not_a_number(row: tuple) -> None:
    path, type_, *_ = row
    expected = "Must be a whole number." if type_ == "int" else "Must be a number."
    err = raised(nest(path, "5"))
    assert (err.field, err.message) == (path, expected)


@pytest.mark.parametrize(
    "path", [r[0] for r in NUMERIC_SPECS if r[1] == "int"], ids=lambda p: p
)
def test_int_field_rejects_a_float_even_when_integral(path: str) -> None:
    err = raised(nest(path, 5.0))
    assert (err.field, err.message) == (path, "Must be a whole number.")


@pytest.mark.parametrize("path", BOOL_PATHS)
def test_bool_field_accepts_only_true_false(path: str) -> None:
    for bad in (1, 0, "true", "yes", 1.0):
        err = raised(nest(path, bad))
        assert (err.field, err.message) == (path, "Must be true or false.")
    for good in (True, False):
        cfg = GameConfig.from_host_input(nest(path, good))
        assert get_path(cfg.to_dict(), path) is good


@pytest.mark.parametrize("path", ["demand.weekday_peak.from", "demand.weekday_peak.to"])
def test_weekday_field_accepts_only_the_seven_lowercase_strings(path: str) -> None:
    msg = "Must be one of mon, tue, wed, thu, fri, sat, sun."
    for bad in ("THU", "Thu", "thursday", "", 4, None, True):
        err = raised(nest(path, bad))
        assert (err.field, err.message) == (path, msg)


def test_random_seed_is_the_only_nullable_field() -> None:
    cfg = GameConfig.from_host_input({"random_seed": None})
    assert cfg.random_seed is None
    assert GameConfig.from_host_input({"random_seed": 0}).random_seed == 0
    assert GameConfig.from_host_input({"random_seed": SEED_MAX}).random_seed == SEED_MAX
    current = GameConfig.from_host_input({"random_seed": 5})
    assert (
        GameConfig.from_host_input({"random_seed": None}, current).random_seed is None
    )
    for row in EXPECTED_SPECS:
        path = row[0]
        if path == "random_seed":
            continue
        err = raised(nest(path, None))
        assert err.field == path
        assert isinstance(err.message, str) and err.message


def test_random_seed_bounds_messages() -> None:
    err = raised({"random_seed": -1})
    assert err.message == "Must be between 0 and 9,007,199,254,740,991."
    err = raised({"random_seed": 2**53})
    assert err.message == "Must be between 0 and 9,007,199,254,740,991."


def test_float_fields_accept_int_and_store_float() -> None:
    cfg = GameConfig.from_host_input({"starting_cash": 7500})
    assert isinstance(cfg.starting_cash, float)
    d = GameConfig.from_host_input({"demand": {"retail_price": 40}}).to_dict()
    assert d["demand"]["retail_price"] == 40
    assert isinstance(d["demand"]["retail_price"], float)


def test_clamping_spot_spread_five_raises_and_nothing_is_returned() -> None:
    err = raised({"supplier": {"spot_spread": 5}})
    assert (err.field, err.message) == (
        "supplier.spot_spread",
        "Must be between 0 and 0.9.",
    )
    # the clamped value must not appear anywhere
    result = validate_config({"supplier": {"spot_spread": 5}})
    assert result.config is None
    for bad in (0.91, 1, -0.01):
        raised({"supplier": {"spot_spread": bad}})
    assert (
        GameConfig.from_host_input(
            {"supplier": {"spot_spread": 0.9}}
        ).supplier.spot_spread
        == 0.9
    )


def test_boolean_duration_and_cash_are_rejected() -> None:
    err = raised({"duration_days": True})
    assert (err.field, err.message) == ("duration_days", "Must be a whole number.")
    err = raised({"starting_cash": False})
    assert (err.field, err.message) == ("starting_cash", "Must be a number.")
    # True must not be read as 1 day or 1 dollar
    assert validate_config({"duration_days": True}).config is None
    assert validate_config({"starting_cash": False}).config is None


def test_quality_as_a_fraction_is_rejected_not_read_as_percent() -> None:
    err = raised({"supplier": {"quality_min": 0.8}})
    assert (err.field, err.message) == (
        "supplier.quality_min",
        "Must be a whole number.",
    )
    err = raised({"supplier": {"quality_max": 0.95}})
    assert (err.field, err.message) == (
        "supplier.quality_max",
        "Must be a whole number.",
    )
    assert validate_config({"supplier": {"quality_min": 0.8}}).config is None


@settings(max_examples=60, deadline=None)
@given(
    max_players=st.integers(2, MAX_PLAYERS),
    cash=st.floats(0, MAX_MONEY, allow_nan=False),
    peak=st.floats(1, MAX_LAMBDA, allow_nan=False),
    retail=st.floats(0.01, 10000, allow_nan=False),
    backlog=st.floats(0, 10000, allow_nan=False),
    rep=st.floats(0, 1, allow_nan=False),
    holding=st.floats(0, 10000, allow_nan=False),
    unit_cost=st.floats(0, 10000, allow_nan=False),
    failure=st.floats(0, 1, allow_nan=False),
    loss=st.floats(0, 1, allow_nan=False),
    seed=st.one_of(st.none(), st.integers(0, SEED_MAX)),
)
def test_valid_input_is_never_altered(
    max_players: int,
    cash: float,
    peak: float,
    retail: float,
    backlog: float,
    rep: float,
    holding: float,
    unit_cost: float,
    failure: float,
    loss: float,
    seed: int | None,
) -> None:
    patch = {
        "max_players": max_players,
        "starting_cash": cash,
        "random_seed": seed,
        "demand": {
            "per_player_peak": peak,
            "retail_price": retail,
            "backlog_penalty": backlog,
        },
        "reputation": {"starting": rep, "smoothing": rep, "waste_weight": rep},
        "stock": {"holding_cost": holding},
        "transport": {
            "unit_cost": unit_cost,
            "failure_probability": failure,
            "loss_fraction": loss,
        },
    }
    cfg = GameConfig.from_host_input(patch)
    assert cfg.to_dict() == deep_merge(SPEC_DEFAULTS, patch)


# --------------------------------------------------------------------------- #
# Criterion 5/6: merging, current unchanged, error order; failure modes 2, 6, 8, 9
# --------------------------------------------------------------------------- #


def test_failed_patch_leaves_current_unchanged() -> None:
    current = GameConfig.from_host_input({"max_players": 6})
    snapshot = current.to_dict()
    err = raised({"max_players": 99}, current)
    assert err.field == "max_players"
    assert current.to_dict() == snapshot
    result = validate_config({"max_players": 99}, current)
    assert result.config is None
    assert current.to_dict() == snapshot


def test_successful_patch_returns_new_config_and_keeps_current() -> None:
    current = GameConfig.from_host_input({"max_players": 6})
    snapshot = current.to_dict()
    new = GameConfig.from_host_input({"starting_cash": 100}, current)
    assert new is not current
    assert new.max_players == 6
    assert new.starting_cash == 100.0
    assert current.to_dict() == snapshot


def test_patch_merges_onto_current_not_onto_defaults() -> None:
    current = GameConfig.from_host_input(
        {
            "max_players": 6,
            "demand": {
                "backlog_penalty": 2.5,
                "weekday_peak": {"from": "mon", "to": "tue"},
            },
        }
    )
    new = GameConfig.from_host_input({"demand": {"retail_price": 40}}, current)
    d = new.to_dict()
    assert d["demand"]["retail_price"] == 40
    assert d["demand"]["backlog_penalty"] == 2.5
    assert d["demand"]["weekday_peak"] == {"from": "mon", "to": "tue"}
    assert d["demand"]["per_player_peak"] == 25
    assert d["max_players"] == 6
    assert validate_config({"demand": {"retail_price": 40}}, current).config == new


def test_partial_patch_does_not_reset_siblings_nested_deeper() -> None:
    current = GameConfig.from_host_input(
        {"insurance": {"tiers": {"cip": {"fee": 0.5}, "dap": {"enabled": False}}}}
    )
    new = GameConfig.from_host_input(
        {"insurance": {"tiers": {"cip": {"enabled": False}}}}, current
    )
    tiers = new.to_dict()["insurance"]["tiers"]
    assert tiers["cip"] == {"enabled": False, "fee": 0.5}
    assert tiers["dap"]["enabled"] is False
    assert tiers["ddp"] == {"enabled": True, "fee": 1.39}


def test_inverted_fresh_max_range_cannot_be_stored() -> None:
    err = raised({"stock": {"fresh_days": 4, "max_days": 4}})
    assert (err.field, err.message) == (
        "stock.max_days",
        "Must be more than the fresh days (4).",
    )
    err = raised({"stock": {"fresh_days": 5, "max_days": 4}})
    assert (err.field, err.message) == (
        "stock.max_days",
        "Must be more than the fresh days (5).",
    )
    # raising fresh_days alone past the current max_days (4)
    err = raised({"stock": {"fresh_days": 4}})
    assert (err.field, err.message) == (
        "stock.max_days",
        "Must be more than the fresh days (4).",
    )
    assert validate_config({"stock": {"fresh_days": 4, "max_days": 4}}).config is None
    ok = GameConfig.from_host_input({"stock": {"fresh_days": 3, "max_days": 4}})
    assert ok.stock.fresh_days < ok.stock.max_days


def test_inverted_range_rules_read_the_merged_config() -> None:
    current = GameConfig.from_host_input({"stock": {"fresh_days": 3, "max_days": 5}})
    err = raised({"stock": {"max_days": 3}}, current)
    assert (err.field, err.message) == (
        "stock.max_days",
        "Must be more than the fresh days (3).",
    )
    current = GameConfig.from_host_input(
        {"demand": {"weekday_peak": {"from": "wed", "to": "fri"}}}
    )
    err = raised({"demand": {"weekday_peak": {"to": "tue"}}}, current)
    assert (err.field, err.message) == (
        "demand.weekday_peak.to",
        "The peak window cannot end before it starts.",
    )
    err = raised({"demand": {"weekday_peak": {"from": "sat"}}}, current)
    assert err.field == "demand.weekday_peak.to"
    # equal from and to is allowed
    ok = GameConfig.from_host_input(
        {"demand": {"weekday_peak": {"from": "fri", "to": "fri"}}}
    )
    assert ok.demand.weekday_peak.from_ == "fri"


def test_inverted_quality_range_cannot_be_stored() -> None:
    err = raised({"supplier": {"quality_min": 95, "quality_max": 90}})
    assert (err.field, err.message) == (
        "supplier.quality_max",
        "Must be at least the minimum quality (95%).",
    )
    assert (
        validate_config({"supplier": {"quality_min": 95, "quality_max": 90}}).config
        is None
    )


def test_lowering_only_quality_max_below_current_min_is_rejected() -> None:
    current = GameConfig.from_host_input({"supplier": {"quality_min": 90}})
    err = raised({"supplier": {"quality_max": 85}}, current)
    assert (err.field, err.message) == (
        "supplier.quality_max",
        "Must be at least the minimum quality (90%).",
    )
    # and raising only quality_min above the default quality_max (100) is a bound error
    err = raised({"supplier": {"quality_min": 101}})
    assert err.field == "supplier.quality_min"
    # default max is 100, so lowering max to the stored min works
    ok = GameConfig.from_host_input({"supplier": {"quality_max": 90}}, current)
    assert ok.supplier.quality_max == 90


def test_horizon_must_be_fewer_days_than_the_game() -> None:
    err = raised({"futures": {"horizon_days": 14}})
    assert (err.field, err.message) == (
        "futures.horizon_days",
        "Must be fewer days than the game (14).",
    )
    ok = GameConfig.from_host_input({"futures": {"horizon_days": 13}})
    assert ok.futures.horizon_days == 13
    # shortening the game under the current horizon (7)
    err = raised({"duration_days": 7})
    assert err.message == "Must be fewer days than the game (7)."
    assert GameConfig.from_host_input({"duration_days": 8}).duration_days == 8


def test_horizon_must_exceed_the_lead_time() -> None:
    err = raised({"supplier": {"lead_time_days": 7}})
    assert (err.field, err.message) == (
        "futures.horizon_days",
        "Must be more days than the lead time (7).",
    )
    ok = GameConfig.from_host_input({"supplier": {"lead_time_days": 6}})
    assert ok.supplier.lead_time_days == 6


def test_salvage_cannot_exceed_base_price_in_the_merged_config() -> None:
    current = GameConfig.from_host_input({"supplier": {"base_price": 20}})
    assert GameConfig.from_host_input({"stock": {"salvage_value": 13}}, current)
    err = raised({"supplier": {"base_price": 1}}, current)
    assert (err.field, err.message) == (
        "stock.salvage_value",
        "Must not exceed the base price ($1.00).",
    )
    ok = GameConfig.from_host_input({"stock": {"salvage_value": 12}})
    assert ok.stock.salvage_value == 12


def test_cross_field_rules_are_reported_in_spec_order() -> None:
    patch = {
        "stock": {"fresh_days": 4, "max_days": 4, "salvage_value": 13},
        "demand": {"weekday_peak": {"from": "sun"}},
        "futures": {"horizon_days": 1},
        "supplier": {"quality_min": 90, "quality_max": 85},
    }
    result = validate_config(patch)
    assert [i.field for i in result.errors] == [
        "stock.max_days",
        "demand.weekday_peak.to",
        "stock.salvage_value",
        "futures.horizon_days",
        "supplier.quality_max",
    ]
    assert result.config is None
    assert result.warnings == []
    assert raised(patch).field == "stock.max_days"


def test_horizon_rule_lead_time_before_game_length() -> None:
    result = validate_config({"futures": {"horizon_days": 1}, "duration_days": 7})
    assert [i.message for i in result.errors][0] == (
        "Must be more days than the lead time (1)."
    )


def test_leaf_errors_follow_field_specs_order_not_patch_order() -> None:
    patch = {
        "transport": {"shared": "x"},
        "stock": {"max_days": 0},
        "max_players": 1,
    }
    result = validate_config(patch)
    assert [i.field for i in result.errors] == [
        "max_players",
        "stock.max_days",
        "transport.shared",
    ]
    assert raised(patch).field == "max_players"


def test_leaf_errors_come_before_cross_field_errors() -> None:
    patch = {"stock": {"salvage_value": 13}, "max_players": 1}
    result = validate_config(patch)
    assert [i.field for i in result.errors] == ["max_players", "stock.salvage_value"]
    assert raised(patch).field == "max_players"


def test_cross_field_rule_is_skipped_when_a_leaf_it_reads_failed() -> None:
    result = validate_config({"stock": {"fresh_days": -1, "max_days": 1}})
    assert issue_pairs(result.errors) == [("stock.fresh_days", between(0, 14))]
    result = validate_config({"duration_days": 3})
    assert issue_pairs(result.errors) == [("duration_days", between(7, 60))]
    result = validate_config({"supplier": {"quality_min": 0.5, "quality_max": 50}})
    assert issue_pairs(result.errors) == [
        ("supplier.quality_min", "Must be a whole number.")
    ]


def test_unknown_keys_are_reported_first_with_their_dot_path() -> None:
    result = validate_config({"max_players": 1, "foo": 1})
    assert result.config is None
    assert result.errors[0] == FieldIssue("foo", "Unknown setting.")
    assert raised({"max_players": 1, "foo": 1}).field == "foo"
    assert raised({"demand": {"bar": 1}}).field == "demand.bar"
    assert raised({"demand": {"weekday_peak": {"mid": "mon"}}}).field == (
        "demand.weekday_peak.mid"
    )
    assert raised({"insurance": {"tiers": {"exw": {"fee": 1}}}}).field == (
        "insurance.tiers.exw"
    )
    assert raised({"insurance": {"tiers": {"cip": {"price": 1}}}}).field == (
        "insurance.tiers.cip.price"
    )


def test_a_group_given_a_non_object_is_unknown_setting_at_the_group() -> None:
    for patch, path in (
        ({"demand": 5}, "demand"),
        ({"stock": "x"}, "stock"),
        ({"demand": {"weekday_peak": 5}}, "demand.weekday_peak"),
        ({"insurance": {"tiers": 5}}, "insurance.tiers"),
        ({"insurance": {"tiers": {"cip": None}}}, "insurance.tiers.cip"),
    ):
        err = raised(patch)
        assert (err.field, err.message) == (path, "Unknown setting.")


def test_validate_config_returns_merged_config_when_valid() -> None:
    result = validate_config({"max_players": 8})
    assert isinstance(result, ValidationResult)
    assert result.errors == []
    assert result.config == GameConfig.from_host_input({"max_players": 8})


def test_validate_config_config_is_none_exactly_when_errors() -> None:
    for patch in ({}, {"max_players": 8}, {"max_players": 1}, {"foo": 1}):
        result = validate_config(patch)
        assert (result.config is None) == bool(result.errors)
        if result.errors:
            assert result.warnings == []


def test_validate_config_does_not_report_warnings_when_there_are_errors() -> None:
    result = validate_config({"max_players": 1, "demand": {"retail_price": 15}})
    assert result.config is None
    assert result.warnings == []
    assert config_warnings(GameConfig.from_host_input({"demand": {"retail_price": 15}}))


def test_all_error_messages_are_nonempty_strings_with_field() -> None:
    result = validate_config(
        {
            "max_players": 1,
            "foo": 1,
            "transport": {"shared": 3},
            "stock": {"max_days": 0},
        }
    )
    assert len(result.errors) >= 3
    for issue in result.errors:
        assert isinstance(issue.field, str) and issue.field
        assert isinstance(issue.message, str) and issue.message


# --------------------------------------------------------------------------- #
# Warnings; failure mode 7
# --------------------------------------------------------------------------- #


def test_warning_only_config_is_accepted_by_from_host_input() -> None:
    patch = {"demand": {"retail_price": 15}, "transport": {"shared": True}}
    cfg = GameConfig.from_host_input(patch)
    assert cfg.demand.retail_price == 15
    assert cfg.transport.shared is True
    assert config_warnings(cfg)
    result = validate_config(patch)
    assert result.errors == []
    assert result.config == cfg
    assert result.warnings


def test_retail_price_warning_threshold() -> None:
    # base 12 x 1.25 + 1 = 16: a price of 16 or less warns, above does not
    for price, warns in ((16, True), (15.99, True), (16.01, False), (30, False)):
        cfg = GameConfig.from_host_input({"demand": {"retail_price": price}})
        got = [
            w.field for w in config_warnings(cfg) if w.field == "demand.retail_price"
        ]
        assert bool(got) is warns, price


def test_retail_price_warning_reads_spread_base_price_and_unit_cost() -> None:
    cfg = GameConfig.from_host_input(
        {"transport": {"unit_cost": 20}, "demand": {"retail_price": 33}}
    )
    assert "demand.retail_price" in [w.field for w in config_warnings(cfg)]
    cfg = GameConfig.from_host_input(
        {"supplier": {"spot_spread": 0.9}, "demand": {"retail_price": 30}}
    )
    # 12 x 1.9 + 1 = 23.8 < 30
    assert "demand.retail_price" not in [w.field for w in config_warnings(cfg)]
    cfg = GameConfig.from_host_input(
        {
            "supplier": {"spot_spread": 0, "base_price": 29},
            "demand": {"retail_price": 30},
        }
    )
    # 29 + 1 = 30 >= 30
    assert "demand.retail_price" in [w.field for w in config_warnings(cfg)]


def test_warnings_order_retail_first_then_tiers_cip_dap_ddp() -> None:
    cfg = GameConfig.from_host_input(
        {"transport": {"shared": True}, "demand": {"retail_price": 15}}
    )
    warnings = config_warnings(cfg)
    assert [w.field for w in warnings] == [
        "demand.retail_price",
        "insurance.tiers.cip.fee",
        "insurance.tiers.dap.fee",
        "insurance.tiers.ddp.fee",
    ]
    assert warnings[0].message == "On the dearest spot days a unit can lose money."
    assert issue_pairs(warnings[1:]) == expected_fee_warnings(cfg)


def test_disabled_tier_has_no_fee_warning() -> None:
    cfg = GameConfig.from_host_input(
        {
            "transport": {"shared": True},
            "insurance": {"tiers": {"cip": {"enabled": False}}},
        }
    )
    assert [w.field for w in config_warnings(cfg)] == [
        "insurance.tiers.dap.fee",
        "insurance.tiers.ddp.fee",
    ]
    all_off = GameConfig.from_host_input(
        {
            "transport": {"shared": True},
            "insurance": {
                "tiers": {
                    "cip": {"enabled": False},
                    "dap": {"enabled": False},
                    "ddp": {"enabled": False},
                }
            },
        }
    )
    assert config_warnings(all_off) == []


def test_no_fee_warning_when_break_even_is_zero() -> None:
    for patch in (
        {"transport": {"failure_probability": 0}},
        {"transport": {"loss_fraction": 0}},
    ):
        cfg = GameConfig.from_host_input(
            {**patch, "insurance": {"tiers": {"cip": {"fee": 9}, "ddp": {"fee": 0.01}}}}
        )
        assert config_warnings(cfg) == []


def test_fee_warning_thresholds_and_two_decimal_ratio() -> None:
    base = GameConfig.default()
    be = {
        t: break_even_fee(
            t,
            failure_probability=0.14,
            loss_fraction=0.30,
            shared=False,
            n_ref=4,
            reference_shipment=20,
            base_price=12,
            retail_price=30,
        )
        for t in (CIP, DAP, DDP)
    }
    for factor, warns in (
        (3.1, True),
        (2.9, False),
        (0.6, False),
        (0.4, True),
        (1, False),
    ):
        fee = be[CIP] * factor
        cfg = GameConfig.from_host_input(
            {"insurance": {"tiers": {"cip": {"fee": fee}}}}, base
        )
        got = [w for w in config_warnings(cfg) if w.field == "insurance.tiers.cip.fee"]
        assert bool(got) is warns, factor
        if warns:
            assert got[0].message == f"Far from break-even (ratio {factor:.2f})."
        assert issue_pairs(config_warnings(cfg)) == expected_fee_warnings(cfg)


def test_fee_warning_uses_max_players_as_n_ref_and_shared_flag() -> None:
    for players in (2, 4, 9):
        cfg = GameConfig.from_host_input(
            {
                "max_players": players,
                "transport": {"shared": True},
                "insurance": {"tiers": {"cip": {"fee": 0.3}, "ddp": {"fee": 0.05}}},
            }
        )
        fee_warnings = [
            (w.field, w.message)
            for w in config_warnings(cfg)
            if w.field != "demand.retail_price"
        ]
        assert fee_warnings == expected_fee_warnings(cfg)


def test_default_config_has_no_warnings() -> None:
    assert config_warnings(GameConfig.default()) == []


# --------------------------------------------------------------------------- #
# Criterion 8: config_public; failure modes 3, 4, 4a
# --------------------------------------------------------------------------- #


def _changed_config() -> GameConfig:
    return GameConfig.from_host_input(
        {
            "demand": {"retail_price": 41.5},
            "stock": {
                "conservation_extra_fresh_days": 5,
                "conservation_cost_per_unit": 2.5,
            },
            "peer_market": {"enabled": False},
            "transport": {"unit_cost": 3.5},
        }
    )


def test_config_public_leaves_are_exactly_the_player_config_keys() -> None:
    for cfg in (GameConfig.default(), _changed_config(), custom_config()):
        public = config_public(cfg)
        flat = leaves(public)
        assert sorted(flat) == sorted(PLAYER_CONFIG_KEYS)
        full = cfg.to_dict()
        for path in PLAYER_CONFIG_KEYS:
            assert flat[path] == get_path(full, path)


def test_config_public_keeps_nested_paths_never_flattened() -> None:
    public = config_public(_changed_config())
    assert public["demand"]["retail_price"] == 41.5
    assert public["stock"]["conservation_extra_fresh_days"] == 5
    assert public["stock"]["conservation_cost_per_unit"] == 2.5
    assert public["peer_market"]["enabled"] is False
    assert public["transport"]["unit_cost"] == 3.5
    for flat_key in (
        "retail_price",
        "unit_cost",
        "enabled",
        "conservation_extra_fresh_days",
        "conservation_cost_per_unit",
        "demand.retail_price",
        "transport.unit_cost",
        "peer_market.enabled",
        "stock.conservation_cost_per_unit",
        "stock.conservation_extra_fresh_days",
    ):
        assert flat_key not in public
    assert list(public) == ["demand", "stock", "peer_market", "transport"]
    assert all(isinstance(v, dict) for v in public.values())


def test_config_public_hides_every_secret_value() -> None:
    cfg = GameConfig.from_host_input(
        {
            "random_seed": 123456789,
            "supplier": {
                "capacity_sd_per_player": 7.77,
                "disruption_probability": 0.0123,
            },
            "transport": {
                "failure_probability": 0.137,
                "loss_fraction": 0.4321,
                "unit_cost": 3.5,
            },
            "insurance": {"target_margin": 0.777, "reference_shipment": 31},
            "demand": {"retail_price": 41.5},
        }
    )
    public = config_public(cfg)
    values = all_values(public)
    for secret in (123456789, 7.77, 0.0123, 0.137, 0.4321, 0.777, 31):
        assert secret not in values
    keys = set(all_keys(public))
    for path in SECRETS:
        assert path not in keys
        assert path.split(".")[-1] not in keys


def test_config_public_is_allow_list_not_deny_list() -> None:
    public = config_public(GameConfig.default())
    for group in ("supplier", "reputation", "futures", "insurance"):
        assert group not in public
    assert "holding_cost" not in public["stock"]
    assert set(public) == {"demand", "stock", "peer_market", "transport"}
    assert set(public["stock"]) == {
        "conservation_extra_fresh_days",
        "conservation_cost_per_unit",
    }
    assert set(public["demand"]) == {"retail_price"}
    assert set(public["transport"]) == {"unit_cost"}
    assert set(public["peer_market"]) == {"enabled"}
    # no non-shown, non-secret key leaks either
    for hidden in (
        "backlog_penalty",
        "per_player_peak",
        "weekday_peak",
        "shared",
        "max_players",
        "duration_days",
        "starting_cash",
        "salvage_value",
        "base_price",
    ):
        assert hidden not in all_keys(public)


def test_config_public_returns_a_copy() -> None:
    cfg = GameConfig.default()
    public = config_public(cfg)
    public["demand"]["retail_price"] = 1
    public["extra"] = 1
    assert config_public(cfg)["demand"]["retail_price"] == 30
    assert cfg.to_dict() == SPEC_DEFAULTS


# --------------------------------------------------------------------------- #
# Criterion 9: config_schema
# --------------------------------------------------------------------------- #


def test_config_schema_has_a_field_for_every_leaf_and_no_other() -> None:
    schema = config_schema()
    assert set(schema) == {"config", "fields", "limits"}
    assert set(schema["fields"]) == set(leaves(DEFAULT_CONFIG))
    assert list(schema["fields"]) == EXPECTED_PATHS
    assert schema["config"] == DEFAULT_CONFIG
    assert ordered(schema["config"]) == ordered(SPEC_DEFAULTS)
    assert schema["limits"] == LIMITS


def test_config_schema_field_entries() -> None:
    for row in EXPECTED_SPECS:
        path, type_, lo, hi, _excl, nullable, players = row
        entry = config_schema()["fields"][path]
        assert set(entry) == {"type", "min", "max", "nullable", "players"}
        assert entry["type"] == type_
        assert entry["min"] == lo
        assert entry["max"] == hi
        assert entry["nullable"] is nullable
        assert entry["players"] == players


def test_config_schema_players_levels_partition_the_leaves() -> None:
    fields = config_schema()["fields"]
    shown = {p for p, f in fields.items() if f["players"] == "shown"}
    secret = {p for p, f in fields.items() if f["players"] == "secret"}
    other = {p for p, f in fields.items() if f["players"] == "not_shown"}
    assert shown == set(PLAYER_CONFIG_KEYS)
    assert secret == set(SECRET_KEYS)
    assert other == set(fields) - shown - secret
    assert shown == set(SHOWN)
    assert secret == set(SECRETS)


def test_field_spec_players_match_public_lists() -> None:
    assert {s.path for s in FIELD_SPECS if s.players == "shown"} == set(
        PLAYER_CONFIG_KEYS
    )
    assert {s.path for s in FIELD_SPECS if s.players == "secret"} == set(SECRET_KEYS)
    assert {s.players for s in FIELD_SPECS} <= {"shown", "secret", "not_shown"}


def test_config_schema_is_json_serialisable() -> None:
    json.dumps(config_schema())


# --------------------------------------------------------------------------- #
# Criterion 10, 11: round trip and tier terms
# --------------------------------------------------------------------------- #


def test_from_dict_round_trip_for_defaults() -> None:
    cfg = GameConfig.default()
    assert GameConfig.from_dict(cfg.to_dict()) == cfg
    assert GameConfig.from_dict(DEFAULT_CONFIG) == cfg


def test_from_dict_round_trip_when_every_field_changed() -> None:
    cfg = custom_config()
    changed = leaves(cfg.to_dict())
    default = leaves(DEFAULT_CONFIG)
    assert set(changed) == set(default)
    assert [p for p in default if changed[p] == default[p]] == []
    assert GameConfig.from_dict(cfg.to_dict()) == cfg
    assert GameConfig.from_dict(json.loads(json.dumps(cfg.to_dict()))) == cfg
    assert GameConfig.from_dict(cfg.to_dict()).to_dict() == cfg.to_dict()


def test_to_dict_of_a_patched_config_reflects_the_patch() -> None:
    d = custom_config().to_dict()
    assert d["demand"]["weekday_peak"] == {"from": "mon", "to": "wed"}
    assert d["random_seed"] == 42
    assert d["peer_market"] == {"enabled": False}
    assert ordered(d) is not None and list(d) == list(DEFAULT_CONFIG)


def test_tier_terms_for_defaults() -> None:
    terms = GameConfig.default().tier_terms()
    assert list(terms) == [CIP, DAP, DDP]
    assert terms == {
        CIP: InsuredTierTerms(enabled=True, fee=0.55),
        DAP: InsuredTierTerms(enabled=True, fee=0.55),
        DDP: InsuredTierTerms(enabled=True, fee=1.39),
    }
    assert IncotermTier.EXW not in terms


def test_tier_terms_follow_the_config() -> None:
    terms = custom_config().tier_terms()
    assert terms == {
        CIP: InsuredTierTerms(enabled=False, fee=0.3),
        DAP: InsuredTierTerms(enabled=False, fee=0.5),
        DDP: InsuredTierTerms(enabled=False, fee=1.2),
    }
    mixed = GameConfig.from_host_input(
        {
            "insurance": {
                "tiers": {"cip": {"fee": 0.1}, "ddp": {"enabled": False, "fee": 2}}
            }
        }
    ).tier_terms()
    assert mixed[CIP] == InsuredTierTerms(enabled=True, fee=0.1)
    assert mixed[DAP] == InsuredTierTerms(enabled=True, fee=0.55)
    assert mixed[DDP] == InsuredTierTerms(enabled=False, fee=2.0)


def test_format_helpers_agree_with_message_copy() -> None:
    err = raised(
        {
            "supplier": {"base_price": 20, "spot_spread": 0.5},
            "stock": {"salvage_value": 21},
        }
    )
    assert err.message == f"Must not exceed the base price ({format_money(20)})."


# --------------------------------------------------------------------------- #
# Criterion 12: purity of the modules
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module", ["app.core.defaults", "app.core.config_models"])
def test_modules_import_only_stdlib_pydantic_and_app_core(module: str) -> None:
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None
    with open(spec.origin, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            ok = top in stdlib or top == "pydantic" or name.startswith("app.core")
            assert ok, f"{module} imports {name}"
            assert not name.startswith("app.core.checks")
            assert name != "app.core.firebase"
