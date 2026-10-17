"""Immutable game configuration, host-input validation and player view (spec §9)."""

import copy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.defaults import DEFAULT_CONFIG, PLAYER_CONFIG_KEYS
from app.core.enums import WEEKDAYS, IncotermTier, Weekday
from app.core.insurance import (
    INSURED_TIERS,
    InsuredTierTerms,
    break_even_fee,
    fee_ratio,
)
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

MSG_CONFIG_LOCKED = "The settings are locked once the game starts."

_FROZEN = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class PeakWindow(BaseModel):
    model_config = _FROZEN
    from_: Weekday = Field(alias="from")
    to: Weekday


class DemandConfig(BaseModel):
    model_config = _FROZEN
    per_player_peak: float
    weekday_peak: PeakWindow
    retail_price: float
    backlog_penalty: float


class ReputationConfig(BaseModel):
    model_config = _FROZEN
    starting: float
    smoothing: float
    waste_weight: float


class SupplierConfig(BaseModel):
    model_config = _FROZEN
    base_price: float
    spot_spread: float
    capacity_mean_per_player: float
    capacity_sd_per_player: float
    disruption_probability: float
    disruption_factor: float
    lead_time_days: int
    initial_pipeline_qty: int
    quality_min: int
    quality_max: int


class FuturesConfig(BaseModel):
    model_config = _FROZEN
    horizon_days: int


class StockConfig(BaseModel):
    model_config = _FROZEN
    fresh_days: int
    max_days: int
    holding_cost: float
    initial_stock: int
    salvage_value: float
    conservation_extra_fresh_days: int
    conservation_cost_per_unit: float


class PeerMarketConfig(BaseModel):
    model_config = _FROZEN
    enabled: bool


class TransportConfig(BaseModel):
    model_config = _FROZEN
    unit_cost: float
    shared: bool
    failure_probability: float
    loss_fraction: float


class InsuredTierConfig(BaseModel):
    model_config = _FROZEN
    enabled: bool
    fee: float


class InsuredTiersConfig(BaseModel):
    model_config = _FROZEN
    cip: InsuredTierConfig
    dap: InsuredTierConfig
    ddp: InsuredTierConfig


class InsuranceConfig(BaseModel):
    model_config = _FROZEN
    tiers: InsuredTiersConfig
    target_margin: float
    reference_shipment: int


@dataclass(frozen=True)
class FieldIssue:
    field: str | None
    message: str


class ConfigError(Exception):
    def __init__(self, field: str | None, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class ValidationResult:
    config: "GameConfig | None"
    errors: list[FieldIssue]
    warnings: list[FieldIssue]


@dataclass(frozen=True)
class FieldSpec:
    path: str
    type: str
    min: float | None
    max: float | None
    min_exclusive: bool
    nullable: bool
    players: str


SECRET_KEYS: frozenset[str] = frozenset(
    {
        "random_seed",
        "supplier.capacity_sd_per_player",
        "supplier.disruption_probability",
        "transport.failure_probability",
        "transport.loss_fraction",
        "insurance.target_margin",
        "insurance.reference_shipment",
    }
)


def _spec(
    path: str,
    type_: str,
    lo: float | None = None,
    hi: float | None = None,
    *,
    exclusive: bool = False,
    nullable: bool = False,
) -> FieldSpec:
    if path in PLAYER_CONFIG_KEYS:
        players = "shown"
    elif path in SECRET_KEYS:
        players = "secret"
    else:
        players = "not_shown"
    return FieldSpec(path, type_, lo, hi, exclusive, nullable, players)


_FEE_MAX = 10_000
_MAX_QTY = 9_999

FIELD_SPECS: tuple[FieldSpec, ...] = (
    _spec("max_players", "int", 2, MAX_PLAYERS),
    _spec("duration_days", "int", MIN_DAYS, MAX_DAYS),
    _spec("starting_cash", "float", 0, MAX_MONEY),
    _spec("random_seed", "int", 0, 2**53 - 1, nullable=True),
    _spec("demand.per_player_peak", "float", 1, MAX_LAMBDA),
    _spec("demand.weekday_peak.from", "weekday"),
    _spec("demand.weekday_peak.to", "weekday"),
    _spec("demand.retail_price", "float", 0, _FEE_MAX, exclusive=True),
    _spec("demand.backlog_penalty", "float", 0, _FEE_MAX),
    _spec("reputation.starting", "float", 0, 1),
    _spec("reputation.smoothing", "float", 0, 1),
    _spec("reputation.waste_weight", "float", 0, 1),
    _spec("supplier.base_price", "float", 0, _FEE_MAX, exclusive=True),
    _spec("supplier.spot_spread", "float", 0, 0.9),
    _spec("supplier.capacity_mean_per_player", "float", 0, _MAX_QTY),
    _spec("supplier.capacity_sd_per_player", "float", 0, _MAX_QTY),
    _spec("supplier.disruption_probability", "float", 0, 1),
    _spec("supplier.disruption_factor", "float", 0, 1),
    _spec("supplier.lead_time_days", "int", 0, MAX_LEAD_TIME),
    _spec("supplier.initial_pipeline_qty", "int", 0, _MAX_QTY),
    _spec("supplier.quality_min", "int", 1, 100),
    _spec("supplier.quality_max", "int", 1, 100),
    _spec("futures.horizon_days", "int", 1, MAX_HORIZON),
    _spec("stock.fresh_days", "int", 0, MAX_SHELF_DAYS),
    _spec("stock.max_days", "int", 1, MAX_SHELF_DAYS),
    _spec("stock.holding_cost", "float", 0, _FEE_MAX),
    _spec("stock.initial_stock", "int", 0, _MAX_QTY),
    _spec("stock.salvage_value", "float", 0, _FEE_MAX),
    _spec("stock.conservation_extra_fresh_days", "int", 1, MAX_SHELF_DAYS),
    _spec("stock.conservation_cost_per_unit", "float", 0, _FEE_MAX),
    _spec("peer_market.enabled", "bool"),
    _spec("transport.unit_cost", "float", 0, _FEE_MAX),
    _spec("transport.shared", "bool"),
    _spec("transport.failure_probability", "float", 0, 1),
    _spec("transport.loss_fraction", "float", 0, 1),
    _spec("insurance.tiers.cip.enabled", "bool"),
    _spec("insurance.tiers.cip.fee", "float", 0, _FEE_MAX),
    _spec("insurance.tiers.dap.enabled", "bool"),
    _spec("insurance.tiers.dap.fee", "float", 0, _FEE_MAX),
    _spec("insurance.tiers.ddp.enabled", "bool"),
    _spec("insurance.tiers.ddp.fee", "float", 0, _FEE_MAX),
    _spec("insurance.target_margin", "float", -0.5, 2.0),
    _spec("insurance.reference_shipment", "int", 1, _MAX_QTY),
)

_WEEKDAY_VALUES: tuple[str, ...] = tuple(day.value for day in WEEKDAYS)


def _get(tree: dict[str, Any], path: str) -> Any:
    node: Any = tree
    for part in path.split("."):
        node = node[part]
    return node


def _set(tree: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class GameConfig(BaseModel):
    model_config = _FROZEN
    max_players: int
    duration_days: int
    starting_cash: float
    random_seed: int | None
    demand: DemandConfig
    reputation: ReputationConfig
    supplier: SupplierConfig
    futures: FuturesConfig
    stock: StockConfig
    peer_market: PeerMarketConfig
    transport: TransportConfig
    insurance: InsuranceConfig

    @classmethod
    def default(cls) -> "GameConfig":
        return cls.from_dict(copy.deepcopy(DEFAULT_CONFIG))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameConfig":
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    @classmethod
    def from_host_input(
        cls, patch: dict[str, Any], current: "GameConfig | None" = None
    ) -> "GameConfig":
        result = validate_config(patch, current)
        if result.config is None:
            first = result.errors[0]
            raise ConfigError(first.field, first.message)
        return result.config

    def tier_terms(self) -> dict[IncotermTier, InsuredTierTerms]:
        tiers = self.insurance.tiers
        return {
            IncotermTier.CIP: InsuredTierTerms(tiers.cip.enabled, tiers.cip.fee),
            IncotermTier.DAP: InsuredTierTerms(tiers.dap.enabled, tiers.dap.fee),
            IncotermTier.DDP: InsuredTierTerms(tiers.ddp.enabled, tiers.ddp.fee),
        }


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if key not in out:
            continue
        if isinstance(out[key], dict):
            if isinstance(value, dict):
                out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _unknown_keys(
    patch: dict[str, Any], schema: dict[str, Any], prefix: str
) -> list[FieldIssue]:
    issues: list[FieldIssue] = []
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if key not in schema:
            issues.append(FieldIssue(path, "Unknown setting."))
        elif isinstance(schema[key], dict):
            if isinstance(value, dict):
                issues.extend(_unknown_keys(value, schema[key], f"{path}."))
            else:
                issues.append(FieldIssue(path, "Unknown setting."))
    return issues


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_leaf(spec: FieldSpec, value: Any) -> str | None:
    if value is None:
        return None if spec.nullable else _type_message(spec)
    if spec.type == "bool":
        return None if isinstance(value, bool) else _type_message(spec)
    if spec.type == "weekday":
        ok = isinstance(value, str) and value in _WEEKDAY_VALUES
        return None if ok else _type_message(spec)
    ok = _is_int(value) if spec.type == "int" else _is_number(value)
    if not ok:
        return _type_message(spec)
    assert spec.min is not None and spec.max is not None
    above = value > spec.min if spec.min_exclusive else value >= spec.min
    if above and value <= spec.max:
        return None
    lo, hi = format_number(spec.min), format_number(spec.max)
    if spec.min_exclusive:
        return f"Must be more than {lo} and at most {hi}."
    return f"Must be between {lo} and {hi}."


def _type_message(spec: FieldSpec) -> str:
    if spec.type == "int":
        return "Must be a whole number."
    if spec.type == "float":
        return "Must be a number."
    if spec.type == "bool":
        return "Must be true or false."
    return "Must be one of " + ", ".join(_WEEKDAY_VALUES) + "."


def _cross_field(merged: dict[str, Any], failed: set[str]) -> list[FieldIssue]:
    issues: list[FieldIssue] = []

    def ready(*paths: str) -> bool:
        return not any(p in failed for p in paths)

    fresh, max_days = "stock.fresh_days", "stock.max_days"
    if ready(fresh, max_days) and _get(merged, fresh) >= _get(merged, max_days):
        issues.append(
            FieldIssue(
                max_days,
                "Must be more than the fresh days "
                f"({format_number(_get(merged, fresh))}).",
            )
        )
    frm, to = "demand.weekday_peak.from", "demand.weekday_peak.to"
    if ready(frm, to) and _WEEKDAY_VALUES.index(
        _get(merged, frm)
    ) > _WEEKDAY_VALUES.index(_get(merged, to)):
        issues.append(FieldIssue(to, "The peak window cannot end before it starts."))
    salvage, base = "stock.salvage_value", "supplier.base_price"
    if ready(salvage, base) and _get(merged, salvage) > _get(merged, base):
        issues.append(
            FieldIssue(
                salvage,
                "Must not exceed the base price "
                f"({format_money(_get(merged, base))}).",
            )
        )
    lead, horizon = "supplier.lead_time_days", "futures.horizon_days"
    duration = "duration_days"
    if ready(lead, horizon) and _get(merged, horizon) <= _get(merged, lead):
        issues.append(
            FieldIssue(
                horizon,
                "Must be more days than the lead time "
                f"({format_number(_get(merged, lead))}).",
            )
        )
    if ready(horizon, duration) and _get(merged, horizon) >= _get(merged, duration):
        issues.append(
            FieldIssue(
                horizon,
                "Must be fewer days than the game "
                f"({format_number(_get(merged, duration))}).",
            )
        )
    qmin, qmax = "supplier.quality_min", "supplier.quality_max"
    if ready(qmin, qmax) and _get(merged, qmin) > _get(merged, qmax):
        issues.append(
            FieldIssue(
                qmax,
                "Must be at least the minimum quality "
                f"({format_number(_get(merged, qmin))}%).",
            )
        )
    return issues


def validate_config(
    patch: dict[str, Any], current: GameConfig | None = None
) -> ValidationResult:
    base = current.to_dict() if current is not None else copy.deepcopy(DEFAULT_CONFIG)
    merged = _merge(base, patch)
    errors = _unknown_keys(patch, DEFAULT_CONFIG, "")
    failed: set[str] = set()
    for spec in FIELD_SPECS:
        message = _check_leaf(spec, _get(merged, spec.path))
        if message is not None:
            errors.append(FieldIssue(spec.path, message))
            failed.add(spec.path)
    errors.extend(_cross_field(merged, failed))
    if errors:
        return ValidationResult(None, errors, [])
    for spec in FIELD_SPECS:
        if spec.type == "float":
            _set(merged, spec.path, float(_get(merged, spec.path)))
    config = GameConfig.from_dict(merged)
    return ValidationResult(config, [], config_warnings(config))


def config_warnings(config: GameConfig) -> list[FieldIssue]:
    warnings: list[FieldIssue] = []
    supplier, transport = config.supplier, config.transport
    floor = supplier.base_price * (1 + supplier.spot_spread) + transport.unit_cost
    if config.demand.retail_price <= floor:
        warnings.append(
            FieldIssue(
                "demand.retail_price",
                "On the dearest spot days a unit can lose money.",
            )
        )
    terms = config.tier_terms()
    for tier in INSURED_TIERS:
        if not terms[tier].enabled:
            continue
        break_even = break_even_fee(
            tier,
            failure_probability=transport.failure_probability,
            loss_fraction=transport.loss_fraction,
            shared=transport.shared,
            n_ref=config.max_players,
            reference_shipment=config.insurance.reference_shipment,
            base_price=supplier.base_price,
            retail_price=config.demand.retail_price,
        )
        ratio = fee_ratio(terms[tier].fee, break_even)
        if ratio is not None and (ratio > 3 or ratio < 0.5):
            warnings.append(
                FieldIssue(
                    f"insurance.tiers.{tier.value}.fee",
                    f"Far from break-even (ratio {ratio:.2f}).",
                )
            )
    return warnings


def config_public(config: GameConfig) -> dict[str, Any]:
    full = config.to_dict()
    out: dict[str, Any] = {}
    for path in PLAYER_CONFIG_KEYS:
        _set(out, path, copy.deepcopy(_get(full, path)))
    return out


def config_schema() -> dict[str, Any]:
    return {
        "config": copy.deepcopy(DEFAULT_CONFIG),
        "fields": {
            spec.path: {
                "type": spec.type,
                "min": spec.min,
                "max": spec.max,
                "nullable": spec.nullable,
                "players": spec.players,
            }
            for spec in FIELD_SPECS
        },
        "limits": dict(LIMITS),
    }
