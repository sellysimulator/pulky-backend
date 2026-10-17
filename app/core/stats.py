"""End-of-game statistics and ranking (spec §8.5, §8.6)."""

from collections.abc import Mapping, Sequence

from app.core.enums import IncotermTier
from app.core.money import round_cents
from app.core.records import DayRecord, FinalSettlement, MarketDay
from app.core.rng import stream

_EMPTY_SETTLEMENT = FinalSettlement(0, 0.0, 0.0, 0, 0.0, 0.0)


def rank_players(
    totals: Mapping[str, tuple[float, float | None, float | None]], seed: int
) -> list[str]:
    """Aliases in rank order: profit, then fill, then waste, then a seeded draw."""
    rng = stream(seed, 0, "rank")
    tiebreak = {alias: rng.random() for alias in sorted(totals)}

    def key(alias: str) -> tuple[float, float, float, float]:
        profit, fill, waste = totals[alias]
        return (
            -profit,
            -(1.0 if fill is None else fill),
            0.0 if waste is None else waste,
            tiebreak[alias],
        )

    return sorted(totals, key=key)


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _player_stats(
    alias: str,
    recs: Sequence[DayRecord],
    settlement: FinalSettlement,
    display_name: str,
    starting_reputation: float,
    total_sold_all: int,
) -> dict:
    units_sold = sum(r.sold for r in recs)
    units_spoiled = sum(r.spoiled for r in recs)
    demand = sum(r.demand for r in recs)
    shipped = sum(r.shipped for r in recs)
    refund_insurance = round_cents(sum(r.refund_insurance for r in recs))
    cost_insurance = round_cents(sum(r.cost_insurance for r in recs))
    cost_peer = round_cents(sum(r.cost_peer for r in recs))
    revenue_peer = round_cents(sum(r.revenue_peer for r in recs))
    reputations = [r.reputation_end for r in recs]
    incoterm_tier_days = {tier.value: 0 for tier in IncotermTier}
    for r in recs:
        incoterm_tier_days[r.incoterm_tier.value] += 1
    return {
        "alias": alias,
        "display_name": display_name,
        "rank": 0,
        "total_profit": round_cents(
            sum(r.profit for r in recs) + settlement.settlement_total
        ),
        "revenue_sales": round_cents(sum(r.revenue_sales for r in recs)),
        "revenue_salvage": round_cents(sum(r.revenue_salvage for r in recs)),
        "revenue_liquidation": settlement.revenue_liquidation,
        "refund_insurance": refund_insurance,
        "units_sold": units_sold,
        "overall_market_share": (
            units_sold / total_sold_all if total_sold_all > 0 else 0.0
        ),
        "fill_rate": _ratio(units_sold, demand),
        "days_with_backlog": sum(1 for r in recs if r.backlog_end > 0),
        "cost_backlog": round_cents(sum(r.cost_backlog for r in recs)),
        "waste_rate": _ratio(units_spoiled, units_sold + units_spoiled),
        "units_spoiled": units_spoiled,
        "liquidated_units": settlement.liquidated_units,
        "units_lost_transport": sum(r.lost_transport for r in recs),
        "cost_insurance": cost_insurance,
        "insurance_net": round_cents(
            refund_insurance + settlement.refund_insurance_in_transit - cost_insurance
        ),
        "futures_units": sum(r.order_futures for r in recs),
        "futures_unfilled": sum(r.futures_unfilled for r in recs),
        "futures_cancelled_units": settlement.futures_cancelled_units,
        "futures_vs_spot": round_cents(sum(r.futures_vs_spot for r in recs)),
        "futures_share": _ratio(sum(r.futures_filled for r in recs), shipped),
        "cost_conservation": round_cents(sum(r.cost_conservation for r in recs)),
        "lots_conserved": sum(r.lots_conserved for r in recs),
        "shortfall_spot_units": sum(r.shortfall_spot for r in recs),
        "peer_units_bought": sum(r.peer_bought for r in recs),
        "peer_units_sold": sum(r.peer_sold for r in recs),
        "cost_peer": cost_peer,
        "revenue_peer": revenue_peer,
        "peer_net": round_cents(
            sum(r.revenue_peer for r in recs) - sum(r.cost_peer for r in recs)
        ),
        "avg_reputation": (
            sum(reputations) / len(reputations) if reputations else starting_reputation
        ),
        "final_reputation": reputations[-1] if reputations else starting_reputation,
        "days_cooperated": sum(1 for r in recs if r.cooperate),
        "peak_stock": max((r.closing_stock for r in recs), default=0),
        "peak_backlog": max((r.backlog_end for r in recs), default=0),
        "incoterm_tier_days": incoterm_tier_days,
        "series": {
            "delivered": [r.delivered for r in recs],
            "peer_bought": [r.peer_bought for r in recs],
            "peer_sold": [r.peer_sold for r in recs],
            "sold": [r.sold for r in recs],
            "spoiled": [r.spoiled for r in recs],
            "backlog_end": [r.backlog_end for r in recs],
            "closing_stock": [r.closing_stock for r in recs],
            "reputation": reputations,
            "profit": [r.profit for r in recs],
            "cooperate": [r.cooperate for r in recs],
        },
        "final_settlement": settlement.to_payload(),
    }


def _market_stats(
    market: Sequence[MarketDay], records: Mapping[str, Sequence[DayRecord]]
) -> dict:
    by_day: dict[int, dict[str, int]] = {
        m.day: {"delivered": 0, "spoiled": 0, "backlog_end": 0} for m in market
    }
    for recs in records.values():
        for r in recs:
            sums = by_day.get(r.day)
            if sums is not None:
                sums["delivered"] += r.delivered
                sums["spoiled"] += r.spoiled
                sums["backlog_end"] += r.backlog_end
    peer_units_total = sum(m.peer_units for m in market)
    trade_value = sum(r.cost_peer for recs in records.values() for r in recs)
    prices = [m.spot_price for m in market]
    return {
        "demand_total": sum(m.demand_total for m in market),
        "units_sold_total": sum(m.units_sold_total for m in market),
        "cooperation_days": sum(1 for m in market if m.cooperation),
        "failure_days": sum(1 for m in market if m.refrigeration_failure),
        "disruption_days": sum(1 for m in market if m.disrupted),
        "avg_spot_price": round_cents(sum(prices) / len(prices)) if prices else None,
        "min_spot_price": round_cents(min(prices)) if prices else None,
        "max_spot_price": round_cents(max(prices)) if prices else None,
        "peer_units_total": peer_units_total,
        "peer_trade_days": sum(1 for m in market if m.peer_units > 0),
        "avg_peer_price": (
            round_cents(trade_value / peer_units_total)
            if peer_units_total > 0
            else None
        ),
        "series": {
            "weekday": [m.weekday.value for m in market],
            "weekday_multiplier": [m.weekday_multiplier for m in market],
            "demand": [m.demand_total for m in market],
            "delivered": [by_day[m.day]["delivered"] for m in market],
            "sold": [m.units_sold_total for m in market],
            "spoiled": [by_day[m.day]["spoiled"] for m in market],
            "backlog_end": [by_day[m.day]["backlog_end"] for m in market],
            "spot_price": [m.spot_price for m in market],
            "capacity": [m.capacity for m in market],
            "cooperators": [m.cooperators for m in market],
            "cooperation_reached": [m.cooperation for m in market],
            "refrigeration_failure": [m.refrigeration_failure for m in market],
            "disrupted": [m.disrupted for m in market],
            "peer_units": [m.peer_units for m in market],
            "peer_avg_price": [m.peer_avg_price for m in market],
        },
    }


def build_stats(
    *,
    records: Mapping[str, Sequence[DayRecord]],
    market: Sequence[MarketDay],
    settlements: Mapping[str, FinalSettlement],
    display_names: Mapping[str, str],
    starting_reputation: float,
    seed: int,
) -> dict:
    """The one builder of end-of-game statistics (spec §8.6)."""
    total_sold_all = sum(r.sold for recs in records.values() for r in recs)
    players: dict[str, dict] = {}
    for alias, name in display_names.items():
        players[alias] = _player_stats(
            alias,
            records.get(alias, ()),
            settlements.get(alias, _EMPTY_SETTLEMENT),
            name,
            starting_reputation,
            total_sold_all,
        )
    order = rank_players(
        {
            alias: (p["total_profit"], p["fill_rate"], p["waste_rate"])
            for alias, p in players.items()
        },
        seed,
    )
    leaderboard = []
    for rank, alias in enumerate(order, start=1):
        players[alias]["rank"] = rank
        p = players[alias]
        leaderboard.append(
            {
                "rank": rank,
                "alias": alias,
                "display_name": p["display_name"],
                "total_profit": p["total_profit"],
                "fill_rate": p["fill_rate"],
                "waste_rate": p["waste_rate"],
            }
        )
    return {
        "leaderboard": leaderboard,
        "players": players,
        "market": _market_stats(market, records),
    }
