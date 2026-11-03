"""Black-box tests for the per-player ``flow`` of ``app.core.stats`` (section 34)."""

from __future__ import annotations

import copy
import random
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.calendar import weekday_of
from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.engine import PulkyEngine
from app.core.enums import IncotermTier
from app.core.money import round_cents
from app.core.records import DayRecord, Decision, FinalSettlement, PeerBuy, PeerSell
from app.core.stats import build_stats

FLOW_KEYS = [
    "initial_stock",
    "initial_pipeline",
    "shipped_spot",
    "shipped_futures",
    "lost_transport",
    "in_transit_end",
    "delivered",
    "peer_bought",
    "sold",
    "peer_sold",
    "spoiled",
    "closing_stock",
]
NAMES = [
    "Don Agave",
    "La Chula",
    "El Tinacal",
    "Casa Maguey",
    "La Penca",
    "El Jarro",
]
EXW = IncotermTier.EXW
TIERS = (IncotermTier.CIP, IncotermTier.DAP, IncotermTier.DDP)


# ---------------------------------------------------------------- builders


def rec(day: int = 1, **kw: Any) -> DayRecord:
    base: dict[str, Any] = dict(
        day=day,
        weekday=weekday_of(day),
        order_spot=0,
        order_futures=0,
        incoterm_tier=IncotermTier.EXW,
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


def hand(
    day: int,
    opening: int,
    spot: int,
    fut: int,
    lost: int,
    delivered: int,
    peer_bought: int,
    sold: int,
    peer_sold: int,
    spoiled: int,
    closing: int,
) -> DayRecord:
    return rec(
        day,
        opening_stock=opening,
        allocated_spot=spot,
        futures_filled=fut,
        shipped=spot + fut,
        lost_transport=lost,
        delivered=delivered,
        peer_bought=peer_bought,
        sold=sold,
        peer_sold=peer_sold,
        spoiled=spoiled,
        closing_stock=closing,
    )


def example_records() -> list[DayRecord]:
    """Spec section 34 section 4: one player, lead time 1."""
    return [
        hand(1, 30, 20, 0, 5, 0, 0, 25, 0, 0, 5),
        hand(2, 5, 10, 4, 0, 15, 2, 18, 1, 1, 2),
        hand(3, 2, 0, 0, 0, 14, 0, 12, 0, 1, 3),
    ]


def flow_of(records: list[DayRecord], initial_pipeline: int = 0) -> dict:
    out = build_stats(
        initial_pipeline=initial_pipeline,
        records={"P1": records},
        market=[],
        settlements={"P1": settle()},
        display_names={"P1": "Pulqueria P1"},
        starting_reputation=0.5,
        seed=1,
    )
    return out["players"]["P1"]["flow"]


def check_identities(f: dict) -> None:
    assert f["initial_pipeline"] + f["shipped_spot"] + f["shipped_futures"] == (
        f["lost_transport"] + f["delivered"] + f["in_transit_end"]
    )
    assert f["initial_stock"] + f["delivered"] + f["peer_bought"] == (
        f["sold"] + f["peer_sold"] + f["spoiled"] + f["closing_stock"]
    )
    assert f["in_transit_end"] >= 0


NATURAL = {
    "initial_stock": 30,
    "initial_pipeline": 0,
    "shipped_spot": 30,
    "shipped_futures": 4,
    "lost_transport": 5,
    "in_transit_end": 0,
    "delivered": 29,
    "peer_bought": 2,
    "sold": 55,
    "peer_sold": 1,
    "spoiled": 2,
    "closing_stock": 3,
}
EARLY = {
    "initial_stock": 30,
    "initial_pipeline": 0,
    "shipped_spot": 30,
    "shipped_futures": 4,
    "lost_transport": 5,
    "in_transit_end": 14,
    "delivered": 15,
    "peer_bought": 2,
    "sold": 43,
    "peer_sold": 1,
    "spoiled": 1,
    "closing_stock": 2,
}


# ------------------------------------------------------- worked examples


def test_natural_end_example_gives_the_spec_values():
    f = flow_of(example_records())
    assert f == NATURAL
    check_identities(f)


def test_closing_stock_is_the_last_days_not_the_first_or_the_peak():
    # failure mode 1: the first record's closing is 5 (also the peak), the last's 3
    f = flow_of(example_records())
    assert f["closing_stock"] == 3
    assert f["closing_stock"] != 5


def test_early_end_example_gives_the_spec_values():
    f = flow_of(example_records()[:2])
    assert f == EARLY
    check_identities(f)


def test_lost_units_are_not_delivered_and_in_transit_is_not_clamped():
    # failure modes 2 and 5
    f = flow_of(example_records()[:2])
    assert f["delivered"] == 15
    assert f["in_transit_end"] == 14
    assert f["lost_transport"] == 5


def test_initial_stock_is_the_first_days_opening_stock():
    # failure mode 4: opening 30 while no closing stock or later opening is 30
    records = [
        hand(1, 30, 0, 0, 0, 0, 0, 10, 0, 0, 7),
        hand(2, 7, 0, 0, 0, 0, 0, 7, 0, 0, 0),
    ]
    # day 1 does not balance on purpose (30 != 10 + 7); only flow is read
    f = flow_of(records)
    assert f["initial_stock"] == 30


def test_no_records_gives_zeros_except_the_initial_pipeline():
    f = flow_of([])
    assert list(f) == FLOW_KEYS
    assert all(v == 0 and type(v) is int for v in f.values())
    f = flow_of([], initial_pipeline=10)
    assert list(f) == FLOW_KEYS
    assert all(type(v) is int for v in f.values())
    assert f["initial_pipeline"] == 10 and f["in_transit_end"] == 10
    assert all(
        v == 0 for k, v in f.items() if k not in ("initial_pipeline", "in_transit_end")
    )


def test_initial_pipeline_example_gives_the_spec_values():
    # failure mode 6: shipments only would give 8 + 0 - 0 - 10 = -2
    records = [
        hand(1, 20, 8, 0, 0, 5, 0, 22, 0, 0, 3),
        hand(2, 3, 0, 0, 0, 5, 0, 6, 0, 0, 2),
    ]
    f = flow_of(records, initial_pipeline=10)
    assert f == {
        "initial_stock": 20,
        "initial_pipeline": 10,
        "shipped_spot": 8,
        "shipped_futures": 0,
        "lost_transport": 0,
        "in_transit_end": 8,
        "delivered": 10,
        "peer_bought": 0,
        "sold": 28,
        "peer_sold": 0,
        "spoiled": 0,
        "closing_stock": 2,
    }
    check_identities(f)
    assert f["closing_stock"] + f["in_transit_end"] == 10


def test_build_stats_without_initial_pipeline_behaves_as_zero():
    kw: dict[str, Any] = dict(
        records={"P1": example_records()},
        market=[],
        settlements={"P1": settle()},
        display_names={"P1": "x"},
        starting_reputation=0.5,
        seed=1,
    )
    assert (
        build_stats(**kw)["players"]["P1"]["flow"]
        == build_stats(initial_pipeline=0, **kw)["players"]["P1"]["flow"]
        == NATURAL
    )


def test_flow_has_exactly_the_eleven_int_keys_in_order():
    for records in (example_records(), example_records()[:2], []):
        f = flow_of(records)
        assert list(f) == FLOW_KEYS
        assert all(type(v) is int for v in f.values())


def test_flow_totals_equal_the_existing_player_totals():
    out = build_stats(
        records={"P1": example_records()},
        market=[],
        settlements={"P1": settle()},
        display_names={"P1": "x"},
        starting_reputation=0.5,
        seed=1,
    )["players"]["P1"]
    f = out["flow"]
    assert f["sold"] == out["units_sold"] == 55
    assert f["spoiled"] == out["units_spoiled"] == 2
    assert f["lost_transport"] == out["units_lost_transport"] == 5
    assert f["peer_bought"] == out["peer_units_bought"] == 2
    assert f["peer_sold"] == out["peer_units_sold"] == 1


def test_flow_is_built_from_the_records_only_not_the_settlement():
    # failure mode 3: a flow read from the settlement would change here
    records = example_records()[:2]
    a = flow_of(records)
    odd = FinalSettlement(
        liquidated_units=999,
        revenue_liquidation=1.0,
        refund_insurance_in_transit=2.0,
        futures_cancelled_units=77,
        refund_futures_cancelled=3.0,
        settlement_total=6.0,
    )
    b = build_stats(
        records={"P1": records},
        market=[],
        settlements={"P1": odd},
        display_names={"P1": "x"},
        starting_reputation=0.5,
        seed=1,
    )["players"]["P1"]["flow"]
    assert a == b == EARLY


def test_hand_built_records_survive_the_payload_round_trip():
    records = example_records()[:2]
    again = [DayRecord.from_payload(r.to_payload()) for r in records]
    assert flow_of(again) == flow_of(records) == EARLY


# ------------------------------------------------------------ engine games


def cfg(**over: Any) -> GameConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    for path, value in over.items():
        keys = path.split("__")
        node = data
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return GameConfig.from_dict(data)


def choose(e: PulkyEngine, alias: str, rnd: random.Random) -> Decision:
    """A random decision the engine accepts (shrunk until affordable)."""
    v = e.player_view(alias)
    day, cash, w = e.day, v["cash"], v["order_windows"]
    tiers = [EXW] + [
        t for t in TIERS if getattr(e.config.insurance.tiers, t.value).enabled
    ]
    spot = rnd.choice([0, 3, 10, 25, 60]) if w["spot_open"] else 0
    fut = rnd.choice([0, 0, 5, 20]) if w["futures_open"] else 0
    tier = rnd.choice(tiers)
    coop = rnd.random() < 0.6
    buy = sell = None
    if e.config.peer_market.enabled:
        if rnd.random() < 0.6:
            buy = PeerBuy(rnd.randint(500, 2500) / 100, rnd.choice([0, 30, 60, 90]))
        if rnd.random() < 0.6:
            sell = PeerSell(
                rnd.randint(500, 2500) / 100,
                rnd.choice([40, 70, 100]),
                rnd.choice([1, 5, 30]),
            )
    decision = Decision(day, spot, fut, tier, (), coop, buy, sell)
    for _ in range(12):
        outlay = e.outlay(alias, decision)
        if outlay <= 0 or round_cents(outlay) <= cash:
            return decision
        decision = Decision(
            day,
            decision.order_spot // 2,
            decision.order_futures // 2,
            decision.incoterm_tier if decision.order_spot // 2 else EXW,
            (),
            coop,
            buy,
            sell,
        )
    return Decision(day, 0, 0, EXW, (), coop, buy, sell)


def play_game(
    seed: int,
    n: int,
    lead: int,
    days_played: int | None,
    peer: bool,
    failure: float = 0.3,
    pipeline_qty: int = 0,
) -> PulkyEngine:
    """Seven-day game; ``days_played`` None plays to the natural end."""
    rnd = random.Random(seed)
    config = cfg(
        max_players=n,
        duration_days=7,
        starting_cash=5000,
        demand__per_player_peak=rnd.choice([5, 20, 60]),
        supplier__lead_time_days=lead,
        supplier__initial_pipeline_qty=pipeline_qty,
        supplier__capacity_mean_per_player=rnd.choice([10, 22]),
        futures__horizon_days=rnd.randint(lead + 1, 6),
        stock__initial_stock=rnd.choice([0, 20, 40]),
        peer_market__enabled=peer,
        transport__shared=rnd.random() < 0.5,
        transport__failure_probability=failure,
    )
    seats = {f"P{i + 1}": NAMES[i] for i in range(n)}
    e = PulkyEngine.start(config, seats, seed)
    for _ in range(days_played if days_played is not None else 7):
        day = e.day
        decisions = {
            a: choose(e, a, random.Random(f"{seed}:{day}:{a}")) for a in e.aliases
        }
        for alias in e.aliases:
            e.submit(alias, decisions[alias])
        e.resolve_day()
    if days_played is not None:
        e.end_early()
    assert e.phase.value == "finished"
    return e


def pipeline_of(e: PulkyEngine) -> int:
    s = e.config.supplier
    return s.lead_time_days * s.initial_pipeline_qty


def stored_stats(e: PulkyEngine) -> dict:
    """What the stored path builds: payload-rebuilt records and the config."""
    return build_stats(
        records={
            a: [DayRecord.from_payload(r.to_payload()) for r in e.records(a)]
            for a in e.aliases
        },
        market=e.market_history(),
        settlements=e.settlements(),
        display_names=e.display_names,
        starting_reputation=e.config.reputation.starting,
        seed=e.seed,
        initial_pipeline=pipeline_of(e),
    )


def check_game(e: PulkyEngine, natural: bool) -> dict:
    stats = e.standings()
    for alias in e.aliases:
        p = stats["players"][alias]
        f = p["flow"]
        assert list(f) == FLOW_KEYS
        assert all(type(v) is int for v in f.values())
        assert f["initial_pipeline"] == pipeline_of(e)
        check_identities(f)
        recs = e.records(alias)
        if recs:
            assert f["closing_stock"] + f["in_transit_end"] == (
                e.settlements()[alias].liquidated_units
            )
            assert f["initial_stock"] == recs[0].opening_stock
            assert f["closing_stock"] == recs[-1].closing_stock
        else:
            assert f["in_transit_end"] == f["initial_pipeline"] == pipeline_of(e)
            assert all(
                v == 0
                for k, v in f.items()
                if k not in ("initial_pipeline", "in_transit_end")
            )
        if natural:
            assert f["in_transit_end"] == 0
        assert f["sold"] == p["units_sold"]
        assert f["spoiled"] == p["units_spoiled"]
        assert f["lost_transport"] == p["units_lost_transport"]
        assert f["peer_bought"] == p["peer_units_bought"]
        assert f["peer_sold"] == p["peer_units_sold"]
    return stats


@settings(max_examples=60, deadline=None, derandomize=True)
@given(
    seed=st.integers(min_value=0, max_value=10**6),
    n=st.integers(min_value=2, max_value=6),
    lead=st.integers(min_value=0, max_value=2),
    end=st.one_of(st.none(), st.integers(min_value=0, max_value=6)),
    peer=st.booleans(),
    failure=st.sampled_from([0.0, 0.3, 1.0]),
    qty=st.sampled_from([0, 5]),
)
def test_flow_identities_and_settlement_tie_hold_on_every_game(
    seed, n, lead, end, peer, failure, qty
):
    e = play_game(seed, n, lead, end, peer, failure, qty)
    check_game(e, natural=end is None)


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    seed=st.integers(min_value=0, max_value=10**6),
    n=st.integers(min_value=2, max_value=4),
    lead=st.integers(min_value=0, max_value=2),
    end=st.one_of(st.none(), st.integers(min_value=0, max_value=6)),
    qty=st.sampled_from([0, 5]),
)
def test_flow_is_identical_after_the_stored_round_trip(seed, n, lead, end, qty):
    e = play_game(seed, n, lead, end, True, pipeline_qty=qty)
    live = e.standings()
    stored = stored_stats(e)
    for alias in e.aliases:
        assert live["players"][alias]["flow"] == stored["players"][alias]["flow"]


def test_the_games_reach_losses_and_units_in_transit_at_an_early_end():
    # guards the property tests against passing vacuously
    lost = transit = 0
    for seed in range(12):
        e = play_game(seed, 3, 2, 3, True, failure=1.0)
        for p in check_game(e, natural=False)["players"].values():
            lost += p["flow"]["lost_transport"]
            transit += p["flow"]["in_transit_end"]
    assert lost > 0 and transit > 0


def test_round_trip_parity_on_a_game_ended_early_with_units_in_transit():
    # failure mode 3
    e = play_game(5, 3, 2, 2, True, failure=1.0, pipeline_qty=5)
    live = e.standings()
    assert any(live["players"][a]["flow"]["in_transit_end"] > 0 for a in e.aliases)
    stored = stored_stats(e)
    for alias in e.aliases:
        assert live["players"][alias]["flow"] == stored["players"][alias]["flow"]


def test_standings_pass_the_initial_pipeline():
    # failure mode 6: lead time 2 x 5 units per slot = 10
    e = play_game(3, 2, 2, 2, True, pipeline_qty=5)
    for a in e.aliases:
        assert e.standings()["players"][a]["flow"]["initial_pipeline"] == 10
    e = play_game(3, 2, 2, 2, True, pipeline_qty=0)
    for a in e.aliases:
        assert e.standings()["players"][a]["flow"]["initial_pipeline"] == 0
    # lead time 0: the pipeline has no slots
    e = play_game(3, 2, 0, 2, True, pipeline_qty=5)
    for a in e.aliases:
        assert e.standings()["players"][a]["flow"]["initial_pipeline"] == 0


def test_an_initial_pipeline_never_makes_in_transit_negative_or_lost_on_a_natural_end():
    for lead in (1, 2):
        e = play_game(9 + lead, 3, lead, None, True, pipeline_qty=5)
        for a in e.aliases:
            f = e.standings()["players"][a]["flow"]
            assert f["initial_pipeline"] == lead * 5
            assert f["in_transit_end"] == 0


def test_in_transit_end_is_zero_on_natural_ends_with_every_lead_time():
    for lead in (0, 1, 2):
        e = play_game(7 + lead, 3, lead, None, True)
        for a in e.aliases:
            assert e.standings()["players"][a]["flow"]["in_transit_end"] == 0


def test_an_early_end_before_day_one_keeps_only_the_initial_pipeline():
    e = play_game(4, 2, 2, 0, True, pipeline_qty=5)
    for a in e.aliases:
        f = e.standings()["players"][a]["flow"]
        assert f["initial_pipeline"] == f["in_transit_end"] == 10
        assert f["initial_stock"] == f["closing_stock"] == f["delivered"] == 0
