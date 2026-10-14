"""Black-box tests for ``app.core.stats`` (plan section 10)."""

from __future__ import annotations

import itertools
import json
import random
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.calendar import weekday_of
from app.core.enums import Incoterm, TradeSide
from app.core.money import round_cents
from app.core.records import (
    DayRecord,
    FinalSettlement,
    MarketDay,
    PeerBuy,
    PeerSell,
    PeerTrade,
)
from app.core.rng import stream
from app.core.stats import build_stats, rank_players

APPROX = {"abs": 1e-9}

LEADERBOARD_KEYS = {
    "rank",
    "alias",
    "display_name",
    "total_profit",
    "fill_rate",
    "waste_rate",
}
PLAYER_KEYS = {
    "alias",
    "display_name",
    "rank",
    "total_profit",
    "revenue_sales",
    "revenue_salvage",
    "revenue_liquidation",
    "refund_insurance",
    "units_sold",
    "overall_market_share",
    "fill_rate",
    "days_with_backlog",
    "cost_backlog",
    "waste_rate",
    "units_spoiled",
    "liquidated_units",
    "units_lost_transport",
    "cost_insurance",
    "insurance_net",
    "futures_units",
    "futures_unfilled",
    "futures_cancelled_units",
    "futures_vs_spot",
    "futures_share",
    "cost_conservation",
    "lots_conserved",
    "shortfall_spot_units",
    "peer_units_bought",
    "peer_units_sold",
    "cost_peer",
    "revenue_peer",
    "peer_net",
    "avg_reputation",
    "final_reputation",
    "days_cooperated",
    "peak_stock",
    "peak_backlog",
    "incoterm_days",
    "series",
    "final_settlement",
}
PLAYER_SERIES_KEYS = {
    "delivered",
    "peer_bought",
    "peer_sold",
    "sold",
    "spoiled",
    "backlog_end",
    "closing_stock",
    "reputation",
    "profit",
    "cooperate",
}
MARKET_KEYS = {
    "demand_total",
    "units_sold_total",
    "cooperation_days",
    "failure_days",
    "disruption_days",
    "avg_spot_price",
    "min_spot_price",
    "max_spot_price",
    "peer_units_total",
    "peer_trade_days",
    "avg_peer_price",
    "series",
}
MARKET_SERIES_KEYS = {
    "weekday",
    "weekday_multiplier",
    "demand",
    "delivered",
    "sold",
    "spoiled",
    "backlog_end",
    "spot_price",
    "capacity",
    "cooperators",
    "cooperation_reached",
    "refrigeration_failure",
    "disrupted",
    "peer_units",
    "peer_avg_price",
}


# ---------------------------------------------------------------- builders


def rec(day: int = 1, **kw: Any) -> DayRecord:
    base: dict[str, Any] = dict(
        day=day,
        weekday=weekday_of(day),
        order_spot=0,
        order_futures=0,
        incoterm=Incoterm.EXW,
        conserve_lots=(),
        lots_conserved=0,
        cooperate=False,
        peer_buy=None,
        peer_sell=None,
        outlay=0.0,
        opening_stock=0,
        futures_due=0,
        futures_filled=0,
        futures_unfilled=0,
        allocated_spot=0,
        shortfall_spot=0,
        shipped=0,
        lost_transport=0,
        delivered=0,
        peer_bought=0,
        peer_sold=0,
        peer_trades=(),
        demand=0,
        backlog_open=0,
        sold=0,
        backlog_end=0,
        spoiled=0,
        closing_stock=0,
        revenue_sales=0.0,
        revenue_salvage=0.0,
        revenue_peer=0.0,
        refund_insurance=0.0,
        refund_futures=0.0,
        cost_spot=0.0,
        cost_futures=0.0,
        cost_peer=0.0,
        cost_transport=0.0,
        cost_insurance=0.0,
        cost_conservation=0.0,
        cost_holding=0.0,
        cost_backlog=0.0,
        profit=0.0,
        cash_open=0.0,
        cash_end=0.0,
        futures_vs_spot=0.0,
        fill=1.0,
        waste=0.0,
        reputation_open=0.5,
        reputation_end=0.5,
        market_share=0.0,
        refrigeration_failure=False,
    )
    base.update(kw)
    return DayRecord(**base)


def mday(day: int = 1, **kw: Any) -> MarketDay:
    base: dict[str, Any] = dict(
        day=day,
        weekday=weekday_of(day),
        weekday_multiplier=1.0,
        expected_demand=0.0,
        demand_total=0,
        capacity=0,
        disrupted=False,
        spot_price=12.0,
        cooperation=False,
        cooperators=0,
        refrigeration_failure=False,
        units_sold_total=0,
        peer_units=0,
        peer_avg_price=None,
    )
    base.update(kw)
    return MarketDay(**base)


def settle(**kw: Any) -> FinalSettlement:
    base: dict[str, Any] = dict(
        liquidated_units=0,
        revenue_liquidation=0.0,
        refund_insurance_in_transit=0.0,
        futures_cancelled_units=0,
        refund_futures_cancelled=0.0,
        settlement_total=0.0,
    )
    base.update(kw)
    return FinalSettlement(**base)


def build(
    records: dict[str, list[DayRecord]],
    market: list[MarketDay] | None = None,
    settlements: dict[str, FinalSettlement] | None = None,
    seed: int = 1,
    starting_reputation: float = 0.5,
) -> dict:
    settlements = settlements or {}
    full = {a: settlements.get(a, settle()) for a in records}
    return build_stats(
        records=records,
        market=market or [],
        settlements=full,
        display_names={a: f"Pulqueria {a}" for a in records},
        starting_reputation=starting_reputation,
        seed=seed,
    )


def example_one() -> dict:
    p1 = [
        rec(
            1,
            demand=20,
            sold=18,
            spoiled=2,
            backlog_end=2,
            closing_stock=5,
            delivered=25,
            shipped=25,
            futures_filled=0,
            order_futures=10,
            profit=100.00,
            reputation_end=0.55,
            cooperate=True,
            incoterm=Incoterm.CIP,
        ),
        rec(
            2,
            demand=30,
            backlog_open=2,
            sold=32,
            spoiled=0,
            backlog_end=0,
            closing_stock=3,
            delivered=30,
            shipped=30,
            futures_filled=10,
            order_futures=0,
            profit=150.50,
            reputation_end=0.62,
            cooperate=False,
            incoterm=Incoterm.EXW,
        ),
    ]
    p2 = [
        rec(
            1,
            demand=20,
            sold=20,
            delivered=20,
            shipped=20,
            profit=80.00,
            reputation_end=0.65,
            cooperate=True,
        ),
        rec(
            2,
            demand=30,
            sold=25,
            spoiled=5,
            backlog_end=5,
            delivered=30,
            shipped=30,
            profit=260.50,
            reputation_end=0.60,
        ),
    ]
    market = [
        mday(1, demand_total=40, units_sold_total=38),
        mday(2, demand_total=60, units_sold_total=57),
    ]
    st1 = settle(liquidated_units=5, revenue_liquidation=10.0, settlement_total=10.0)
    return build({"P1": p1, "P2": p2}, market, {"P1": st1})


def peer_example() -> dict:
    p1 = [
        rec(1, shortfall_spot=6, peer_bought=4, cost_peer=60.00),
        rec(2, shortfall_spot=3, peer_bought=1, cost_peer=14.50),
    ]
    p2 = [
        rec(1, peer_sold=4, revenue_peer=60.00),
        rec(2, peer_sold=1, revenue_peer=14.50),
    ]
    market = [
        mday(1, peer_units=4, peer_avg_price=15.0),
        mday(2, peer_units=1, peer_avg_price=14.5),
    ]
    return build({"P1": p1, "P2": p2}, market)


def mixed_scenario() -> tuple[dict, dict[str, list[DayRecord]], list[MarketDay]]:
    p1 = [
        rec(
            1,
            demand=12,
            sold=10,
            spoiled=1,
            backlog_end=2,
            closing_stock=7,
            delivered=10,
            shipped=12,
            futures_filled=4,
            order_futures=5,
            shortfall_spot=3,
            peer_bought=2,
            cost_peer=20.0,
            lost_transport=2,
            lots_conserved=1,
            cost_conservation=2.5,
            refund_insurance=1.1,
            cost_insurance=5.0,
            revenue_sales=100.1,
            revenue_salvage=1.0,
            cost_backlog=0.1,
            futures_vs_spot=0.1,
            profit=50.0,
            reputation_end=0.6,
            cooperate=True,
            incoterm=Incoterm.CIP,
        ),
        rec(
            2,
            demand=8,
            backlog_open=2,
            sold=10,
            delivered=5,
            shipped=3,
            peer_bought=1,
            cost_peer=5.0,
            refund_insurance=0.1,
            cost_insurance=3.0,
            revenue_sales=100.2,
            cost_backlog=0.2,
            futures_vs_spot=0.2,
            profit=20.0,
            closing_stock=3,
            reputation_end=0.5,
        ),
        rec(
            3,
            demand=1,
            spoiled=2,
            backlog_end=1,
            closing_stock=9,
            revenue_sales=0.1,
            profit=-3.5,
            reputation_end=0.4,
            cooperate=True,
            incoterm=Incoterm.DAP,
        ),
    ]
    p2 = [
        rec(
            1,
            demand=10,
            sold=10,
            delivered=4,
            peer_sold=2,
            revenue_peer=20.0,
            profit=1.0,
        ),
        rec(
            2,
            demand=10,
            sold=8,
            delivered=6,
            backlog_end=2,
            peer_sold=1,
            revenue_peer=5.0,
            profit=1.0,
        ),
        rec(
            3,
            demand=10,
            backlog_open=2,
            sold=12,
            delivered=8,
            spoiled=1,
            profit=1.0,
        ),
    ]
    market = [
        mday(
            1,
            weekday_multiplier=1.0,
            demand_total=22,
            units_sold_total=20,
            capacity=50,
            disrupted=True,
            spot_price=12.0,
            cooperation=True,
            cooperators=1,
            peer_units=2,
            peer_avg_price=10.0,
        ),
        mday(
            2,
            weekday_multiplier=0.5,
            demand_total=18,
            units_sold_total=18,
            capacity=40,
            spot_price=13.0,
            refrigeration_failure=True,
            peer_units=1,
            peer_avg_price=5.0,
        ),
        mday(
            3,
            weekday_multiplier=0.25,
            demand_total=11,
            units_sold_total=12,
            capacity=30,
            disrupted=True,
            spot_price=14.5,
            cooperation=True,
            cooperators=1,
        ),
    ]
    records = {"P1": p1, "P2": p2}
    settlements = {
        "P1": settle(
            liquidated_units=4,
            revenue_liquidation=8.0,
            refund_insurance_in_transit=2.2,
            futures_cancelled_units=3,
            settlement_total=10.2,
        )
    }
    return build(records, market, settlements), records, market


def flow(r: random.Random) -> dict[str, int]:
    demand, opened = r.randint(0, 50), r.randint(0, 9)
    sold = r.randint(0, demand + opened)
    return dict(
        demand=demand,
        backlog_open=opened,
        sold=sold,
        backlog_end=demand + opened - sold,
    )


def random_data(
    seed: int,
) -> tuple[
    dict[str, list[DayRecord]], list[MarketDay], dict[str, FinalSettlement], dict
]:
    r = random.Random(seed)
    aliases = [f"P{i}" for i in range(1, 5)]
    days = 6
    records: dict[str, list[DayRecord]] = {}
    for a in aliases:
        rows = []
        for d in range(1, days + 1):
            trades = (
                PeerTrade(TradeSide.BUY, "P9", f"B{d}-1", 3, 61, 12.5),
                PeerTrade(TradeSide.SELL, "P8", f"L{d}", 2, 70, 13.0),
            )
            rows.append(
                rec(
                    d,
                    order_spot=r.randint(0, 40),
                    order_futures=r.randint(0, 40),
                    incoterm=r.choice(list(Incoterm)),
                    conserve_lots=("L1", "L2"),
                    cooperate=r.random() < 0.5,
                    peer_buy=PeerBuy(16.0, 90),
                    peer_sell=PeerSell(15.0, 60, 10),
                    **flow(r),
                    spoiled=r.randint(0, 9),
                    closing_stock=r.randint(0, 99),
                    delivered=r.randint(0, 50),
                    shipped=r.randint(0, 50),
                    futures_filled=r.randint(0, 20),
                    peer_bought=r.randint(0, 5),
                    peer_sold=r.randint(0, 5),
                    peer_trades=trades,
                    cost_peer=round(r.uniform(0, 80), 2),
                    revenue_peer=round(r.uniform(0, 80), 2),
                    profit=round(r.uniform(-50, 150), 2),
                    revenue_sales=round(r.uniform(0, 300), 2),
                    reputation_end=round(r.uniform(0.2, 1.0), 4),
                )
            )
        records[a] = rows
    market = [
        mday(
            d,
            weekday_multiplier=r.choice([0.25, 0.5, 1.0]),
            demand_total=r.randint(0, 200),
            units_sold_total=r.randint(0, 200),
            capacity=r.randint(0, 200),
            spot_price=round(r.uniform(8, 20), 2),
            cooperation=r.random() < 0.5,
            cooperators=r.randint(0, 4),
            peer_units=r.randint(0, 6) if d % 2 else 0,
            peer_avg_price=12.5 if d % 2 else None,
        )
        for d in range(1, days + 1)
    ]
    settlements = {
        a: settle(
            liquidated_units=r.randint(0, 9),
            revenue_liquidation=round(r.uniform(0, 30), 2),
            settlement_total=round(r.uniform(0, 30), 2),
        )
        for a in aliases
    }
    out = build_stats(
        records=records,
        market=market,
        settlements=settlements,
        display_names={a: f"N{a}" for a in aliases},
        starting_reputation=0.5,
        seed=seed,
    )
    return records, market, settlements, out


def ref_rank(
    totals: dict[str, tuple[float, float | None, float | None]], seed: int
) -> list[str]:
    """Independent restatement of spec 8.5 for comparison."""
    rng = stream(seed, 0, "rank")
    draws = {a: rng.random() for a in sorted(totals)}

    def key(a: str) -> tuple[float, float, float, float]:
        profit, fill, waste = totals[a]
        return (
            -profit,
            -(1.0 if fill is None else fill),
            0.0 if waste is None else waste,
            draws[a],
        )

    return sorted(totals, key=key)


# ------------------------------------------------- criterion 1: shape / keys


def check_shapes(out: dict) -> None:
    assert set(out) == {"leaderboard", "players", "market"}
    for row in out["leaderboard"]:
        assert set(row) == LEADERBOARD_KEYS
    for stats in out["players"].values():
        assert set(stats) == PLAYER_KEYS
        assert set(stats["series"]) == PLAYER_SERIES_KEYS
        assert set(stats["incoterm_days"]) == {"exw", "cip", "dap", "ddp"}
    assert set(out["market"]) == MARKET_KEYS
    assert set(out["market"]["series"]) == MARKET_SERIES_KEYS


def test_shapes_example_one() -> None:
    check_shapes(example_one())


def test_shapes_random_data() -> None:
    check_shapes(random_data(3)[3])


def test_player_without_records_and_game_without_market_days() -> None:
    out = build(
        {"P1": [], "P2": []},
        market=[],
        starting_reputation=0.42,
    )
    check_shapes(out)
    assert sorted(r["rank"] for r in out["leaderboard"]) == [1, 2]
    for alias, p in out["players"].items():
        assert p["alias"] == alias
        assert p["display_name"] == f"Pulqueria {alias}"
        assert p["total_profit"] == 0
        assert p["units_sold"] == 0
        assert p["overall_market_share"] == 0
        assert p["fill_rate"] is None
        assert p["waste_rate"] is None
        assert p["futures_share"] is None
        assert p["avg_reputation"] == pytest.approx(0.42, **APPROX)
        assert p["final_reputation"] == pytest.approx(0.42, **APPROX)
        assert p["peak_stock"] == 0
        assert p["peak_backlog"] == 0
        assert p["days_with_backlog"] == 0
        assert p["days_cooperated"] == 0
        assert p["incoterm_days"] == {"exw": 0, "cip": 0, "dap": 0, "ddp": 0}
        assert all(v == [] for v in p["series"].values())
    m = out["market"]
    assert m["demand_total"] == 0
    assert m["units_sold_total"] == 0
    assert m["cooperation_days"] == 0
    assert m["failure_days"] == 0
    assert m["disruption_days"] == 0
    assert m["peer_units_total"] == 0
    assert m["peer_trade_days"] == 0
    assert m["avg_spot_price"] is None
    assert m["min_spot_price"] is None
    assert m["max_spot_price"] is None
    assert m["avg_peer_price"] is None
    assert all(v == [] for v in m["series"].values())


def test_every_display_name_player_appears() -> None:
    out = build({"P1": [rec(1)], "P2": [], "P3": []})
    assert set(out["players"]) == {"P1", "P2", "P3"}
    assert {r["alias"] for r in out["leaderboard"]} == {"P1", "P2", "P3"}


# ------------------------------------------------- criterion 2: worked examples


def test_example_one_players() -> None:
    out = example_one()
    p1, p2 = out["players"]["P1"], out["players"]["P2"]
    assert p1["total_profit"] == pytest.approx(260.5, **APPROX)
    assert p1["units_sold"] == 50
    assert p1["fill_rate"] == pytest.approx(1.0, **APPROX)
    assert p1["waste_rate"] == pytest.approx(2 / 52, **APPROX)
    assert p1["futures_units"] == 10
    assert p1["futures_share"] == pytest.approx(10 / 55, **APPROX)
    assert p1["days_with_backlog"] == 1
    assert p1["peak_backlog"] == 2
    assert p1["avg_reputation"] == pytest.approx(0.585, **APPROX)
    assert p1["final_reputation"] == pytest.approx(0.62, **APPROX)
    assert p1["days_cooperated"] == 1
    assert p1["incoterm_days"] == {"exw": 1, "cip": 1, "dap": 0, "ddp": 0}
    assert p1["overall_market_share"] == pytest.approx(50 / 95, **APPROX)
    assert p1["series"]["profit"] == pytest.approx([100.0, 150.5], **APPROX)
    assert p1["revenue_liquidation"] == pytest.approx(10.0, **APPROX)
    assert p1["liquidated_units"] == 5
    assert p2["total_profit"] == pytest.approx(340.5, **APPROX)
    assert p2["fill_rate"] == pytest.approx(45 / 50, **APPROX)
    assert p2["waste_rate"] == pytest.approx(5 / 50, **APPROX)
    assert p2["futures_share"] == pytest.approx(0.0, **APPROX)


def test_example_one_leaderboard() -> None:
    out = example_one()
    board = out["leaderboard"]
    assert [(r["rank"], r["alias"]) for r in board] == [(1, "P2"), (2, "P1")]
    assert board[0]["total_profit"] == pytest.approx(340.5, **APPROX)
    assert board[1]["total_profit"] == pytest.approx(260.5, **APPROX)
    assert board[0]["display_name"] == "Pulqueria P2"
    assert out["players"]["P2"]["rank"] == 1
    assert out["players"]["P1"]["rank"] == 2


def test_example_one_market_totals() -> None:
    m = example_one()["market"]
    assert m["demand_total"] == 100
    assert m["units_sold_total"] == 95
    assert m["peer_units_total"] == 0
    assert m["peer_trade_days"] == 0
    assert m["series"]["delivered"] == [45, 60]
    assert m["series"]["spoiled"] == [2, 5]
    assert m["series"]["backlog_end"] == [2, 5]
    assert m["series"]["sold"] == [38, 57]
    assert m["series"]["demand"] == [40, 60]


def test_peer_example() -> None:
    out = peer_example()
    p1, p2 = out["players"]["P1"], out["players"]["P2"]
    assert p1["shortfall_spot_units"] == 9
    assert p1["peer_units_bought"] == 5
    assert p1["cost_peer"] == pytest.approx(74.5, **APPROX)
    assert p1["revenue_peer"] == pytest.approx(0.0, **APPROX)
    assert p1["peer_net"] == pytest.approx(-74.5, **APPROX)
    assert p1["series"]["peer_bought"] == [4, 1]
    assert p1["series"]["peer_sold"] == [0, 0]
    assert p1["units_sold"] == 0
    assert p2["peer_units_sold"] == 5
    assert p2["revenue_peer"] == pytest.approx(74.5, **APPROX)
    assert p2["peer_net"] == pytest.approx(74.5, **APPROX)
    m = out["market"]
    assert m["peer_units_total"] == 5
    assert m["peer_trade_days"] == 2
    assert m["avg_peer_price"] == pytest.approx(14.9, **APPROX)
    assert m["series"]["peer_units"] == [4, 1]
    assert m["series"]["peer_avg_price"] == pytest.approx([15.0, 14.5], **APPROX)


# ----------------------------------------- per-key semantics (spec 8.6, plan 3)


def test_mixed_scenario_player_one() -> None:
    out, _, _ = mixed_scenario()
    p = out["players"]["P1"]
    assert p["total_profit"] == pytest.approx(76.7, **APPROX)
    assert p["revenue_sales"] == pytest.approx(200.4, **APPROX)
    assert p["revenue_salvage"] == pytest.approx(1.0, **APPROX)
    assert p["revenue_liquidation"] == pytest.approx(8.0, **APPROX)
    assert p["liquidated_units"] == 4
    assert p["futures_cancelled_units"] == 3
    assert p["refund_insurance"] == pytest.approx(1.2, **APPROX)
    assert p["cost_insurance"] == pytest.approx(8.0, **APPROX)
    assert p["insurance_net"] == pytest.approx(-4.6, **APPROX)
    assert p["units_sold"] == 20
    assert p["units_spoiled"] == 3
    assert p["units_lost_transport"] == 2
    assert p["fill_rate"] == pytest.approx(20 / 21, **APPROX)
    assert p["waste_rate"] == pytest.approx(3 / 23, **APPROX)
    assert p["futures_units"] == 5
    assert p["futures_share"] == pytest.approx(4 / 15, **APPROX)
    assert p["futures_vs_spot"] == pytest.approx(0.3, **APPROX)
    assert p["cost_backlog"] == pytest.approx(0.3, **APPROX)
    assert p["cost_conservation"] == pytest.approx(2.5, **APPROX)
    assert p["lots_conserved"] == 1
    assert p["shortfall_spot_units"] == 3
    assert p["peer_units_bought"] == 3
    assert p["peer_units_sold"] == 0
    assert p["cost_peer"] == pytest.approx(25.0, **APPROX)
    assert p["peer_net"] == pytest.approx(-25.0, **APPROX)
    assert p["avg_reputation"] == pytest.approx(0.5, **APPROX)
    assert p["final_reputation"] == pytest.approx(0.4, **APPROX)
    assert p["days_cooperated"] == 2
    assert p["days_with_backlog"] == 2
    assert p["peak_stock"] == 9
    assert p["peak_backlog"] == 2
    assert p["incoterm_days"] == {"exw": 1, "cip": 1, "dap": 1, "ddp": 0}
    assert p["overall_market_share"] == pytest.approx(20 / 50, **APPROX)
    assert p["series"]["delivered"] == [10, 5, 0]
    assert p["series"]["reputation"] == pytest.approx([0.6, 0.5, 0.4], **APPROX)
    assert p["series"]["cooperate"] == [True, False, True]
    assert p["series"]["closing_stock"] == [7, 3, 9]
    assert (
        p["final_settlement"]
        == settle(
            liquidated_units=4,
            revenue_liquidation=8.0,
            refund_insurance_in_transit=2.2,
            futures_cancelled_units=3,
            settlement_total=10.2,
        ).to_payload()
    )


def test_mixed_scenario_player_two_and_rank() -> None:
    out, _, _ = mixed_scenario()
    p = out["players"]["P2"]
    assert p["total_profit"] == pytest.approx(3.0, **APPROX)
    assert p["fill_rate"] == pytest.approx(1.0, **APPROX)
    assert p["waste_rate"] == pytest.approx(1 / 31, **APPROX)
    assert p["futures_share"] is None
    assert p["peer_units_sold"] == 3
    assert p["revenue_peer"] == pytest.approx(25.0, **APPROX)
    assert p["peer_net"] == pytest.approx(25.0, **APPROX)
    assert p["overall_market_share"] == pytest.approx(30 / 50, **APPROX)
    assert out["players"]["P1"]["rank"] == 1
    assert p["rank"] == 2


def test_mixed_scenario_market() -> None:
    out, _, _ = mixed_scenario()
    m = out["market"]
    assert m["demand_total"] == 51
    assert m["units_sold_total"] == 50
    assert m["cooperation_days"] == 2
    assert m["failure_days"] == 1
    assert m["disruption_days"] == 2
    assert m["avg_spot_price"] == pytest.approx(13.17, **APPROX)
    assert m["min_spot_price"] == pytest.approx(12.0, **APPROX)
    assert m["max_spot_price"] == pytest.approx(14.5, **APPROX)
    assert m["peer_units_total"] == 3
    assert m["peer_trade_days"] == 2
    assert m["avg_peer_price"] == pytest.approx(8.33, **APPROX)
    s = m["series"]
    assert s["weekday"] == ["mon", "tue", "wed"]
    assert s["weekday_multiplier"] == pytest.approx([1.0, 0.5, 0.25], **APPROX)
    assert s["demand"] == [22, 18, 11]
    assert s["delivered"] == [14, 11, 8]
    assert s["sold"] == [20, 18, 12]
    assert s["spoiled"] == [1, 0, 3]
    assert s["backlog_end"] == [2, 2, 1]
    assert s["spot_price"] == pytest.approx([12.0, 13.0, 14.5], **APPROX)
    assert s["capacity"] == [50, 40, 30]
    assert s["cooperators"] == [1, 0, 1]
    assert s["cooperation_reached"] == [True, False, True]
    assert s["refrigeration_failure"] == [False, True, False]
    assert s["disrupted"] == [True, False, True]
    assert s["peer_units"] == [2, 1, 0]
    assert s["peer_avg_price"] == [10.0, 5.0, None]


def test_money_sums_are_cent_rounded() -> None:
    records = {
        "P1": [
            rec(d, revenue_sales=0.1, cost_backlog=0.1, profit=0.1) for d in (1, 2, 3)
        ]
    }
    out = build(records)
    p = out["players"]["P1"]
    assert p["revenue_sales"] == 0.3
    assert p["cost_backlog"] == 0.3
    assert p["total_profit"] == 0.3


# --------------------------------------------- criterion 3: score and market share


def test_total_profit_is_sum_plus_settlement_and_shares_sum_to_one() -> None:
    for seed in range(5):
        records, _, settlements, out = random_data(seed)
        for alias, rows in records.items():
            expected = sum(r.profit for r in rows) + settlements[alias].settlement_total
            assert out["players"][alias]["total_profit"] == pytest.approx(
                expected, abs=0.006
            )
            assert out["players"][alias]["total_profit"] == round_cents(
                out["players"][alias]["total_profit"]
            )
        shares = sum(p["overall_market_share"] for p in out["players"].values())
        assert shares == pytest.approx(1.0, **APPROX)


def test_market_share_zero_when_nobody_sold() -> None:
    out = build(
        {
            "P1": [rec(1, demand=5, backlog_end=5)],
            "P2": [rec(1, demand=5, backlog_end=5)],
        }
    )
    assert all(p["overall_market_share"] == 0 for p in out["players"].values())


# ------------------------------------------------------ criterion 4: ranking


def test_rank_profit_dominates() -> None:
    totals = {"P1": (5.0, 0.1, 0.9), "P2": (6.0, 0.0, 1.0)}
    assert rank_players(totals, 1) == ["P2", "P1"]


def test_rank_fill_breaks_profit_tie() -> None:
    for seed in range(10):
        totals = {"P1": (10.0, 0.9, 0.5), "P2": (10.0, 0.8, 0.0)}
        assert rank_players(totals, seed) == ["P1", "P2"]
        totals = {"P1": (10.0, 0.8, 0.0), "P2": (10.0, 0.9, 0.5)}
        assert rank_players(totals, seed) == ["P2", "P1"]


def test_rank_waste_breaks_fill_tie() -> None:
    for seed in range(10):
        assert rank_players({"P1": (10.0, 0.9, 0.1), "P2": (10.0, 0.9, 0.2)}, seed) == [
            "P1",
            "P2",
        ]
        assert rank_players({"P1": (10.0, 0.9, 0.2), "P2": (10.0, 0.9, 0.1)}, seed) == [
            "P2",
            "P1",
        ]


def test_rank_null_waste_counts_as_zero() -> None:
    for seed in range(10):
        assert rank_players(
            {"P1": (10.0, 0.9, 0.1), "P2": (10.0, 0.9, None)}, seed
        ) == ["P2", "P1"]


def test_rank_final_tiebreak_uses_rank_stream() -> None:
    totals = {"P1": (1.0, 0.5, 0.5), "P2": (1.0, 0.5, 0.5)}
    seen = set()
    for seed in range(20):
        rng = stream(seed, 0, "rank")
        d1, d2 = rng.random(), rng.random()
        expected = ["P1", "P2"] if d1 < d2 else ["P2", "P1"]
        assert rank_players(totals, seed) == expected
        seen.add(tuple(expected))
    assert len(seen) == 2


def test_rank_tiebreak_draws_in_string_sort_order() -> None:
    aliases = [f"P{i}" for i in range(1, 13)]
    totals = {a: (1.0, 0.5, 0.5) for a in aliases}
    numeric = {a: i for i, a in enumerate(aliases)}
    differs = False
    for seed in range(8):
        rng = stream(seed, 0, "rank")
        draws = {a: rng.random() for a in sorted(aliases)}
        expected = sorted(aliases, key=lambda a: draws[a])
        assert rank_players(totals, seed) == expected
        rng = stream(seed, 0, "rank")
        wrong = {a: rng.random() for a in aliases}
        if sorted(aliases, key=lambda a: wrong[a]) != expected:
            differs = True
    assert differs, "test data cannot distinguish string from numeric alias order"
    assert numeric  # aliases are P1..P12, so P10 sorts before P2 as a string


def test_rank_tiebreak_drawn_even_without_ties() -> None:
    # A tie among P2 and P3 only: the draws still come from the full alias list.
    totals = {"P1": (9.0, 0.5, 0.5), "P2": (1.0, 0.5, 0.5), "P3": (1.0, 0.5, 0.5)}
    for seed in range(12):
        assert rank_players(totals, seed) == ref_rank(totals, seed)


@given(
    st.dictionaries(
        st.integers(1, 12).map(lambda i: f"P{i}"),
        st.tuples(
            st.sampled_from([0.0, 1.0, 2.0]),
            st.sampled_from([None, 0.0, 0.5, 1.0]),
            st.sampled_from([None, 0.0, 0.5]),
        ),
        min_size=1,
        max_size=12,
    ),
    st.integers(0, 50),
)
def test_rank_matches_reference_and_ignores_insertion_order(
    totals: dict[str, tuple[float, float | None, float | None]], seed: int
) -> None:
    result = rank_players(totals, seed)
    assert result == ref_rank(totals, seed)
    assert sorted(result) == sorted(totals)
    reordered = dict(reversed(list(totals.items())))
    assert rank_players(reordered, seed) == result


def test_null_fill_ranks_before_half_fill_in_build_stats() -> None:
    for seed in range(8):
        records = {
            "P1": [rec(1, demand=10, sold=5, backlog_end=5, profit=10.0)],
            "P2": [rec(1, demand=0, sold=0, profit=10.0)],
        }
        out = build(records, seed=seed)
        assert out["players"]["P2"]["fill_rate"] is None
        assert [r["alias"] for r in out["leaderboard"]] == ["P2", "P1"]


def test_ranks_permutation_leaderboard_agrees_with_player_rank() -> None:
    for seed in range(5):
        _, _, _, out = random_data(seed)
        board = out["leaderboard"]
        n = len(out["players"])
        assert [r["rank"] for r in board] == list(range(1, n + 1))
        assert sorted(r["alias"] for r in board) == sorted(out["players"])
        for row in board:
            player = out["players"][row["alias"]]
            assert player["rank"] == row["rank"]
            assert player["total_profit"] == row["total_profit"]
            assert player["fill_rate"] == row["fill_rate"]
            assert player["waste_rate"] == row["waste_rate"]
        totals = {
            a: (p["total_profit"], p["fill_rate"], p["waste_rate"])
            for a, p in out["players"].items()
        }
        assert [r["alias"] for r in board] == ref_rank(totals, seed)


def test_build_stats_tie_uses_rank_stream() -> None:
    seen = set()
    for seed in range(20):
        records = {
            "P1": [rec(1, demand=10, sold=10, profit=5.0)],
            "P2": [rec(1, demand=10, sold=10, profit=5.0)],
        }
        out = build(records, seed=seed)
        rng = stream(seed, 0, "rank")
        d1, d2 = rng.random(), rng.random()
        expected = ["P1", "P2"] if d1 < d2 else ["P2", "P1"]
        assert [r["alias"] for r in out["leaderboard"]] == expected
        seen.add(tuple(expected))
    assert len(seen) == 2


# -------------------------------------- criterion 5: rank_players determinism


def test_rank_players_all_ties_depends_on_seed_not_insertion_order() -> None:
    aliases = ["P1", "P2", "P3", "P4", "P5"]
    totals = {a: (3.0, 0.5, 0.1) for a in aliases}
    orders = set()
    for seed in range(20):
        base = rank_players(totals, seed)
        assert rank_players(totals, seed) == base
        for perm in itertools.islice(itertools.permutations(aliases), 0, 120, 7):
            shuffled = {a: totals[a] for a in perm}
            assert rank_players(shuffled, seed) == base
        orders.add(tuple(base))
    assert len(orders) > 1


# ------------------------------------------------ criterion 6: series lengths


def test_series_length_and_day_order() -> None:
    records, market, _, out = random_data(11)
    n = len(market)
    for alias, rows in records.items():
        series = out["players"][alias]["series"]
        for name, values in series.items():
            assert len(values) == len(rows), name
        assert series["delivered"] == [r.delivered for r in rows]
        assert series["peer_bought"] == [r.peer_bought for r in rows]
        assert series["peer_sold"] == [r.peer_sold for r in rows]
        assert series["sold"] == [r.sold for r in rows]
        assert series["spoiled"] == [r.spoiled for r in rows]
        assert series["backlog_end"] == [r.backlog_end for r in rows]
        assert series["closing_stock"] == [r.closing_stock for r in rows]
        assert series["reputation"] == [r.reputation_end for r in rows]
        assert series["profit"] == [r.profit for r in rows]
        assert series["cooperate"] == [r.cooperate for r in rows]
    series = out["market"]["series"]
    for name, values in series.items():
        assert len(values) == n, name
    assert series["spot_price"] == [m.spot_price for m in market]
    assert series["weekday"] == [str(m.weekday) for m in market]
    assert series["peer_avg_price"] == [m.peer_avg_price for m in market]
    assert series["delivered"] == [
        sum(rows[i].delivered for rows in records.values()) for i in range(n)
    ]
    assert series["sold"] == [m.units_sold_total for m in market]


def test_series_follow_input_order_not_sorted() -> None:
    rows = [rec(1, sold=1, profit=9.0), rec(2, sold=2, profit=1.0), rec(3, sold=3)]
    out = build({"P1": rows})
    assert out["players"]["P1"]["series"]["sold"] == [1, 2, 3]
    assert out["players"]["P1"]["series"]["profit"] == [9.0, 1.0, 0.0]


# ------------------------------------------------------ criterion 7: parity


def test_parity_through_payload_round_trip() -> None:
    for seed in range(4):
        records, market, settlements, out = random_data(seed)
        records2 = {
            a: [DayRecord.from_payload(r.to_payload()) for r in rows]
            for a, rows in records.items()
        }
        market2 = [MarketDay.from_payload(m.to_payload()) for m in market]
        settlements2 = {
            a: FinalSettlement.from_payload(s.to_payload())
            for a, s in settlements.items()
        }
        out2 = build_stats(
            records=records2,
            market=market2,
            settlements=settlements2,
            display_names={a: f"N{a}" for a in records},
            starting_reputation=0.5,
            seed=seed,
        )
        assert out2 == out


def test_parity_through_json_payloads() -> None:
    records, market, settlements, out = random_data(21)
    records2 = {
        a: [
            DayRecord.from_payload(json.loads(json.dumps(r.to_payload()))) for r in rows
        ]
        for a, rows in records.items()
    }
    market2 = [
        MarketDay.from_payload(json.loads(json.dumps(m.to_payload()))) for m in market
    ]
    settlements2 = {
        a: FinalSettlement.from_payload(json.loads(json.dumps(s.to_payload())))
        for a, s in settlements.items()
    }
    out2 = build_stats(
        records=records2,
        market=market2,
        settlements=settlements2,
        display_names={a: f"N{a}" for a in records},
        starting_reputation=0.5,
        seed=21,
    )
    assert out2 == out


# ---------------------------------------------------- criterion 8: JSON safety


def test_output_survives_json() -> None:
    outputs = [example_one(), peer_example(), mixed_scenario()[0], random_data(5)[3]]
    outputs.append(build({"P1": [], "P2": []}))
    for out in outputs:
        assert json.loads(json.dumps(out)) == out


# --------------------------------------------------- failure modes (plan 6)


def test_fm1_settlement_is_in_the_score() -> None:
    out = example_one()
    assert out["players"]["P1"]["total_profit"] == pytest.approx(260.5, **APPROX)
    assert out["players"]["P1"]["total_profit"] != pytest.approx(250.5, abs=1e-6)
    assert out["leaderboard"][1]["total_profit"] == pytest.approx(260.5, **APPROX)


def test_fm1_settlement_can_change_the_order() -> None:
    records = {"P1": [rec(1, profit=10.0)], "P2": [rec(1, profit=12.0)]}
    out = build(records, settlements={"P1": settle(settlement_total=5.0)})
    assert [r["alias"] for r in out["leaderboard"]] == ["P1", "P2"]


def test_fm2_fill_is_over_demand_not_over_owed() -> None:
    p1 = example_one()["players"]["P1"]
    assert p1["fill_rate"] == pytest.approx(1.0, **APPROX)
    # owed as backlog_open + demand, or as sold + backlog_end, gives 50/52
    assert p1["fill_rate"] != pytest.approx(50 / 52, abs=1e-6)


def test_fm2_fill_with_carried_backlog_every_day() -> None:
    rows = [
        rec(1, demand=10, sold=6, backlog_end=4),
        rec(2, demand=10, backlog_open=4, sold=8, backlog_end=6),
        rec(3, demand=10, backlog_open=6, sold=10, backlog_end=6),
    ]
    p = build({"P1": rows})["players"]["P1"]
    assert p["fill_rate"] == pytest.approx(24 / 30, **APPROX)


def test_fm3_null_fill_counts_as_one() -> None:
    assert rank_players({"P1": (7.0, 0.5, 0.0), "P2": (7.0, None, 0.0)}, 3) == [
        "P2",
        "P1",
    ]
    for seed in range(10):
        assert rank_players({"P1": (7.0, None, 0.0), "P2": (7.0, 0.5, 0.0)}, seed) == [
            "P1",
            "P2",
        ]
        assert rank_players({"P1": (7.0, 0.5, 0.0), "P2": (7.0, None, 0.0)}, seed) == [
            "P2",
            "P1",
        ]


def test_fm4_dict_order_does_not_decide_ties() -> None:
    aliases = [f"P{i}" for i in range(1, 7)]
    totals = {a: (2.0, 0.5, 0.5) for a in aliases}
    results = set()
    for seed in range(15):
        forward = rank_players(totals, seed)
        backward = rank_players(dict(reversed(list(totals.items()))), seed)
        assert forward == backward
        results.add(tuple(forward))
    assert tuple(aliases) not in results or len(results) > 1
    assert len(results) > 1
    # in build_stats too
    for seed in range(15):
        a = build({"P1": [rec(1, profit=1.0)], "P2": [rec(1, profit=1.0)]}, seed=seed)
        b = build({"P2": [rec(1, profit=1.0)], "P1": [rec(1, profit=1.0)]}, seed=seed)
        assert [r["alias"] for r in a["leaderboard"]] == [
            r["alias"] for r in b["leaderboard"]
        ]


def test_fm5_exact_tie_gets_distinct_ranks() -> None:
    for seed in range(6):
        records = {
            "P1": [rec(1, demand=4, sold=4, profit=2.0)],
            "P2": [rec(1, demand=4, sold=4, profit=2.0)],
        }
        out = build(records, seed=seed)
        assert sorted(r["rank"] for r in out["leaderboard"]) == [1, 2]
        assert sorted(p["rank"] for p in out["players"].values()) == [1, 2]


def test_fm6_zero_denominators_give_nulls() -> None:
    out = build({"P1": [rec(1), rec(2)], "P2": [rec(1, demand=3, sold=3)]})
    p = out["players"]["P1"]
    assert p["fill_rate"] is None
    assert p["waste_rate"] is None
    assert p["futures_share"] is None
    assert p["overall_market_share"] == pytest.approx(0.0, **APPROX)
    assert out["leaderboard"][0]["alias"] in {"P1", "P2"}
    row = next(r for r in out["leaderboard"] if r["alias"] == "P1")
    assert row["fill_rate"] is None and row["waste_rate"] is None


def test_fm6_nothing_anywhere_does_not_raise() -> None:
    out = build({"P1": [rec(1)], "P2": [rec(1)]}, market=[mday(1)])
    assert out["market"]["avg_peer_price"] is None
    assert all(p["overall_market_share"] == 0 for p in out["players"].values())


def test_fm7_peer_sales_are_not_sales() -> None:
    out = peer_example()
    p2 = out["players"]["P2"]
    assert p2["peer_units_sold"] == 5
    assert p2["units_sold"] == 0
    assert p2["overall_market_share"] == 0
    assert p2["fill_rate"] is None
    assert out["players"]["P1"]["overall_market_share"] == 0
    assert out["market"]["units_sold_total"] == 0


def test_fm8_no_trade_average_is_null_not_zero() -> None:
    out = example_one()
    assert out["market"]["avg_peer_price"] is None
    assert out["market"]["series"]["peer_avg_price"] == [None, None]
    assert out["market"]["series"]["peer_units"] == [0, 0]


def test_fm9_peer_average_is_value_over_units_not_mean_of_daily_averages() -> None:
    avg = peer_example()["market"]["avg_peer_price"]
    assert avg == pytest.approx(14.9, **APPROX)
    assert avg != pytest.approx(14.75, abs=1e-6)
