"""Black-box tests for section 11: natural and early end, settlement, futures money."""

import copy

import pytest

from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.engine import PulkyEngine
from app.core.enums import Incoterm, Phase
from app.core.money import round_cents
from app.core.records import Decision

EXW = Incoterm.EXW
NAMES = [
    "Don Agave",
    "La Chula",
    "El Tinacal",
    "Casa Maguey",
    "La Penca",
    "El Jarro",
    "Las Pencas",
    "El Tlachiquero",
    "La Mixiotera",
    "El Aguamiel",
    "La Mayahuel",
    "El Xoconostle",
]
QUIET = {
    "supplier__spot_spread": 0,
    "supplier__capacity_sd_per_player": 0,
    "supplier__disruption_probability": 0,
    "transport__failure_probability": 0,
}


def cfg(**over):
    data = copy.deepcopy(DEFAULT_CONFIG)
    for path, value in over.items():
        keys = path.split("__")
        node = data
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return GameConfig.from_dict(data)


def quiet(n=2, **over):
    return cfg(max_players=n, **{**QUIET, **over})


def seats(n):
    return {f"P{i + 1}": NAMES[i] for i in range(n)}


def start(config, seed=11):
    return PulkyEngine.start(config, seats(config.max_players), seed)


def play(e, decisions=None):
    day = e.day
    for alias in e.aliases:
        e.submit(alias, (decisions or {}).get(alias, Decision.zero(day)))
    return e.resolve_day()


def D(day, spot=0, fut=0, tier=EXW, conserve=(), coop=False, buy=None, sell=None):
    return Decision(day, spot, fut, tier, tuple(conserve), coop, buy, sell)


CIP, DAP, DDP = Incoterm.CIP, Incoterm.DAP, Incoterm.DDP


def short(**over):
    """Seven days, futures horizon 3: the shortest legal game."""
    return quiet(2, duration_days=7, futures__horizon_days=3, **over)


def cash_of(e, alias):
    return e.player_view(alias)["cash"]


def lots_total(e, alias):
    return sum(x["qty"] for x in e.player_view(alias)["cellar"])


# ------------------------------------------------------------- natural end


def test_natural_end_liquidates_only_the_cellar_and_publishes_results():
    e = start(short(demand__per_player_peak=3), seed=5)
    outs = []
    for day in range(1, 8):
        decisions = {}
        if day <= 6:
            decisions["P1"] = D(day, 12)
        if day == 4:
            decisions["P2"] = D(day, 4, 6)  # the last day to buy futures
        outs.append(play(e, decisions))
    assert [o.finished for o in outs] == [False] * 6 + [True]
    assert [o.day for o in outs] == list(range(1, 8))
    assert e.phase == Phase.FINISHED and e.ended_early is False
    assert e.brief() is None and e.awaiting() == []
    settlements = e.settlements()
    assert set(settlements) == {"P1", "P2"}
    for alias in e.aliases:
        last = e.records(alias)[-1]
        s = settlements[alias]
        assert len(e.records(alias)) == 7
        assert s.liquidated_units == last.closing_stock
        assert s.revenue_liquidation == round_cents(2.0 * last.closing_stock)
        assert s.refund_insurance_in_transit == 0.0
        assert s.futures_cancelled_units == 0 and s.refund_futures_cancelled == 0.0
        assert s.settlement_total == s.revenue_liquidation
        view = e.player_view(alias)
        assert view["in_transit"] == [] and view["futures_book"] == []
        assert view["cash"] == round_cents(last.cash_end + s.settlement_total)
        assert view["final_settlement"] == s.to_payload()
        total_profit = sum(r.profit for r in e.records(alias)) + s.settlement_total
        assert view["cash"] == round_cents(5000 + total_profit)
    assert e.settlements()["P1"].liquidated_units > 0
    res = e.results()
    assert res["days_played"] == 7 and res["ended_early"] is False
    assert len(res["leaderboard"]) == 2
    assert set(res["stats"]["players"]) == {"P1", "P2"}
    assert len(e.public_history()) == 7 and len(e.market_history()) == 7


def test_the_open_day_is_not_a_game_day_after_a_natural_end():
    e = start(short())
    for _ in range(7):
        play(e)
    with pytest.raises(ValueError):
        e.end_early()
    with pytest.raises(ValueError):
        e.submit("P1", Decision.zero(7))


# --------------------------------------------------------------- early end


def early_engine():
    config = quiet(
        2,
        supplier__lead_time_days=2,
        supplier__spot_spread=0.25,
        supplier__capacity_mean_per_player=40,
        transport__failure_probability=1.0,
        demand__per_player_peak=1,
    )
    e = start(config, seed=11)
    play(e, {"P1": D(1, 50, 20, DAP), "P2": D(1, 30, 0, CIP)})
    return e


def test_early_end_liquidates_cellar_and_transit_and_refunds_what_is_owed():
    e = early_engine()
    cellar = {a: lots_total(e, a) for a in e.aliases}
    cash = {a: cash_of(e, a) for a in e.aliases}
    transit = {a: e.player_view(a)["in_transit"] for a in e.aliases}
    assert transit["P1"][0]["qty"] == 35 and transit["P1"][0]["lost"] == 15
    assert transit["P2"][0]["qty"] == 21 and transit["P2"][0]["lost"] == 9
    paid2 = e.records("P2")[0].cost_spot / 30
    e.submit("P1", D(2, 5))  # a pending decision for the open day is thrown away
    e.end_early()
    assert e.phase == Phase.FINISHED and e.ended_early is True
    assert e.awaiting() == [] and e.brief() is None
    s1, s2 = e.settlements()["P1"], e.settlements()["P2"]
    assert s1.liquidated_units == cellar["P1"] + 35
    assert s1.revenue_liquidation == round_cents(2.0 * (cellar["P1"] + 35))
    assert s1.refund_insurance_in_transit == 15 * 12.0  # DAP: the base price
    assert s1.futures_cancelled_units == 20
    assert s1.refund_futures_cancelled == 240.0
    assert s1.settlement_total == round_cents(s1.revenue_liquidation + 180.0 + 240.0)
    assert s2.liquidated_units == cellar["P2"] + 21
    assert s2.refund_insurance_in_transit == pytest.approx(9 * paid2, abs=0.011)
    assert s2.futures_cancelled_units == 0 and s2.refund_futures_cancelled == 0.0
    for alias, s in (("P1", s1), ("P2", s2)):
        view = e.player_view(alias)
        assert view["cash"] == round_cents(cash[alias] + s.settlement_total)
        assert view["final_settlement"] == s.to_payload()
        assert view["futures_book"] == []
    assert e.host_view()["players"]["P1"]["open_futures_units"] == 0


def test_early_end_writes_no_record_for_the_open_day():
    # spec 11 failure mode 11
    e = early_engine()
    before = {a: len(e.records(a)) for a in e.aliases}
    history = (len(e.public_history()), len(e.market_history()))
    e.submit("P1", D(2, 5))
    e.end_early()
    assert {a: len(e.records(a)) for a in e.aliases} == before == {"P1": 1, "P2": 1}
    assert (len(e.public_history()), len(e.market_history())) == history == (1, 1)
    res = e.results()
    assert res["ended_early"] is True and res["days_played"] == 1
    # the discarded decision cost nothing: cash moved only by the settlement
    assert cash_of(e, "P1") == round_cents(
        e.records("P1")[0].cash_end + e.settlements()["P1"].settlement_total
    )


def test_early_end_is_only_possible_while_deciding():
    e = start(short())
    e.end_early()
    with pytest.raises(ValueError):
        e.end_early()
    assert e.settlements() != {}


def test_settlements_are_empty_until_the_game_is_finished():
    e = start(short())
    play(e)
    assert e.settlements() == {}


# ---------------------------------------------------------- futures money


def test_futures_are_paid_once_and_refunded_only_when_unfilled_or_cancelled():
    # spec 11 failure mode 4: scarce capacity so that some units go unfilled
    config = short(supplier__capacity_mean_per_player=4, demand__per_player_peak=2)
    e = start(config, seed=13)
    bought = {"P1": 0, "P2": 0}
    for day in range(1, 8):
        decisions = {}
        if day <= 4:
            decisions = {"P1": D(day, 3, 7 + day), "P2": D(day, 0, 5)}
            bought["P1"] += 7 + day
            bought["P2"] += 5
        play(e, decisions)
        for alias in e.aliases:
            recs = e.records(alias)
            open_units = e.host_view()["players"][alias]["open_futures_units"]
            paid = sum(r.cost_futures for r in recs)
            units = sum(r.futures_filled + r.futures_unfilled for r in recs)
            assert paid == pytest.approx(12.0 * (units + open_units))
    for alias in e.aliases:
        recs = e.records(alias)
        assert sum(r.cost_futures for r in recs) == 12.0 * bought[alias]
        unfilled = sum(r.futures_unfilled for r in recs)
        assert unfilled > 0
        assert sum(r.refund_futures for r in recs) == 12.0 * unfilled
        assert sum(r.futures_filled for r in recs) + unfilled == bought[alias]
        assert e.settlements()[alias].futures_cancelled_units == 0


def test_futures_still_open_at_an_early_end_are_refunded_at_the_base_price():
    config = short(supplier__capacity_mean_per_player=4, demand__per_player_peak=2)
    e = start(config, seed=13)
    play(e, {"P1": D(1, 0, 9), "P2": D(1, 0, 5)})
    play(e, {"P1": D(2, 0, 4)})
    e.end_early()
    for alias, qty in (("P1", 13), ("P2", 5)):
        recs = e.records(alias)
        cancelled = e.settlements()[alias].futures_cancelled_units
        assert cancelled == qty  # nothing has shipped by day 2
        assert e.settlements()[alias].refund_futures_cancelled == 12.0 * qty
        paid = sum(r.cost_futures for r in recs)
        assert paid == 12.0 * (
            sum(r.futures_filled + r.futures_unfilled for r in recs) + cancelled
        )


# ------------------------------------------------------------ salvage price 0


def test_salvage_price_zero_earns_nothing_but_spoilage_still_counts_as_waste():
    def run(salvage):
        config = short(
            stock__initial_stock=100,
            stock__salvage_value=salvage,
            demand__per_player_peak=2,
        )
        e = start(config, seed=3)
        outs = [play(e) for _ in range(7)]
        return e, outs

    e0, outs0 = run(0.0)
    e2, outs2 = run(2.0)
    spoiled_days = 0
    for o0, o2 in zip(outs0, outs2, strict=True):
        for alias in ("P1", "P2"):
            r0, r2 = o0.records[alias], o2.records[alias]
            assert r0.revenue_salvage == 0.0
            assert r2.revenue_salvage == round_cents(2.0 * r2.spoiled)
            # salvage never changes what the world does, or how reputation moves
            assert r0.spoiled == r2.spoiled and r0.sold == r2.sold
            assert r0.waste == r2.waste and r0.fill == r2.fill
            assert r0.reputation_end == pytest.approx(r2.reputation_end)
            spoiled_days += r0.spoiled > 0
    assert spoiled_days >= 2
    wasted = [r for r in e0.records("P1") if r.spoiled > 0]
    assert wasted and all(r.waste > 0 for r in wasted)
    for alias in ("P1", "P2"):
        s0, s2 = e0.settlements()[alias], e2.settlements()[alias]
        assert s0.liquidated_units == s2.liquidated_units
        assert s0.revenue_liquidation == 0.0 and s0.settlement_total == 0.0
        assert s2.revenue_liquidation == round_cents(2.0 * s2.liquidated_units)
    assert e0.host_view()["players"]["P1"]["waste_rate"] > 0


def test_salvage_zero_early_end_liquidates_for_nothing():
    config = quiet(2, supplier__lead_time_days=2, stock__salvage_value=0)
    e = start(config)
    play(e, {"P1": D(1, 20)})
    e.end_early()
    s = e.settlements()["P1"]
    assert s.liquidated_units >= 20
    assert s.revenue_liquidation == 0.0 and s.settlement_total == 0.0
