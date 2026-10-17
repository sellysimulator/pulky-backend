"""Black-box tests for section 11: PulkyEngine.resolve_day (spec 4.4, S1 to S16)."""

import copy
import sys

import pytest

from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.demand import draw_demands, market_lambda, shares
from app.core.engine import DayOutcome, PulkyEngine
from app.core.enums import IncotermTier, Phase, TradeSide, Weekday
from app.core.futures import fill_futures
from app.core.insurance import refund_value
from app.core.kernels import weekday_multiplier
from app.core.lots import Cellar, Lot
from app.core.money import round_cents
from app.core.records import Decision, PeerBuy, PeerSell
from app.core.reputation import fill_ratio, update_reputation, waste_ratio
from app.core.rng import stream
from app.core.supply import allocate
from app.core.transport import draw_failure, transport_losses

EXW = IncotermTier.EXW
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


CIP, DAP, DDP = IncotermTier.CIP, IncotermTier.DAP, IncotermTier.DDP


def view_lots(e, alias):
    return {x["id"]: x for x in e.player_view(alias)["cellar"]}


def market(e, day):
    return e.market_history()[day - 1]


# ---------------------------------------------------------- the worked example


def test_deterministic_day_worked_example():
    # fresh_days 1 keeps L0 safe on night 1 (the default 0 gives it a 1/3 hazard)
    e = start(quiet(2, stock__fresh_days=1), seed=11)
    e.submit("P1", Decision(1, 30, 0, EXW, (), False))
    assert e.outlay("P1", Decision(1, 30, 0, EXW, (), False)) == pytest.approx(390.0)
    e.submit("P2", Decision(1, 10, 0, EXW, (), False))
    assert e.outlay("P2", Decision(1, 10, 0, EXW, (), False)) == pytest.approx(130.0)
    assert e.all_in()
    out = e.resolve_day()
    assert isinstance(out, DayOutcome)
    assert out.day == 1 and out.finished is False
    d = draw_demands(11, 1, {"P1": 6.25, "P2": 6.25})
    r = out.records["P1"]
    assert set(out.records) == {"P1", "P2"}
    assert r.allocated_spot == 30 and r.shipped == 30 and r.delivered == 0
    assert r.shortfall_spot == 0 and r.lost_transport == 0
    assert r.cost_spot == 360.0
    assert r.cost_transport == 30.0
    assert r.cost_insurance == 0.0
    assert r.sold == min(d["P1"], 30)
    assert r.demand == d["P1"]
    assert r.backlog_end == d["P1"] - r.sold
    assert r.spoiled == 0
    assert r.closing_stock == 30 - r.sold
    assert r.opening_stock == 30 and r.cash_open == 5000
    assert r.weekday == Weekday.MON
    assert r.outlay == 390.0
    assert r.profit == round_cents(
        30 * r.sold - 360 - 30 - 0.2 * r.closing_stock - 1.5 * r.backlog_end
    )
    assert r.cash_end == round_cents(5000 + r.profit)
    r2 = out.records["P2"]
    assert r2.allocated_spot == 10 and r2.cost_spot == 120.0 and r2.cost_transport == 10
    assert r2.demand == d["P2"]
    assert e.day == 2 and e.phase == Phase.DECIDING
    assert e.records("P1")[0] == r
    assert e.records("P1") == [r]
    assert e.awaiting() == ["P1", "P2"] and not e.has_submitted("P1")
    # the shipment is in the pipeline and arrives tomorrow as lot L2
    assert e.player_view("P1")["in_transit"] == [
        {
            "shipped_day": 1,
            "arrives_day": 2,
            "qty": 30,
            "lost": 0,
            "incoterm_tier": "exw",
        }
    ]
    out2 = play(e)
    assert out2.records["P1"].delivered == 30
    assert out2.records["P1"].opening_stock == r.closing_stock


def test_public_results_and_market_day_of_the_deterministic_day():
    e = start(quiet(2), seed=11)
    out = play(e, {"P1": D(1, 30), "P2": D(1, 10)})
    d = draw_demands(11, 1, {"P1": 6.25, "P2": 6.25})
    pub = out.public
    r1, r2 = out.records["P1"], out.records["P2"]
    assert pub.day == 1 and pub.weekday == Weekday.MON
    assert pub.demand_total == d["P1"] + d["P2"]
    assert pub.units_sold_total == r1.sold + r2.sold
    assert pub.spot_price == 12.0 and pub.capacity == 44
    assert pub.disrupted is False and pub.cooperation is False
    assert pub.cooperators == 0 and pub.refrigeration_failure is False
    assert pub.peer_units == 0 and pub.peer_avg_price is None
    for alias, rec in out.records.items():
        player = pub.players[alias]
        assert player.units_sold == rec.sold
        assert player.reputation == rec.reputation_end
        assert player.market_share == rec.market_share
    total = r1.sold + r2.sold
    assert r1.market_share == pytest.approx(r1.sold / total)
    assert e.public_history() == [pub]
    md = market(e, 1)
    assert md.day == 1 and md.capacity == 44 and md.spot_price == 12.0
    assert md.weekday == Weekday.MON and md.weekday_multiplier == 0.25
    assert md.expected_demand == 12.5
    assert md.demand_total == pub.demand_total
    assert md.units_sold_total == pub.units_sold_total
    assert e.public_results(1) == pub.to_payload(e.display_names)
    assert e.public_results(1)["players"]["P1"]["display_name"] == NAMES[0]


def test_reputation_fill_and_waste_follow_the_spec_formulas():
    e = start(quiet(2, demand__per_player_peak=60), seed=5)
    for _ in range(5):
        out = play(e, {"P1": D(e.day, 40), "P2": D(e.day, 5)})
        for rec in out.records.values():
            owed = rec.backlog_open + rec.demand
            assert rec.fill == fill_ratio(rec.sold, owed)
            assert rec.waste == waste_ratio(rec.spoiled, rec.sold)
            assert rec.reputation_end == pytest.approx(
                update_reputation(rec.reputation_open, rec.fill, rec.waste, 0.3, 0.3)
            )
    assert any(r.spoiled > 0 for r in e.records("P1") + e.records("P2"))
    reps = [r.reputation_end for r in e.records("P1")]
    assert len(set(reps)) > 1


def test_demand_is_split_by_reputation_at_the_start_of_the_day():
    config = quiet(
        3, demand__per_player_peak=500, supplier__capacity_mean_per_player=100
    )
    e = start(config, seed=7)
    play(e, {"P1": D(1, 200)})
    play(e)
    assert e.day == 3
    reps = {a: e.player_view(a)["reputation"] for a in e.aliases}
    assert max(reps.values()) - min(reps.values()) > 0.01
    mult = weekday_multiplier(Weekday.WED, Weekday.THU, Weekday.SAT)
    lam = market_lambda(500, 3, mult)
    split = shares(reps)
    expected = draw_demands(7, 3, {a: lam * split[a] for a in reps})
    out = play(e)
    for alias in e.aliases:
        assert out.records[alias].demand == expected[alias]
        assert out.records[alias].reputation_open == pytest.approx(reps[alias])
    assert sum(split.values()) == pytest.approx(1.0, abs=1e-9)


# ------------------------------------------------------------ timing (AC 5)


@pytest.mark.parametrize("lead", [0, 1, 3])
def test_spot_order_arrives_lead_time_days_later(lead):
    config = quiet(
        2,
        supplier__lead_time_days=lead,
        supplier__quality_min=100,
        demand__per_player_peak=1,
        stock__initial_stock=0,
    )
    e = start(config)
    arrival = 1 + lead
    delivered = []
    for _ in range(arrival + 2):
        decisions = {"P1": D(1, 30)} if e.day == 1 else {}
        delivered.append(play(e, decisions).records["P1"].delivered)
        if e.day == arrival + 1:
            lot = view_lots(e, "P1")[f"L{arrival}"]
            assert lot["age"] == 1
            assert lot["unit_cost"] == 12.0
            assert lot["initial_quality"] == 100
    assert delivered == [30 if d == arrival else 0 for d in range(1, arrival + 3)]


def test_futures_timeline_paid_ships_and_arrives_on_schedule():
    config = quiet(
        2,
        futures__horizon_days=3,
        supplier__spot_spread=0.25,
        supplier__quality_min=100,
        demand__per_player_peak=1,
        stock__initial_stock=0,
    )
    e = start(config, seed=11)
    out1 = play(e, {"P1": D(1, 0, 10)})
    r1 = out1.records["P1"]
    assert r1.cost_futures == 120.0 and r1.shipped == 0
    assert r1.outlay == 120.0
    assert e.player_view("P1")["futures_book"] == [
        {"contract_day": 1, "ships_day": 3, "delivery_day": 4, "qty": 10}
    ]
    r2 = play(e).records["P1"]
    assert (r2.futures_due, r2.shipped, r2.delivered) == (0, 0, 0)
    out3 = play(e, {"P1": D(3, 10, 0, CIP)})
    r3 = out3.records["P1"]
    spot3 = market(e, 3).spot_price
    assert (r3.futures_due, r3.futures_filled, r3.futures_unfilled) == (10, 10, 0)
    assert r3.allocated_spot == 10 and r3.shipped == 20 and r3.delivered == 0
    assert r3.cost_futures == 0.0 and r3.refund_futures == 0.0
    assert r3.cost_spot == round_cents(spot3 * 10)
    assert r3.cost_transport == 20.0
    assert r3.cost_insurance == round_cents(0.55 * 20)
    assert r3.futures_vs_spot == round_cents((spot3 - 12.0) * 10)
    assert e.player_view("P1")["futures_book"] == []
    assert e.player_view("P1")["in_transit"] == [
        {
            "shipped_day": 3,
            "arrives_day": 4,
            "qty": 20,
            "lost": 0,
            "incoterm_tier": "cip",
        }
    ]
    r4 = play(e).records["P1"]
    assert r4.delivered == 20
    lot = view_lots(e, "P1")["L4"]
    # the shipment's unit cost mixes the spot price and the base price
    assert lot["unit_cost"] == pytest.approx((spot3 * 10 + 12.0 * 10) / 20, abs=0.006)


def test_a_futures_contract_is_made_only_for_a_positive_order():
    e = start(quiet(2, futures__horizon_days=3))
    play(e)
    assert e.player_view("P1")["futures_book"] == []
    play(e, {"P2": D(2, 0, 4)})
    assert e.player_view("P1")["futures_book"] == []
    assert e.player_view("P2")["futures_book"] == [
        {"contract_day": 2, "ships_day": 4, "delivery_day": 5, "qty": 4}
    ]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_short_futures_day_uses_the_seeded_hamilton_fill_and_refunds_at_base(seed):
    config = quiet(
        2,
        futures__horizon_days=3,
        supplier__capacity_mean_per_player=2.5,
        supplier__spot_spread=0.25,
        demand__per_player_peak=1,
    )
    e = start(config, seed=seed)
    play(e, {"P1": D(1, 0, 6), "P2": D(1, 0, 6)})
    play(e)
    out = play(e, {"P1": D(3, 5)})
    cap = market(e, 3).capacity
    assert cap == 5
    # 6 + 6 due against 5: both floor to 2 and the extra unit is a seeded tie
    expected = fill_futures({"P1": 6, "P2": 6}, cap, stream(seed, 3, "alloc_futures"))
    assert sum(expected.values()) == cap
    spot3 = market(e, 3).spot_price
    for alias, due in (("P1", 6), ("P2", 6)):
        r = out.records[alias]
        assert r.futures_due == due
        assert r.futures_filled == expected[alias]
        assert r.futures_unfilled == due - expected[alias]
        assert r.refund_futures == round_cents(12.0 * (due - expected[alias]))
        assert r.futures_vs_spot == round_cents((spot3 - 12.0) * expected[alias])
        assert r.shipped == expected[alias]
        assert e.player_view(alias)["futures_book"] == []
    # futures take the whole capacity first: the spot order gets nothing, and
    # the unfilled futures are not a spot shortfall
    p1 = out.records["P1"]
    assert p1.allocated_spot == 0 and p1.shortfall_spot == 5
    assert out.records["P2"].shortfall_spot == 0


def test_spot_gets_the_capacity_futures_leave():
    config = quiet(
        2,
        futures__horizon_days=3,
        supplier__capacity_mean_per_player=10,
        demand__per_player_peak=1,
    )
    e = start(config)
    play(e, {"P1": D(1, 0, 12)})
    play(e)
    out = play(e, {"P1": D(3, 30), "P2": D(3, 30)})
    # capacity 20, 12 taken by P1's futures, 8 left for spot; equal orders are
    # tied, so the seeded draw on alloc_spot decides
    assert out.records["P1"].futures_filled == 12
    expected = allocate({"P1": 30, "P2": 30}, 8, False, stream(11, 3, "alloc_spot"))
    assert sum(expected.values()) == 8
    assert out.records["P1"].allocated_spot == expected["P1"]
    assert out.records["P2"].allocated_spot == expected["P2"]
    assert out.records["P1"].shipped == 12 + expected["P1"]
    assert out.records["P2"].shipped == expected["P2"]


# ---------------------------------------------------------- spot allocation


def scarce_engine(**over):
    config = quiet(4, supplier__capacity_mean_per_player=25, **over)
    return start(config, seed=3)


def test_cooperative_allocation_matches_the_spec_example():
    e = scarce_engine()
    orders = {"P1": 60, "P2": 50, "P3": 30, "P4": 0}
    out = play(e, {a: D(1, q, coop=True) for a, q in orders.items()})
    assert market(e, 1).capacity == 100
    got = {a: out.records[a].allocated_spot for a in orders}
    assert got == {"P1": 43, "P2": 36, "P3": 21, "P4": 0}
    assert out.public.cooperation is True and out.public.cooperators == 4
    assert market(e, 1).cooperation is True and market(e, 1).cooperators == 4
    for a, rec in out.records.items():
        assert rec.cooperate is True
        assert rec.shortfall_spot == orders[a] - got[a]
        assert rec.cost_spot == 12.0 * got[a]
        assert rec.shipped == got[a] and rec.cost_transport == float(got[a])


def test_one_dissenter_even_with_a_zero_order_means_largest_first():
    e = scarce_engine()
    orders = {"P1": 60, "P2": 50, "P3": 30, "P4": 0}
    decisions = {a: D(1, q, coop=(a != "P4")) for a, q in orders.items()}
    out = play(e, decisions)
    got = {a: out.records[a].allocated_spot for a in orders}
    assert got == {"P1": 60, "P2": 40, "P3": 0, "P4": 0}
    assert out.public.cooperation is False and out.public.cooperators == 3


@pytest.mark.parametrize("coop", [True, False])
def test_spot_allocation_follows_the_allocate_reference_and_its_stream(coop):
    config = quiet(3, supplier__capacity_mean_per_player=10)
    e = start(config, seed=21)
    orders = {"P1": 15, "P2": 15, "P3": 10}  # ties force the seeded tie-break
    out = play(e, {a: D(1, q, coop=coop) for a, q in orders.items()})
    expected = allocate(orders, 30, coop, stream(21, 1, "alloc_spot"))
    assert sum(expected.values()) == 30
    got = {a: out.records[a].allocated_spot for a in orders}
    assert got == dict(expected)


def test_a_short_day_costs_only_allocated_units():
    e = start(quiet(2, supplier__capacity_mean_per_player=5))
    out = play(e, {"P1": D(1, 40), "P2": D(1, 0)})
    r = out.records["P1"]
    assert (r.allocated_spot, r.shortfall_spot, r.shipped) == (10, 30, 10)
    assert r.cost_spot == 120.0 and r.cost_transport == 10.0
    assert r.outlay == round_cents(12 * 40 + 1 * 40)
    assert r.cash_end == round_cents(5000 + r.profit)


# ------------------------------------------------- transport and insurance


def test_transport_and_fee_are_charged_on_shipped_units_lost_ones_included():
    # spec 14 #2
    config = quiet(2, transport__failure_probability=1.0)
    e = start(config)
    out = play(e, {"P1": D(1, 30, 0, CIP), "P2": D(1, 0)})
    r = out.records["P1"]
    assert r.refrigeration_failure is True
    assert r.shipped == 30 and r.lost_transport == 9
    assert r.cost_transport == 30.0  # not 21
    assert r.cost_insurance == 16.5  # 0.55 x 30, not 0.55 x 21
    assert r.cost_spot == 360.0
    assert e.player_view("P1")["in_transit"][0]["qty"] == 21
    assert e.player_view("P1")["in_transit"][0]["lost"] == 9
    r2 = play(e).records["P1"]
    assert r2.delivered == 21


def test_the_failure_is_drawn_every_day_even_when_nothing_ships():
    seed = 11
    config = quiet(2, transport__failure_probability=0.5)
    e = start(config, seed=seed)
    flags = []
    for _ in range(14):
        out = play(e)  # nobody ships anything
        flags.append(out.records["P1"].refrigeration_failure)
    oracle = [draw_failure(stream(seed, d, "transport"), 0.5) for d in range(1, 15)]
    assert any(oracle) and not all(oracle)
    assert flags == oracle
    assert [m.refrigeration_failure for m in e.market_history()] == oracle
    assert [p.refrigeration_failure for p in e.public_history()] == oracle


@pytest.mark.parametrize("shared", [False, True])
def test_shared_loss_counts_only_players_who_shipped(shared):
    # spec 14 #19: N is 2 here (P3 ordered 0), so the shared loss is L / 2
    config = quiet(
        3,
        transport__failure_probability=1.0,
        transport__shared=shared,
        supplier__capacity_mean_per_player=44,
    )
    e = start(config)
    out = play(e, {"P1": D(1, 60), "P2": D(1, 60), "P3": D(1, 0)})
    oracle = transport_losses({"P1": 60, "P2": 60, "P3": 0}, True, 0.3, shared)
    got = {a: out.records[a].lost_transport for a in e.aliases}
    assert got == dict(oracle)
    assert got["P1"] == (9 if shared else 18)  # shared with N=3 would give 6
    assert got["P3"] == 0


@pytest.mark.parametrize(
    "tier,fee", [(EXW, 0.0), (CIP, 0.55), (DAP, 0.55), (DDP, 1.39)]
)
def test_insurance_refund_is_credited_on_the_arrival_day_at_the_tier_value(tier, fee):
    config = quiet(
        2,
        transport__failure_probability=1.0,
        supplier__spot_spread=0.25,
        supplier__capacity_mean_per_player=30,
        demand__per_player_peak=1,
    )
    e = start(config, seed=11)
    out1 = play(e, {"P1": D(1, 50, 0, tier)})
    r1 = out1.records["P1"]
    spot1, spot2 = market(e, 1).spot_price, None
    assert r1.shipped == 50 and r1.lost_transport == 15
    assert r1.cost_insurance == round_cents(fee * 50)  # on shipped, lost included
    assert r1.refund_insurance == 0.0  # not credited on the shipping day
    out2 = play(e)
    spot2 = market(e, 2).spot_price
    assert spot1 != spot2
    r2 = out2.records["P1"]
    assert r2.delivered == 35
    paid = r1.cost_spot / 50
    expected = 15 * refund_value(
        tier, paid_unit_cost=paid, arrival_spot_price=spot2, retail_price=30.0
    )
    assert r2.refund_insurance == pytest.approx(expected, abs=0.011)
    if tier == EXW:
        assert r2.refund_insurance == 0.0
    if tier == DAP:
        assert r2.refund_insurance == round_cents(15 * spot2)
    if tier == DDP:
        assert r2.refund_insurance == 450.0
    if tier == CIP:
        assert r2.refund_insurance == pytest.approx(15 * spot1, abs=0.011)
    assert r2.cash_end == round_cents(r2.cash_open + r2.profit)


def test_an_empty_shipment_is_pushed_every_day_so_arrivals_stay_on_time():
    # spec 11 failure mode 10; with L = 2 and the buggy "push only when something
    # ships" pipeline, the day-4 shipment would arrive on day 4 instead of day 6.
    config = quiet(
        2,
        supplier__lead_time_days=2,
        stock__initial_stock=0,
        supplier__quality_min=100,
        demand__per_player_peak=1,
    )
    e = start(config)
    delivered = []
    for day in range(1, 8):
        decisions = {}
        if day == 1:
            decisions = {"P1": D(day, 10)}
        if day == 4:
            decisions = {"P1": D(day, 25)}
        delivered.append(play(e, decisions).records["P1"].delivered)
    assert delivered == [0, 0, 10, 0, 0, 25, 0]
    in_transit = e.player_view("P1")["in_transit"]
    assert in_transit == []


# ---------------------------------------------- stock, spoilage, conservation


def test_units_delivered_today_are_sold_before_tonights_check():
    # spec 11 failure mode 12: F = 0, so a lot ages into risk on its first night
    config = quiet(
        2,
        stock__fresh_days=0,
        stock__max_days=3,
        supplier__lead_time_days=0,
        supplier__quality_min=100,
        demand__per_player_peak=500,
    )
    e = start(config, seed=11)
    demand = draw_demands(11, 1, {"P1": 125.0, "P2": 125.0})
    assert demand["P1"] >= 60 and demand["P2"] >= 30
    out = play(e, {"P1": D(1, 30)})
    r = out.records["P1"]
    assert r.delivered == 30 and r.opening_stock == 30
    assert r.sold == 60  # the whole stock, delivery included, is sold first
    assert r.closing_stock == 0 and r.spoiled == 0
    assert out.records["P2"].sold == 30 and out.records["P2"].spoiled == 0


def test_spoilage_uses_the_per_alias_spoil_stream_after_sales():
    config = quiet(
        2,
        stock__fresh_days=1,
        stock__max_days=4,
        stock__initial_stock=100,
        supplier__quality_min=100,
        demand__per_player_peak=1,
    )
    seed = 4
    e = start(config, seed=seed)
    out1 = play(e)
    assert out1.records["P1"].spoiled == 0  # age 1 <= F: no risk at 100%
    out2 = play(e)
    for alias in e.aliases:
        r = out2.records[alias]
        left = r.opening_stock - r.sold
        oracle = Cellar([Lot("L0", left, 1, 12.0, 100, 1, 4, False)])
        expected = oracle.nightly_spoil(stream(seed, 2, f"spoil:{alias}"))
        assert 0 < expected < left
        assert r.spoiled == expected
        assert r.revenue_salvage == round_cents(2.0 * expected)
        assert r.closing_stock == left - expected


def test_a_plain_lot_follows_the_spoilage_table_and_dies_on_its_fourth_night():
    config = quiet(
        2,
        stock__fresh_days=1,
        stock__max_days=4,
        stock__initial_stock=100,
        supplier__quality_min=100,
        demand__per_player_peak=1,
    )
    e = start(config, seed=9)
    risks = []
    qualities = []
    for day in range(1, 4):
        lot = view_lots(e, "P1")["L0"]
        risks.append(lot["risk_tonight"])
        qualities.append(lot["quality"])
        assert lot["age"] == day - 1
        play(e)
    assert risks == pytest.approx([0.0, 1 / 3, 2 / 3], abs=1e-3)
    assert qualities == [100, 67, 33]
    lot = view_lots(e, "P1")["L0"]
    assert lot["risk_tonight"] == 1.0 and lot["quality"] == 0
    out = play(e)
    r = out.records["P1"]
    assert r.spoiled == r.opening_stock - r.sold  # every remaining unit spoils
    assert r.closing_stock == 0
    assert r.revenue_salvage == round_cents(2.0 * r.spoiled)
    assert "L0" not in view_lots(e, "P1")


def test_conservation_is_charged_and_applied_before_tonights_check():
    config = quiet(
        2,
        stock__fresh_days=0,
        stock__max_days=2,
        supplier__quality_min=100,
        demand__per_player_peak=1,
    )
    seed = 6
    e = start(config, seed=seed)
    out = play(e, {"P1": D(1, conserve=("L0",))})
    r1, r2 = out.records["P1"], out.records["P2"]
    assert r1.lots_conserved == 1 and r1.conserve_lots == ("L0",)
    assert r1.cost_conservation == 30.0
    assert r1.spoiled == 0  # conserved: F = 2, so age 1 is still safe
    assert r2.lots_conserved == 0 and r2.cost_conservation == 0.0
    left = r2.opening_stock - r2.sold
    plain = Cellar([Lot("L0", left, 0, 12.0, 100, 0, 2, False)])
    assert r2.spoiled == plain.nightly_spoil(stream(seed, 1, "spoil:P2"))
    assert r2.spoiled > 0
    lot = view_lots(e, "P1")["L0"]
    assert lot["conserved"] is True
    assert lot["risk_tonight"] == 0.0  # hazard(2, 2, 4) at 100%
    assert r1.profit == round_cents(
        30 * r1.sold - 30 - 0.2 * r1.closing_stock - 1.5 * r1.backlog_end
    )


# -------------------------------------------------------- demand and backlog


def test_served_backlog_earns_the_retail_price():
    # spec 14 #5
    config = quiet(
        2,
        demand__per_player_peak=40,
        stock__initial_stock=0,
        supplier__quality_min=100,
        supplier__capacity_mean_per_player=60,
    )
    e = start(config, seed=11)
    out1 = play(e, {"P1": D(1, 100)})
    r1 = out1.records["P1"]
    assert r1.sold == 0 and r1.backlog_end == r1.demand > 0
    assert r1.cost_backlog == round_cents(1.5 * r1.backlog_end)
    out2 = play(e)
    r2 = out2.records["P1"]
    assert r2.delivered == 100
    assert r2.backlog_open == r1.backlog_end > 0
    assert r2.sold == r2.backlog_open + r2.demand  # backlog served first, all of it
    assert r2.revenue_sales == round_cents(30 * r2.sold)
    assert r2.revenue_sales > 30 * r2.demand
    assert r2.backlog_end == 0 and r2.cost_backlog == 0.0
    assert r2.cost_holding == round_cents(0.2 * r2.closing_stock)
    # a player without stock keeps carrying the backlog, never negative
    q = out2.records["P2"]
    assert q.sold == 0 and q.revenue_sales == 0.0
    assert q.backlog_end == q.backlog_open + q.demand
    assert q.cost_backlog == round_cents(1.5 * q.backlog_end)
    assert q.profit == round_cents(-1.5 * q.backlog_end - 0.2 * 0)


def test_money_lines_are_cents_and_cash_chains_to_the_cent():
    e = start(quiet(2, transport__unit_cost=0.333, stock__holding_cost=0.137), seed=2)
    cash = {"P1": 5000.0, "P2": 5000.0}
    for _ in range(4):
        out = play(e, {"P1": D(e.day, 7), "P2": D(e.day, 3)})
        for alias, r in out.records.items():
            lines = [
                r.revenue_sales,
                r.revenue_salvage,
                r.revenue_peer,
                r.refund_insurance,
                r.refund_futures,
                r.cost_spot,
                r.cost_futures,
                r.cost_peer,
                r.cost_transport,
                r.cost_insurance,
                r.cost_conservation,
                r.cost_holding,
                r.cost_backlog,
                r.profit,
                r.cash_open,
                r.cash_end,
                r.outlay,
            ]
            for line in lines:
                assert line == round_cents(line)
            total = (
                r.revenue_sales
                + r.revenue_salvage
                + r.revenue_peer
                + r.refund_insurance
                + r.refund_futures
                - r.cost_spot
                - r.cost_futures
                - r.cost_peer
                - r.cost_transport
                - r.cost_insurance
                - r.cost_conservation
                - r.cost_holding
                - r.cost_backlog
            )
            assert r.profit == pytest.approx(total, abs=0.07)
            assert r.cash_open == cash[alias]
            assert r.cash_end == round_cents(r.cash_open + r.profit)
            cash[alias] = r.cash_end
            assert e.player_view(alias)["cash"] == r.cash_end
    assert r.cost_transport != 0.0 and r.cost_transport == round_cents(0.333 * 3)


# ------------------------------------------------------------ quality draws


def test_every_arriving_lot_draws_its_quality_on_its_own_stream():
    config = quiet(
        2,
        supplier__quality_min=50,
        supplier__quality_max=100,
        demand__per_player_peak=1,
    )
    seed = 8
    e = start(config, seed=seed)
    assert view_lots(e, "P1")["L0"]["initial_quality"] == 100
    seen = 0
    for day in range(1, 7):
        play(e, {"P1": D(day, 30), "P2": D(day, 25)})
        if day < 2:
            continue
        for alias in e.aliases:
            lot = view_lots(e, alias).get(f"L{day}")
            if lot is None:
                continue
            seen += 1
            expected = stream(seed, day, f"quality:{alias}").randint(50, 100)
            assert 50 <= lot["initial_quality"] <= 100
            assert lot["initial_quality"] == expected
    assert seen >= 8


def test_full_quality_range_means_no_spoilage_in_the_fresh_period():
    config = quiet(
        2,
        stock__fresh_days=10,
        stock__max_days=14,
        supplier__quality_min=100,
        supplier__quality_max=100,
        demand__per_player_peak=5,
    )
    e = start(config, seed=3)
    for day in range(1, 9):
        out = play(e, {"P1": D(day, 20), "P2": D(day, 10)})
        assert all(r.spoiled == 0 for r in out.records.values())


# --------------------------------------------------------------- peer market


def peer_config(**over):
    return quiet(
        3,
        supplier__capacity_mean_per_player=10,
        supplier__quality_min=100,
        supplier__lead_time_days=0,
        futures__horizon_days=7,
        stock__fresh_days=1,  # the old shelf life keeps day-1 lots at 100%
        stock__max_days=4,
        **over,
    )


def run_peer_day(**over):
    e = start(peer_config(**over), seed=11)
    decisions = {
        "P1": D(1, 40, buy=PeerBuy(20.0, 50)),
        "P2": D(1, 0, sell=PeerSell(15.0, 100, 25)),
        "P3": D(1, 0, sell=PeerSell(14.0, 100, 4)),
    }
    return e, play(e, decisions)


def test_peer_day_worked_example():
    e, out = run_peer_day()
    r1, r2, r3 = (out.records[a] for a in ("P1", "P2", "P3"))
    assert r1.allocated_spot == 30 and r1.shortfall_spot == 10
    assert r1.peer_bought == 10
    assert r1.cost_peer == 4 * 14.0 + 6 * 15.0
    assert r3.peer_sold == 4 and r3.revenue_peer == 56.0
    assert r2.peer_sold == 6 and r2.revenue_peer == 90.0
    assert [t.to_payload() for t in r1.peer_trades] == [
        {
            "side": "buy",
            "counterparty": "P3",
            "lot_id": "B1-1",
            "qty": 4,
            "quality": 100,
            "price": 14.0,
        },
        {
            "side": "buy",
            "counterparty": "P2",
            "lot_id": "B1-2",
            "qty": 6,
            "quality": 100,
            "price": 15.0,
        },
    ]
    assert [t.to_payload() for t in r2.peer_trades] == [
        {
            "side": "sell",
            "counterparty": "P1",
            "lot_id": "L0",
            "qty": 6,
            "quality": 100,
            "price": 15.0,
        }
    ]
    assert [t.to_payload() for t in r3.peer_trades] == [
        {
            "side": "sell",
            "counterparty": "P1",
            "lot_id": "L0",
            "qty": 4,
            "quality": 100,
            "price": 14.0,
        }
    ]
    assert out.public.peer_units == 10 and out.public.peer_avg_price == 14.6
    assert market(e, 1).peer_units == 10 and market(e, 1).peer_avg_price == 14.6
    assert e.public_results(1)["peer_units"] == 10
    assert e.public_results(1)["peer_avg_price"] == 14.6
    for r in (r1, r2, r3):
        assert (
            r.opening_stock + r.delivered + r.peer_bought
            == r.sold + r.peer_sold + r.spoiled + r.closing_stock
        )
    assert r1.profit == round_cents(
        30 * r1.sold
        + 2 * r1.spoiled
        - 360
        - 30
        - 146
        - 0.2 * r1.closing_stock
        - 1.5 * r1.backlog_end
    )
    assert r1.cash_end == round_cents(5000 + r1.profit)
    assert r3.cash_end == round_cents(5000 + r3.profit)
    for r in (r2, r3):
        assert r.shortfall_spot == 0 and r.peer_bought == 0 and r.cost_peer == 0.0
    # the bought lots sit in the buyer's cellar, ids named by purchase order
    cellar = view_lots(e, "P1")
    d = draw_demands(11, 1, {a: 6.25 for a in ("P1", "P2", "P3")})
    assert d["P1"] < 50
    assert cellar["B1-1"]["unit_cost"] == 14.0 and cellar["B1-2"]["unit_cost"] == 15.0
    assert cellar["B1-1"]["initial_quality"] == 100 and cellar["B1-1"]["age"] == 1


def test_units_sold_to_rivals_are_gone_before_the_sellers_own_customers():
    e, out = run_peer_day(demand__per_player_peak=500)
    demand = draw_demands(11, 1, {a: 125.0 for a in ("P1", "P2", "P3")})
    assert min(demand.values()) >= 70
    assert out.records["P2"].sold == 30 - 6
    assert out.records["P3"].sold == 30 - 4
    assert out.records["P1"].sold == 30 + 30 + 10
    assert out.records["P2"].closing_stock == 0


def test_peer_market_off_means_empty_peer_fields():
    e = start(peer_config(peer_market__enabled=False), seed=11)
    out = play(e, {"P1": D(1, 40)})
    for r in out.records.values():
        assert r.peer_bought == r.peer_sold == 0
        assert r.cost_peer == r.revenue_peer == 0.0
        assert r.peer_trades == () and r.peer_buy is None and r.peer_sell is None
    assert out.records["P1"].shortfall_spot == 10
    assert out.public.peer_units == 0 and out.public.peer_avg_price is None
    assert e.player_view("P1")["quality_guide"] == []


def _patch_streams(monkeypatch, forbidden=(), calls=None):
    original = stream

    def wrapped(seed, day, purpose):
        if calls is not None:
            calls.append((day, purpose))
        if purpose in forbidden:
            raise AssertionError(f"stream {purpose!r} must not be read")
        return original(seed, day, purpose)

    patched = 0
    for name, module in list(sys.modules.items()):
        if name.startswith("app.core") and getattr(module, "stream", None) is original:
            monkeypatch.setattr(module, "stream", wrapped)
            patched += 1
    assert patched, "no module of app.core binds the stream function"


def _record_game(e, days):
    out = []
    for _ in range(days):
        out.append(play(e, {"P1": D(e.day, 12), "P2": D(e.day, 3)}))
    return out


def test_peer_off_never_reads_the_peer_market_stream(monkeypatch):
    config = quiet(2, peer_market__enabled=False, supplier__capacity_mean_per_player=3)
    plain = _record_game(start(config, seed=5), 5)
    calls = []
    _patch_streams(monkeypatch, forbidden=("peer_market",), calls=calls)
    guarded = _record_game(start(config, seed=5), 5)
    assert calls  # the patch reaches the engine
    assert [o.records for o in guarded] == [o.records for o in plain]
    assert [o.public for o in guarded] == [o.public for o in plain]


def test_peer_on_reads_the_peer_market_stream_every_day(monkeypatch):
    calls = []
    _patch_streams(monkeypatch, calls=calls)
    e = start(quiet(2), seed=5)
    for _ in range(4):
        play(e)  # nobody trades, the draws are made anyway
    days = [d for d, purpose in calls if purpose == "peer_market"]
    assert sorted(days) == [1, 2, 3, 4]
    transport_days = [d for d, purpose in calls if purpose == "transport"]
    assert sorted(transport_days) == [1, 2, 3, 4]


def test_peer_on_trips_the_forbidden_stream_guard(monkeypatch):
    # guards the guard: with the market on, the same patch must raise
    _patch_streams(monkeypatch, forbidden=("peer_market",))
    e = start(quiet(2), seed=5)
    for alias in e.aliases:
        e.submit(alias, Decision.zero(1))
    with pytest.raises(AssertionError):
        e.resolve_day()


def peer_cash_engine(starting_cash):
    config = quiet(
        3,
        starting_cash=starting_cash,
        futures__horizon_days=3,
        stock__initial_stock=100,
        stock__holding_cost=0,
        stock__salvage_value=0,
        demand__backlog_penalty=0,
        demand__retail_price=0.01,
        demand__per_player_peak=1,
        supplier__quality_min=100,
    )
    e = start(config, seed=4)
    play(e, {"P1": D(1, 0, 60)})
    play(e)
    assert e.day == 3
    cash = e.player_view("P1")["cash"]
    s1 = int(cash // 13)
    out = play(
        e,
        {
            "P1": D(3, s1, buy=PeerBuy(20.0, 0), sell=PeerSell(13.0, 100, 100)),
            "P2": D(3, 0, sell=PeerSell(14.0, 100, 100)),
            "P3": D(3, 7, buy=PeerBuy(30.0, 0)),
        },
    )
    return e, out, cash


def cash_when_market_opens(r):
    return (
        r.cash_open
        + r.refund_futures
        + r.refund_insurance
        - r.cost_conservation
        - r.cost_futures
        - r.cost_spot
        - r.cost_transport
        - r.cost_insurance
    )


def test_buyer_without_cash_when_the_market_opens_buys_nothing_even_when_selling():
    # spec 11 failure modes 13 and 16: P1's futures transport (66) takes it below
    # zero, while its sales to P3 (7 x $13) must not fund its own purchases.
    e, out, cash = peer_cash_engine(830)
    r1, r3 = out.records["P1"], out.records["P3"]
    assert r1.futures_filled == 60 and r1.allocated_spot == 6
    assert r1.shortfall_spot > 0 and cash > 0
    assert cash_when_market_opens(r1) < 0
    assert r1.peer_bought == 0 and r1.cost_peer == 0.0
    assert r3.peer_bought == 7 and r3.shortfall_spot == 7
    assert r1.peer_sold == 7 and r1.revenue_peer == 91.0
    assert [t.side for t in r1.peer_trades] == [TradeSide.SELL]


def test_buyer_is_capped_by_cash_when_the_market_opens_not_by_cash_open():
    e, out, cash = peer_cash_engine(920)
    r1 = out.records["P1"]
    market_cash = cash_when_market_opens(r1)
    assert 0 < market_cash < r1.shortfall_spot * 14.0 < r1.cash_open
    expected = int(round_cents(market_cash) * 100) // 1400
    assert 0 < expected < r1.shortfall_spot
    assert r1.peer_bought == expected
    assert r1.cost_peer == 14.0 * expected <= market_cash
    buys = [t for t in r1.peer_trades if t.side == TradeSide.BUY]
    assert buys and all(t.counterparty == "P2" for t in buys)


def test_unfilled_futures_alone_create_no_shortfall_and_buy_nothing():
    # spec 14: unfilled futures are refunded, they never open a peer purchase
    config = quiet(
        2,
        futures__horizon_days=3,
        supplier__capacity_mean_per_player=20,
        demand__per_player_peak=1,
    )
    e = start(config, seed=2)
    play(e, {"P1": D(1, 0, 60)})
    play(e)
    out = play(
        e,
        {
            "P1": D(3, 0, buy=PeerBuy(30.0, 0)),
            "P2": D(3, 0, sell=PeerSell(5.0, 100, 100)),
        },
    )
    r1 = out.records["P1"]
    assert (r1.futures_due, r1.futures_filled, r1.futures_unfilled) == (60, 40, 20)
    assert r1.refund_futures == 240.0
    assert r1.shortfall_spot == 0
    assert r1.peer_bought == 0 and r1.cost_peer == 0.0
    assert out.records["P2"].peer_sold == 0


def test_bought_lot_keeps_the_sellers_age_quality_shelf_life_and_conserved_flag():
    # spec 14 #24: trading must not launder old stock into fresh stock
    config = quiet(
        2,
        supplier__quality_min=90,
        supplier__quality_max=90,
        supplier__capacity_mean_per_player=1,
        demand__per_player_peak=1,
    )
    e = start(config, seed=3)
    play(e, {"P2": D(1, conserve=("L0",))})
    seller_before = view_lots(e, "P2")["L0"]
    assert seller_before["conserved"] is True and seller_before["age"] == 1
    out = play(
        e,
        {
            "P1": D(2, 10, buy=PeerBuy(20.0, 0)),
            "P2": D(2, sell=PeerSell(14.0, 100, 100)),
        },
    )
    trade = out.records["P1"].peer_trades[0]
    assert (trade.lot_id, trade.counterparty, trade.qty) == ("B2-1", "P2", 8)
    assert trade.quality == seller_before["quality"]
    bought = view_lots(e, "P1")["B2-1"]
    kept = view_lots(e, "P2")["L0"]
    for key in (
        "age",
        "initial_quality",
        "conserved",
        "quality",
        "last_day",
        "risk_tonight",
    ):
        assert bought[key] == kept[key], key
    assert bought["conserved"] is True and bought["age"] == 2
    assert bought["unit_cost"] == 14.0
    assert view_lots(e, "P1")["L0"]["conserved"] is False


def test_a_player_who_sells_and_buys_never_resells_what_it_bought_today():
    # spec 11 failure mode 15
    config = quiet(
        2,
        stock__fresh_days=1,
        stock__max_days=4,
        supplier__quality_min=100,
        supplier__lead_time_days=0,
        supplier__capacity_mean_per_player=3,
        demand__per_player_peak=1,
    )
    e = start(config, seed=12)
    play(e)
    out = play(
        e,
        {
            "P1": D(2, 8, coop=True, buy=PeerBuy(20.0, 0), sell=PeerSell(13.0, 100, 9)),
            "P2": D(2, 7, coop=True, buy=PeerBuy(15.0, 0), sell=PeerSell(14.0, 100, 9)),
        },
    )
    r1, r2 = out.records["P1"], out.records["P2"]
    assert (r1.allocated_spot, r2.allocated_spot) == (3, 3)
    assert (r1.shortfall_spot, r2.shortfall_spot) == (5, 4)

    def trade(side, who, lot, qty, quality, price):
        return {
            "side": side,
            "counterparty": who,
            "lot_id": lot,
            "qty": qty,
            "quality": quality,
            "price": price,
        }

    assert [t.to_payload() for t in r1.peer_trades if t.side == TradeSide.BUY] == [
        trade("buy", "P2", "B2-1", 3, 100, 14.0),
        trade("buy", "P2", "B2-2", 2, 67, 14.0),
    ]
    assert [t.to_payload() for t in r2.peer_trades if t.side == TradeSide.BUY] == [
        trade("buy", "P1", "B2-1", 3, 100, 13.0),
        trade("buy", "P1", "B2-2", 1, 67, 13.0),
    ]
    for r in (r1, r2):
        for t in r.peer_trades:
            if t.side == TradeSide.SELL:
                assert not t.lot_id.startswith("B"), t
    assert [t.to_payload() for t in r1.peer_trades if t.side == TradeSide.SELL] == [
        trade("sell", "P2", "L2", 3, 100, 13.0),
        trade("sell", "P2", "L0", 1, 67, 13.0),
    ]
    assert [t.to_payload() for t in r2.peer_trades if t.side == TradeSide.SELL] == [
        trade("sell", "P1", "L2", 3, 100, 14.0),
        trade("sell", "P1", "L0", 2, 67, 14.0),
    ]


def test_spec_peer_market_example_end_to_end():
    # spec 5.7 example on day 5, built by putting the example's lots in the cellars
    config = quiet(
        4,
        supplier__capacity_mean_per_player=10,
        demand__per_player_peak=1,
        stock__initial_stock=0,
    )
    e = start(config, seed=11)
    for _ in range(4):
        play(e)
    assert e.day == 5
    payload = copy.deepcopy(e.to_payload())
    lots = {
        "P1": [],
        "P2": [],
        "P3": [
            Lot("L3", 12, 2, 12.0, 90, 0, 3, False),
            Lot("L4", 10, 1, 12.0, 95, 0, 3, False),
            Lot("L5", 20, 0, 12.0, 85, 0, 3, False),
        ],
        "P4": [
            Lot("L4", 8, 1, 12.0, 88, 0, 3, False),
            Lot("L5", 20, 0, 12.0, 82, 0, 3, False),
        ],
    }
    for alias, cellar in lots.items():
        payload["players"][alias]["cellar"] = [lot.to_payload() for lot in cellar]
    e = PulkyEngine.from_payload(payload, config)
    assert [x["quality"] for x in e.player_view("P3")["cellar"]] == [0, 28, 52]
    assert [x["last_day"] for x in e.player_view("P3")["cellar"]] == [5, 6, 7]
    assert market(e, 4).capacity == 40
    out = play(
        e,
        {
            "P1": D(5, 56, buy=PeerBuy(16.0, 20)),
            "P2": D(5, 12, buy=PeerBuy(14.0, 20)),
            "P3": D(5, sell=PeerSell(15.0, 40, 20)),
            "P4": D(5, sell=PeerSell(13.0, 40, 30)),
        },
    )
    r1, r2, r3, r4 = (out.records[a] for a in ("P1", "P2", "P3", "P4"))
    assert (r1.shortfall_spot, r2.shortfall_spot) == (16, 12)
    assert [
        (t.counterparty, t.lot_id, t.qty, t.quality, t.price) for t in r1.peer_trades
    ] == [("P3", "B5-1", 10, 28, 15.0), ("P4", "B5-2", 6, 21, 13.0)]
    assert r1.cost_peer == 228.0 and r1.peer_bought == 16
    assert [
        (t.counterparty, t.lot_id, t.qty, t.quality, t.price) for t in r2.peer_trades
    ] == [("P4", "B5-1", 2, 21, 13.0)]
    assert r2.cost_peer == 26.0 and r2.peer_bought == 2
    assert (r3.peer_sold, r3.revenue_peer) == (10, 150.0)
    assert (r4.peer_sold, r4.revenue_peer) == (8, 104.0)
    assert out.public.peer_units == 18
    assert out.public.peer_avg_price == round_cents((228 + 26) / 18)
    bought = view_lots(e, "P1")
    assert bought["B5-1"]["initial_quality"] == 95 and bought["B5-1"]["age"] == 2
    assert bought["B5-1"]["unit_cost"] == 15.0
    assert bought["B5-2"]["initial_quality"] == 88
    assert bought["B5-2"]["unit_cost"] == 13.0
    assert "L5" in view_lots(e, "P3") and "L5" in view_lots(e, "P4")


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_equal_buyers_are_ordered_by_the_peer_market_draw_not_by_alias(seed):
    # spec 14 #22: two buyers with the same cap and shortfall compete for 10 units
    config = quiet(
        3,
        supplier__capacity_mean_per_player=2,
        supplier__quality_min=100,
        demand__per_player_peak=1,
    )
    e = start(config, seed=seed)
    rng = stream(seed, 1, "peer_market")
    draw = {a: rng.random() for a in sorted(e.aliases)}
    first = min(("P1", "P2"), key=draw.get)
    out = play(
        e,
        {
            "P1": D(1, 20, coop=True, buy=PeerBuy(20.0, 0)),
            "P2": D(1, 20, coop=True, buy=PeerBuy(20.0, 0)),
            "P3": D(1, 0, coop=True, sell=PeerSell(14.0, 100, 10)),
        },
    )
    r1, r2 = out.records["P1"], out.records["P2"]
    assert r1.shortfall_spot == r2.shortfall_spot  # a genuine tie
    winner, loser = (r1, r2) if first == "P1" else (r2, r1)
    assert winner.peer_bought == 10 and loser.peer_bought == 0
    assert out.records["P3"].peer_sold == 10


@pytest.mark.parametrize(
    "shared,expected",
    [(False, (18, 9, 3)), (True, (6, 3, 1))],
)
def test_refrigeration_losses_reproduce_the_spec_example(shared, expected):
    config = quiet(
        3,
        transport__failure_probability=1.0,
        transport__shared=shared,
        supplier__capacity_mean_per_player=44,
    )
    e = start(config)
    out = play(e, {"P1": D(1, 60), "P2": D(1, 30), "P3": D(1, 10)})
    got = tuple(out.records[a].lost_transport for a in ("P1", "P2", "P3"))
    assert got == expected
    assert [out.records[a].shipped for a in ("P1", "P2", "P3")] == [60, 30, 10]
