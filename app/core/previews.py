"""The host config screen's figures: previews, calculator and help examples
(spec 6.4, 13.9, 13.10), from the engine's own functions.

Pure. ``config_preview`` takes a host config (spec 9.3: null at each hidden default
the host left blank) and never reads a hidden default: a blank one is replaced by 0
before any figure is computed, and every figure that needs it is null.
"""

import copy
from typing import Any

from app.core.calendar import weekday_of
from app.core.config_models import GameConfig, hidden_keys
from app.core.defaults import HIDDEN_DEFAULT_KEYS
from app.core.demand import market_lambda, shares
from app.core.enums import WEEKDAYS
from app.core.insurance import (
    INSURED_TIERS,
    break_even_fee,
    fee_from_margin,
    fee_ratio,
    ratio_badge,
    reference_refund_value,
    reference_units_lost,
)
from app.core.kernels import weekday_multipliers, weekday_reach
from app.core.lots import guide_rows, hazard, quality
from app.core.money import round_cents
from app.core.peer_market import Bid, Offer, OfferLot, clear_peer_market
from app.core.reputation import day_score, update_reputation
from app.core.rng import stream
from app.core.sampling import halfup
from app.core.supply import allocate, spot_range
from app.core.transport import transport_losses

# The sample inputs of the examples (spec 13.10); the figures come from the engine.
SEAT_EXAMPLES = (2, 4)
BACKLOG_DAYS = (1, 3, 7)
HOLDING_UNITS = 50
SALVAGE_UNITS = 10
CONSERVE_LOT_UNITS = 30
TRANSPORT_UNITS = 40
LOSS_EXAMPLE_UNITS = 20
LOSS_TABLE_SHIPMENTS = (60, 30, 10)
STARTING_ALIASES = ("P1", "P2", "P3", "P4")
CLEAN_DAYS = 7
DAY_SCORE_FILLS = (1.0, 0.8, 0.5)
DAY_SCORE_WASTES = (0.0, 0.2, 0.5)
SPLIT_REPUTATIONS = (0.3, 0.5, 0.8)
CAPACITY_BAND_Z = 1.96  # about 95% of a normal draw
ALLOCATION_ORDERS = {"P1": 60, "P2": 50, "P3": 30}
ALLOCATION_CAPACITY = 100


def _blank_hidden(host_config: dict[str, Any]) -> dict[str, Any]:
    """The host config with each blank hidden default set to 0, never to the default."""
    out = copy.deepcopy(host_config)
    for path in HIDDEN_DEFAULT_KEYS:
        node = out
        parts = path.split(".")
        for part in parts[:-1]:
            node = node[part]
        if node[parts[-1]] is None:
            node[parts[-1]] = 0.0
    return out


def chance_of_at_least_one(probability: float, days: int) -> float:
    """Spec 6.4: the chance a daily event happens at least once in ``days``."""
    return 1 - (1 - probability) ** days


def expected_count(probability: float, days: int) -> float:
    """Spec 6.4: the expected number of days a daily event happens."""
    return probability * days


def _lot_nights(
    initial_quality: int, fresh: int, max_days: int, nights: int
) -> list[dict[str, Any]]:
    """Quality and spoil risk at the check of nights 1..``nights`` for a lot that
    arrives at ``initial_quality`` (spec 3.3, 3.5); null once it is certainly gone."""
    rows: list[dict[str, Any]] = []
    gone = False
    for night in range(1, nights + 1):
        if gone:
            rows.append({"night": night, "quality": None, "risk": None})
            continue
        risk = hazard(night, fresh, max_days, initial_quality)
        rows.append(
            {
                "night": night,
                "quality": quality(night - 1, fresh, max_days, initial_quality),
                "risk": risk,
            }
        )
        gone = risk >= 1
    return rows


def _lot(
    role: str,
    initial_quality: int,
    fresh: int,
    max_days: int,
    nights: int,
    conserved: bool = False,
) -> dict[str, Any]:
    """One example lot; ``role`` says which it is (lowest, middle or highest
    initial quality, plain or conserved), for the client to label."""
    return {
        "role": role,
        "initial_quality": initial_quality,
        "conserved": conserved,
        "nights": _lot_nights(initial_quality, fresh, max_days, nights),
    }


def _weekdays(cfg: GameConfig) -> dict[str, Any]:
    window = cfg.demand.weekday_peak
    mults = weekday_multipliers(window.from_, window.to)
    seats = cfg.max_players
    lam = cfg.demand.per_player_peak
    start, end = WEEKDAYS.index(window.from_), WEEKDAYS.index(window.to)
    average = sum(mults.values()) / len(mults)
    return {
        "days": [
            {
                "weekday": day.value,
                "multiplier": mults[day],
                "in_peak": start <= i <= end,
                "market_demand": market_lambda(lam, seats, mults[day]),
            }
            for i, day in enumerate(WEEKDAYS)
        ],
        "reach": weekday_reach(window.from_, window.to),
        "average_multiplier": average,
        "peak_demand_by_seats": [
            {"seats": n, "units": market_lambda(lam, n, 1.0)}
            for n in sorted({*SEAT_EXAMPLES, seats})
        ],
    }


def _game(cfg: GameConfig, average_multiplier: float) -> dict[str, Any]:
    own = cfg.demand.per_player_peak * average_multiplier
    per_day = own * cfg.supplier.base_price
    return {
        "weekly_cycles": cfg.duration_days / 7,
        "last_weekday": weekday_of(cfg.duration_days).value,
        "own_demand_per_day": own,
        "cost_per_day": round_cents(per_day),
        "days_covered": cfg.starting_cash / per_day if per_day > 0 else 0.0,
    }


def _prices(cfg: GameConfig) -> dict[str, Any]:
    spot = spot_range(cfg.supplier.base_price, cfg.supplier.spot_spread)
    low, high = spot["low"], spot["high"]
    retail = cfg.demand.retail_price
    return {
        "spot_low": low,
        "spot_high": high,
        "margin_at_low": round_cents(retail - low),
        "margin_at_high": round_cents(retail - high),
        "backlog_cost": [
            {"days": n, "cost": round_cents(cfg.demand.backlog_penalty * n)}
            for n in BACKLOG_DAYS
        ],
    }


def _reputation(cfg: GameConfig) -> dict[str, Any]:
    rep = cfg.reputation
    start = shares({a: rep.starting for a in STARTING_ALIASES})
    trajectory = [rep.starting]
    for _ in range(CLEAN_DAYS):
        trajectory.append(
            update_reputation(trajectory[-1], 1.0, 0.0, rep.smoothing, rep.waste_weight)
        )
    split = shares({str(i): r for i, r in enumerate(SPLIT_REPUTATIONS)})
    market = market_lambda(cfg.demand.per_player_peak, len(SPLIT_REPUTATIONS), 1.0)
    return {
        "starting_shares": [{"alias": a, "share": start[a]} for a in STARTING_ALIASES],
        "clean_days": trajectory,
        "day_scores": [
            {
                "fill": fill,
                "scores": [
                    {
                        "waste": waste,
                        "score": day_score(fill, waste, rep.waste_weight),
                    }
                    for waste in DAY_SCORE_WASTES
                ],
            }
            for fill in DAY_SCORE_FILLS
        ],
        "split": [
            {
                "reputation": r,
                "share": split[str(i)],
                "units": halfup(market * split[str(i)]),
            }
            for i, r in enumerate(SPLIT_REPUTATIONS)
        ],
    }


def _supplier(cfg: GameConfig, hidden: frozenset[str]) -> dict[str, Any]:
    s = cfg.supplier
    seats = cfg.max_players
    capacity = s.capacity_mean_per_player * seats
    band = None
    if "supplier.capacity_sd_per_player" not in hidden:
        spread = CAPACITY_BAND_Z * s.capacity_sd_per_player * seats
        band = {
            "low": max(0, halfup(capacity - spread)),
            "high": halfup(capacity + spread),
        }
    disruptions = None
    if "supplier.disruption_probability" not in hidden:
        p = s.disruption_probability
        disruptions = {
            "chance_any": chance_of_at_least_one(p, cfg.duration_days),
            "expected": expected_count(p, cfg.duration_days),
        }
    lead, horizon, days = s.lead_time_days, cfg.futures.horizon_days, cfg.duration_days
    return {
        "expected_capacity": capacity,
        "disrupted_capacity": capacity * s.disruption_factor,
        "capacity_band": band,
        "disruptions": disruptions,
        "lead_time": {
            "arrival_day": 1 + lead,
            "arrival_weekday": weekday_of(1 + lead).value,
            "last_spot_day": days - lead,
        },
        "initial_pipeline": {
            "used": lead > 0,
            "arrivals": [
                {"day": d, "units": s.initial_pipeline_qty} for d in range(1, lead + 1)
            ],
        },
        "futures": {"delivery_day": 1 + horizon, "last_day_to_buy": days - horizon},
    }


def _stock(cfg: GameConfig) -> dict[str, Any]:
    st, s = cfg.stock, cfg.supplier
    fresh, max_days, extra = (
        st.fresh_days,
        st.max_days,
        st.conservation_extra_fresh_days,
    )
    q_min, q_max = s.quality_min, s.quality_max
    q_mid = halfup((q_min + q_max) / 2)
    risk_after_fresh = hazard(fresh + 1, fresh, max_days, q_max)
    nights = max_days + extra
    spoil_specs = [(q_min, False), (q_max, False), (q_min, True), (q_max, True)]
    if q_min == q_max:
        spoil_specs = [(q_min, False), (q_min, True)]
    return {
        "quality_step_per_night": 100 / (max_days - fresh),
        "quality_range": [
            _lot("lowest", q_min, fresh, max_days, max_days),
            _lot("middle", q_mid, fresh, max_days, max_days),
            _lot("highest", q_max, fresh, max_days, max_days),
        ],
        "highest_lot": _lot("highest", q_max, fresh, max_days, max_days),
        "plain_vs_conserved": [
            _lot("plain", q_max, fresh, max_days, nights),
            _lot("conserved", q_max, fresh + extra, max_days + extra, nights, True),
        ],
        "spoil_risk": [
            (
                _lot("conserved", q, fresh + extra, max_days + extra, nights, True)
                if conserved
                else _lot("plain", q, fresh, max_days, nights)
            )
            for q, conserved in spoil_specs
        ],
        "holding": {
            "units": HOLDING_UNITS,
            "cost": round_cents(HOLDING_UNITS * st.holding_cost),
        },
        "salvage": {
            "units": SALVAGE_UNITS,
            "revenue": round_cents(SALVAGE_UNITS * st.salvage_value),
        },
        "conservation": {
            "units": CONSERVE_LOT_UNITS,
            "cost": round_cents(CONSERVE_LOT_UNITS * st.conservation_cost_per_unit),
            "risk_after_fresh": risk_after_fresh,
            "units_saved": CONSERVE_LOT_UNITS * risk_after_fresh,
        },
        "quality_guide": guide_rows(1, fresh, max_days),
    }


def _losses(units: int, loss: float, shared_by: int) -> dict[str, int]:
    """Units a failure destroys from one shipment, alone and shared (spec 6.2)."""
    alone = transport_losses({"P1": units}, True, loss, False)["P1"]
    others = {f"P{i}": units for i in range(2, shared_by + 1)}
    shared = transport_losses({"P1": units, **others}, True, loss, True)["P1"]
    return {"alone": alone, "shared": shared}


def _transport(cfg: GameConfig, hidden: frozenset[str]) -> dict[str, Any]:
    t = cfg.transport
    seats = cfg.max_players
    p_hidden = "transport.failure_probability" in hidden
    l_hidden = "transport.loss_fraction" in hidden
    out: dict[str, Any] = {
        "shipping": {
            "units": TRANSPORT_UNITS,
            "cost": round_cents(TRANSPORT_UNITS * t.unit_cost),
        },
        "failures": None,
        "loss_example": None,
        "loss_table": None,
        "reference_losses": None,
    }
    if not p_hidden:
        out["failures"] = {
            "chance_any": chance_of_at_least_one(
                t.failure_probability, cfg.duration_days
            ),
            "expected": expected_count(t.failure_probability, cfg.duration_days),
        }
    if not l_hidden:
        ships = {f"P{i + 1}": q for i, q in enumerate(LOSS_TABLE_SHIPMENTS)}
        solo = transport_losses(ships, True, t.loss_fraction, False)
        shared = transport_losses(ships, True, t.loss_fraction, True)
        out["loss_example"] = {
            "units": LOSS_EXAMPLE_UNITS,
            **_losses(LOSS_EXAMPLE_UNITS, t.loss_fraction, seats),
        }
        out["loss_table"] = {
            "shared_by": len(ships),
            "shared_fraction": t.loss_fraction / len(ships),
            "rows": [
                {"alias": a, "shipped": q, "alone": solo[a], "shared": shared[a]}
                for a, q in ships.items()
            ],
            "total_alone": sum(solo.values()),
            "total_shared": sum(shared.values()),
        }
        u_ref = cfg.insurance.reference_shipment
        out["reference_losses"] = {
            "units": u_ref,
            **_losses(u_ref, t.loss_fraction, seats),
        }
    return out


def _calculator(cfg: GameConfig, hidden: frozenset[str]) -> dict[str, Any]:
    """Spec 6.4: per insured tier, both transport cases, and the margin."""
    t, ins = cfg.transport, cfg.insurance
    known = not ({"transport.failure_probability", "transport.loss_fraction"} & hidden)
    terms = cfg.tier_terms()
    margin = ins.target_margin
    tiers: list[dict[str, Any]] = []
    for tier in INSURED_TIERS:
        fee = terms[tier].fee
        refund = reference_refund_value(
            tier,
            base_price=cfg.supplier.base_price,
            retail_price=cfg.demand.retail_price,
        )
        cases: dict[str, Any] = {"solo": None, "shared": None}
        current: dict[str, Any] | None = None
        if known:
            for key, shared in (("solo", False), ("shared", True)):
                be = break_even_fee(
                    tier,
                    failure_probability=t.failure_probability,
                    loss_fraction=t.loss_fraction,
                    shared=shared,
                    n_ref=cfg.max_players,
                    reference_shipment=ins.reference_shipment,
                    base_price=cfg.supplier.base_price,
                    retail_price=cfg.demand.retail_price,
                )
                lost = reference_units_lost(
                    t.loss_fraction,
                    shared=shared,
                    n_ref=cfg.max_players,
                    reference_shipment=ins.reference_shipment,
                )
                cases[key] = {
                    "break_even": round_cents(be),
                    "ratio": fee_ratio(fee, be),
                    "badge": ratio_badge(fee, be),
                    "worst_single_failure": round_cents(lost * refund),
                    "fee_from_margin": fee_from_margin(be, margin),
                }
            current = cases["shared" if t.shared else "solo"]
        in_sync = (
            None
            if current is None
            else current["ratio"] is None
            or abs(fee - current["fee_from_margin"]) < 1e-9
        )
        tiers.append(
            {
                "tier": tier.value,
                "enabled": terms[tier].enabled,
                "fee": fee,
                "refund_value": refund,
                "solo": cases["solo"],
                "shared": cases["shared"],
                "current": current,
                "in_sync": in_sync,
            }
        )
    return {
        "known": known,
        "shared_seats": cfg.max_players,
        "target_ratio": 1 + margin,
        "tiers": tiers,
    }


def _examples() -> dict[str, Any]:
    """The worked examples with no knob (spec 5.4, 5.7), played by the engine."""
    allocation = {
        mode: allocate(
            ALLOCATION_ORDERS,
            ALLOCATION_CAPACITY,
            mode == "cooperative",
            stream(0, 1, "preview:allocation"),
        )
        for mode in ("largest_first", "cooperative")
    }
    return {
        "allocation": {
            "capacity": ALLOCATION_CAPACITY,
            "orders": dict(ALLOCATION_ORDERS),
            **allocation,
        },
        "peer_trades": peer_market_example(),
    }


def peer_market_example() -> list[dict[str, Any]]:
    """Spec 5.7's day-5 example, cleared by the engine's matcher."""
    cash = 5000 * 100
    bids = {
        "P1": Bid(shortfall=16, max_price_cents=1600, min_quality=20, cash_cents=cash),
        "P2": Bid(shortfall=12, max_price_cents=1400, min_quality=20, cash_cents=cash),
    }
    offers = {
        "P3": Offer(1500, 20, (OfferLot("L3", 12, 0, 1), OfferLot("L4", 10, 28, 1))),
        "P4": Offer(1300, 30, (OfferLot("L4", 8, 21, 1),)),
    }
    trades = clear_peer_market(
        bids, offers, ("P1", "P2", "P3", "P4"), stream(0, 5, "preview:peer")
    )
    return [
        {
            "buyer": tr.buyer,
            "seller": tr.seller,
            "seller_lot": tr.seller_lot,
            "units": tr.qty,
            "price": tr.price_cents / 100,
            "total": round_cents(tr.qty * tr.price_cents / 100),
        }
        for tr in trades
    ]


def config_preview(host_config: dict[str, Any]) -> dict[str, Any]:
    """Every figure the host config screen shows for a valid host config (spec
    13.9, 13.10, 6.4). A figure that needs a hidden default the host left blank is
    null (spec 9.3); ``hidden`` names those defaults."""
    hidden = hidden_keys(host_config)
    cfg = GameConfig.from_dict(_blank_hidden(host_config))
    weekdays = _weekdays(cfg)
    return {
        "hidden": sorted(hidden),
        "weekdays": weekdays,
        "game": _game(cfg, weekdays["average_multiplier"]),
        "prices": _prices(cfg),
        "reputation": _reputation(cfg),
        "supplier": _supplier(cfg, hidden),
        "stock": _stock(cfg),
        "transport": _transport(cfg, hidden),
        "calculator": _calculator(cfg, hidden),
        "examples": _examples(),
    }
