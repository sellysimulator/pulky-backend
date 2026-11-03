"""Shipping insurance: tiers, plan rows and the host calculator (spec 6.3, 6.4)."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.enums import IncotermTier
from app.core.money import round_cents

INCOTERM_TIERS: tuple[IncotermTier, ...] = (
    IncotermTier.EXW,
    IncotermTier.CIP,
    IncotermTier.DAP,
    IncotermTier.DDP,
)
"""Every tier, from no protection to the most."""

INSURED_TIERS: tuple[IncotermTier, ...] = INCOTERM_TIERS[1:]
"""The tiers that refund lost units: the host can disable them and set their fees."""

INCOTERM_TIER_LABELS: dict[IncotermTier, str] = {
    IncotermTier.EXW: "No insurance — you carry the risk",
    IncotermTier.CIP: "Paid-price insurance — refund what you paid",
    IncotermTier.DAP: "Market-price insurance — refund at the arrival-day spot price",
    IncotermTier.DDP: "Retail-price insurance — refund at the retail price",
}
"""Each tier's card label, sent in `tier_plans[].label` (spec section 6.3)."""

REFUND_BASIS: dict[IncotermTier, str] = {
    IncotermTier.EXW: "none",
    IncotermTier.CIP: "paid",
    IncotermTier.DAP: "arrival_spot",
    IncotermTier.DDP: "retail",
}


@dataclass(frozen=True)
class InsuredTierTerms:
    """The host's terms for one insured tier."""

    enabled: bool
    fee: float


def tier_fee(
    tier: IncotermTier, terms: Mapping[IncotermTier, InsuredTierTerms]
) -> float:
    if tier == IncotermTier.EXW:
        return 0.0
    tier_terms = terms[tier]
    if not tier_terms.enabled:
        raise ValueError(f"tier {tier.value} is disabled")
    return tier_terms.fee


def shipment_unit_cost(
    spot_units: int, spot_price: float, futures_units: int, base_price: float
) -> float:
    """A shipment's average price paid (spec 5.6): what Paid-price insurance refunds
    per lost unit, since a lost unit is not traced to spot or futures. The engine
    prices every shipment with it, and the Paid-price card shows its value for today's
    shipment (spec 6.3). 0 when nothing ships."""
    shipped = spot_units + futures_units
    if shipped <= 0:
        return 0.0
    return (spot_price * spot_units + base_price * futures_units) / shipped


def refund_value(
    tier: IncotermTier,
    *,
    paid_unit_cost: float,
    arrival_spot_price: float,
    retail_price: float,
) -> float:
    if tier == IncotermTier.CIP:
        return paid_unit_cost
    if tier == IncotermTier.DAP:
        return arrival_spot_price
    if tier == IncotermTier.DDP:
        return retail_price
    return 0.0


def plan_rows(
    terms: Mapping[IncotermTier, InsuredTierTerms],
    *,
    today_spot_price: float,
    base_price: float,
    retail_price: float,
    spot_units: int,
    futures_units: int,
    lead_time_days: int,
) -> list[dict[str, Any]]:
    """The tier cards (spec 6.3, 12.4). ``refund_value_today`` is what the engine
    would refund per lost unit of today's shipment: ``futures_units`` due to ship
    today plus a spot order of ``spot_units``, both filled in full."""
    # With no futures due, every unit of the shipment is a spot unit, so the refund
    # is the spot price whatever the spot order: one unit stands for any order.
    paid_spot = spot_units if futures_units > 0 else max(spot_units, 1)
    paid = shipment_unit_cost(paid_spot, today_spot_price, futures_units, base_price)
    paid_shipment = (
        {"spot_units": spot_units, "futures_units": futures_units}
        if futures_units > 0
        else None
    )
    rows: list[dict[str, Any]] = []
    for tier in INCOTERM_TIERS:
        if tier == IncotermTier.EXW:
            enabled = True
            fee = 0.0
        else:
            enabled = terms[tier].enabled
            fee = terms[tier].fee
        today: float | None = None
        shipment: dict[str, int] | None = None
        if tier == IncotermTier.CIP:
            today = paid
            shipment = paid_shipment
        elif tier == IncotermTier.DAP and lead_time_days == 0:
            today = today_spot_price  # the shipment arrives today
        elif tier == IncotermTier.DDP:
            today = retail_price
        rows.append(
            {
                "tier": tier.value,
                "label": INCOTERM_TIER_LABELS[tier],
                "enabled": enabled,
                "refund_basis": REFUND_BASIS[tier],
                "refund_value_today": today,
                "refund_shipment": shipment,
                "fee_per_unit": fee,
            }
        )
    return rows


def break_even_fee(
    tier: IncotermTier,
    *,
    failure_probability: float,
    loss_fraction: float,
    shared: bool,
    n_ref: int,
    reference_shipment: int,
    base_price: float,
    retail_price: float,
) -> float:
    if tier == IncotermTier.EXW or reference_shipment <= 0:
        return 0.0
    loss_eff = loss_fraction / n_ref if shared else loss_fraction
    lost_per_unit = (
        failure_probability
        * math.floor(reference_shipment * loss_eff + 1e-9)
        / reference_shipment
    )
    value = retail_price if tier == IncotermTier.DDP else base_price
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
