"""Incoterm tiers, plan rows and the host calculator maths (spec section 6.3, 6.4)."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.enums import Incoterm
from app.core.money import round_cents

TIER_ORDER: tuple[Incoterm, ...] = (
    Incoterm.EXW,
    Incoterm.CIP,
    Incoterm.DAP,
    Incoterm.DDP,
)

TIER_LABELS: dict[Incoterm, str] = {
    Incoterm.EXW: "EXW — you carry the risk",
    Incoterm.CIP: "CIP + blockchain — refund what you paid",
    Incoterm.DAP: "DAP + blockchain — refund at market price",
    Incoterm.DDP: "DDP + blockchain — refund at retail price",
}

REFUND_BASIS: dict[Incoterm, str] = {
    Incoterm.EXW: "none",
    Incoterm.CIP: "paid",
    Incoterm.DAP: "arrival_spot",
    Incoterm.DDP: "retail",
}


@dataclass(frozen=True)
class TierTerms:
    enabled: bool
    fee: float


def tier_fee(incoterm: Incoterm, tiers: Mapping[Incoterm, TierTerms]) -> float:
    if incoterm == Incoterm.EXW:
        return 0.0
    terms = tiers[incoterm]
    if not terms.enabled:
        raise ValueError(f"tier {incoterm.value} is disabled")
    return terms.fee


def refund_value(
    incoterm: Incoterm,
    *,
    paid_unit_cost: float,
    arrival_spot_price: float,
    retail_price: float,
) -> float:
    if incoterm == Incoterm.CIP:
        return paid_unit_cost
    if incoterm == Incoterm.DAP:
        return arrival_spot_price
    if incoterm == Incoterm.DDP:
        return retail_price
    return 0.0


def plan_rows(
    tiers: Mapping[Incoterm, TierTerms],
    *,
    today_spot_price: float,
    retail_price: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tier in TIER_ORDER:
        if tier == Incoterm.EXW:
            enabled = True
            fee = 0.0
        else:
            terms = tiers[tier]
            enabled = terms.enabled
            fee = terms.fee
        today: float | None = None
        if tier == Incoterm.CIP:
            today = today_spot_price
        elif tier == Incoterm.DDP:
            today = retail_price
        rows.append(
            {
                "tier": tier.value,
                "label": TIER_LABELS[tier],
                "enabled": enabled,
                "refund_basis": REFUND_BASIS[tier],
                "refund_value_today": today,
                "fee_per_unit": fee,
            }
        )
    return rows


def break_even_fee(
    incoterm: Incoterm,
    *,
    failure_probability: float,
    loss_fraction: float,
    shared: bool,
    n_ref: int,
    reference_shipment: int,
    base_price: float,
    retail_price: float,
) -> float:
    if incoterm == Incoterm.EXW or reference_shipment <= 0:
        return 0.0
    loss_eff = loss_fraction / n_ref if shared else loss_fraction
    lost_per_unit = (
        failure_probability
        * math.floor(reference_shipment * loss_eff + 1e-9)
        / reference_shipment
    )
    value = retail_price if incoterm == Incoterm.DDP else base_price
    return lost_per_unit * value


def fee_from_margin(break_even: float, target_margin: float) -> float:
    return round_cents(break_even * (1 + target_margin))


def fee_ratio(fee: float, break_even: float) -> float | None:
    if break_even == 0:
        return None
    return fee / break_even


def ratio_badge(fee: float, break_even: float) -> str:
    ratio = fee_ratio(fee, break_even)
    if ratio is None:
        return "no_risk"
    if ratio < 0.95:
        return "worth_it"
    if ratio <= 1.05:
        return "break_even"
    return "not_worth_it"
