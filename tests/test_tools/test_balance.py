"""Black-box tests for the balance harness (plan section 12)."""

from __future__ import annotations

import dataclasses
import json
import statistics
from collections.abc import Sequence
from typing import Any

import pytest

from app.core.calendar import weekday_of
from app.core.config_models import GameConfig
from app.core.engine import PulkyEngine
from app.core.enums import Incoterm, Phase, Weekday
from app.core.kernels import weekday_multiplier
from app.core.limits import MAX_ORDER
from app.core.records import Decision, PeerBuy, PeerSell
from app.core.sampling import halfup
from app.tools import balance
from app.tools.balance import (
    STRATEGIES,
    StrategyResult,
    TargetResult,
    check_targets,
    evaluate,
    main,
    play_game,
)

ALL = ("naive", "order_up_to", "hedger", "insured", "conserver", "coop", "trader")


def make_config(**patches: Any) -> GameConfig:
    """Default config with dot-path patches, e.g. ``{"stock.initial_stock": 0}``."""
    data = GameConfig.default().to_dict()
    for path, value in patches.items():
        node = data
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return GameConfig.from_dict(data)


def seats(n: int) -> dict[str, str]:
    return {f"P{i}": f"Bot {i}" for i in range(1, n + 1)}


def start(config: GameConfig | None = None, n: int = 4, seed: int = 0) -> PulkyEngine:
    return PulkyEngine.start(config or GameConfig.default(), seats(n), seed)


def advance_zero_days(engine: PulkyEngine, days: int) -> None:
    for _ in range(days):
        for alias in engine.aliases:
            engine.submit(alias, Decision.zero(engine.day))
        engine.resolve_day()


def own_demand(engine: PulkyEngine, alias: str, day: int) -> float:
    """Independent restatement of spec 7.2 / 7.3 for ``own(d)``."""
    brief = engine.player_view(alias)["brief"]
    reps = brief["reputation"]
    share = (0.5 + reps[alias]) / sum(0.5 + r for r in reps.values())
    cfg = engine.config
    mult = weekday_multiplier(
        weekday_of(day), cfg.demand.weekday_peak.from_, cfg.demand.weekday_peak.to
    )
    return cfg.demand.per_player_peak * len(reps) * mult * share


class Spy:
    """Wraps a strategy; records every decision with the state it saw."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name
        self.seen: list[tuple[Decision, dict, float, float]] = []

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        d = self.inner.decide(engine, alias)
        view = engine.player_view(alias)
        self.seen.append((d, view, engine.outlay(alias, d), view["cash"]))
        return d


class ProxyEngine:
    """Exposes only what a strategy may read (spec 12 AC 7)."""

    def __init__(self, engine: PulkyEngine, alias: str, log: list[str]) -> None:
        self._engine = engine
        self._alias = alias
        self._log = log

    @property
    def config(self) -> GameConfig:
        return self._engine.config

    def player_view(self, alias: str, full: bool = True) -> dict:
        self._log.append(alias)
        assert alias == self._alias, "strategy read another player's view"
        return self._engine.player_view(alias, full)

    def outlay(self, alias: str, decision: Decision) -> float:
        assert alias == self._alias
        return self._engine.outlay(alias, decision)


class Peeker:
    name = "proxy"

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.log: list[str] = []

    def decide(self, engine: PulkyEngine, alias: str) -> Decision:
        return self.inner.decide(ProxyEngine(engine, alias, self.log), alias)


# --------------------------------------------------------------------- surface


def test_strategies_registry_names() -> None:
    assert tuple(STRATEGIES) == ALL or set(STRATEGIES) == set(ALL)
    for name, strategy in STRATEGIES.items():
        assert strategy.name == name


# ------------------------------------------------------------ worked examples


def test_play_game_naive_example() -> None:
    e = play_game(GameConfig.default(), STRATEGIES["naive"], 4, seed=0)
    assert e.phase == Phase.FINISHED
    assert len(e.market_history()) == 14
    expected = halfup(25 * (0.25 + 0.5 + 0.75 + 1 + 1 + 1 + 0.75) / 7)
    assert expected == 19
    assert e.records("P1")[0].order_spot == expected


def test_naive_orders_the_same_every_open_day_without_extras() -> None:
    e = play_game(GameConfig.default(), STRATEGIES["naive"], 4, seed=3)
    for rec in e.records("P2"):
        assert rec.order_futures == 0
        assert rec.incoterm == Incoterm.EXW
        assert rec.conserve_lots == ()
        assert rec.cooperate is False
        assert rec.peer_buy is None and rec.peer_sell is None
        if rec.day + 1 <= 14:
            assert rec.order_spot == 19


def test_trader_day_one_example() -> None:
    e1 = start()
    d = STRATEGIES["trader"].decide(e1, "P1")
    assert d.peer_buy == PeerBuy(30.0, 30)
    assert d.peer_sell == PeerSell(15.0, 60, 14)
    assert d.order_spot == STRATEGIES["order_up_to"].decide(e1, "P1").order_spot


def test_trader_with_peer_market_off_sends_no_limits() -> None:
    e = start(make_config(**{"peer_market.enabled": False}))
    d = STRATEGIES["trader"].decide(e, "P1")
    assert d.peer_buy is None and d.peer_sell is None


@pytest.mark.parametrize("name", [n for n in ALL if n != "trader"])
def test_only_trader_sends_peer_limits(name: str) -> None:
    e = start()
    d = STRATEGIES[name].decide(e, "P1")
    assert d.peer_buy is None and d.peer_sell is None


def test_evaluate_and_play_game_are_deterministic() -> None:
    cfg = GameConfig.default()
    a = evaluate(cfg, STRATEGIES["coop"], 4, range(3))
    b = evaluate(cfg, STRATEGIES["coop"], 4, range(3))
    assert a == b
    g1 = play_game(cfg, STRATEGIES["trader"], 3, seed=5)
    g2 = play_game(cfg, STRATEGIES["trader"], 3, seed=5)
    assert [r.to_payload() for r in g1.records("P1")] == [
        r.to_payload() for r in g2.records("P1")
    ]
    assert g1.standings() == g2.standings()


# ------------------------------------------------------------- strategy rules


def test_order_up_to_day_one_is_covered_by_initial_stock() -> None:
    # target = own(1) + own(2) = 25*4*(0.25+0.5)*0.25 = 18.75 < stock 20
    e = start()
    assert STRATEGIES["order_up_to"].decide(e, "P1").order_spot == 0


def test_order_up_to_orders_the_difference() -> None:
    cfg = make_config(**{"stock.initial_stock": 0})
    e = start(cfg)
    target = own_demand(e, "P1", 1) + own_demand(e, "P1", 2)
    d = STRATEGIES["order_up_to"].decide(e, "P1")
    assert d.order_spot == halfup(target)
    assert d.incoterm == Incoterm.EXW and d.order_futures == 0
    assert d.conserve_lots == () and d.cooperate is False


def test_order_up_to_counts_backlog_and_never_orders_negative() -> None:
    cfg = make_config(**{"stock.initial_stock": 0})
    e = start(cfg)
    advance_zero_days(e, 2)  # nothing bought: stock stays 0
    view = e.player_view("P1")
    assert view["backlog"] > 0
    target = (
        own_demand(e, "P1", e.day) + own_demand(e, "P1", e.day + 1) + view["backlog"]
    )
    cover = sum(v["qty"] for v in view["in_transit"])
    d = STRATEGIES["order_up_to"].decide(e, "P1")
    assert d.order_spot == max(0, halfup(target - cover))


def test_order_up_to_subtracts_goods_in_transit() -> None:
    cfg = make_config(**{"stock.initial_stock": 0})
    e = start(cfg)
    first = STRATEGIES["order_up_to"].decide(e, "P1")
    assert first.order_spot > 0
    for a in e.aliases:
        e.submit(a, dataclasses.replace(Decision.zero(1), order_spot=first.order_spot))
    e.resolve_day()
    view = e.player_view("P1")
    in_transit = sum(v["qty"] for v in view["in_transit"])
    assert in_transit > 0
    target = own_demand(e, "P1", 2) + own_demand(e, "P1", 3) + view["backlog"]
    stock = sum(c["qty"] for c in view["cellar"])
    cover = stock - view["expected_spoil_tonight"] + in_transit
    d = STRATEGIES["order_up_to"].decide(e, "P1")
    assert d.order_spot == max(0, halfup(target - cover))


def test_spot_closed_means_no_spot_order_for_every_strategy() -> None:
    cfg = make_config(**{"stock.initial_stock": 0})
    e = start(cfg)
    advance_zero_days(e, 13)
    assert e.day == 14
    assert e.player_view("P1")["order_windows"]["spot_open"] is False
    for name in ALL:
        d = STRATEGIES[name].decide(e, "P1")
        assert d.order_spot == 0, name
        assert d.order_futures == 0, name


def test_hedger_buys_forecast_for_peak_arrival_days() -> None:
    e = start()
    assert STRATEGIES["hedger"].decide(e, "P1").order_futures == 0  # day 8 is a Mon
    advance_zero_days(e, 3)
    assert weekday_of(e.day) == Weekday.THU
    d = STRATEGIES["hedger"].decide(e, "P1")
    assert d.order_futures == halfup(own_demand(e, "P1", e.day + 7))
    assert d.order_futures > 0
    assert d.order_spot == STRATEGIES["order_up_to"].decide(e, "P1").order_spot


def test_hedger_never_buys_futures_after_the_window_closes() -> None:
    e = start()
    advance_zero_days(e, 10)
    assert weekday_of(e.day) == Weekday.THU and e.day == 11
    assert e.player_view("P1")["order_windows"]["futures_open"] is False
    assert STRATEGIES["hedger"].decide(e, "P1").order_futures == 0


@pytest.mark.parametrize(
    ("tiers", "expected"),
    [
        ({}, Incoterm.DDP),
        ({"ddp": False}, Incoterm.DAP),
        ({"ddp": False, "dap": False}, Incoterm.CIP),
        ({"ddp": False, "dap": False, "cip": False}, Incoterm.EXW),
    ],
)
def test_insured_uses_most_protective_enabled_tier(
    tiers: dict[str, bool], expected: Incoterm
) -> None:
    patches = {f"insurance.tiers.{t}.enabled": on for t, on in tiers.items()}
    e = start(make_config(**patches))
    assert STRATEGIES["insured"].decide(e, "P1").incoterm == expected


def test_coop_cooperates() -> None:
    e = start()
    d = STRATEGIES["coop"].decide(e, "P1")
    assert d.cooperate is True
    assert d.order_spot == STRATEGIES["order_up_to"].decide(e, "P1").order_spot


def _conserver_expectation(view: dict, cost: float) -> tuple[str, ...]:
    return tuple(
        lot["id"]
        for lot in view["cellar"]
        if not lot["conserved"]
        and lot["expected_loss_tonight"] * lot["unit_cost"] > cost * lot["qty"]
    )


def test_conserver_conserves_lots_whose_loss_exceeds_the_cost() -> None:
    cheap = make_config(**{"stock.conservation_cost_per_unit": 0.0})
    e = start(cheap)
    seen_any = False
    for _ in range(6):
        view = e.player_view("P1")
        d = STRATEGIES["conserver"].decide(e, "P1")
        assert d.conserve_lots == _conserver_expectation(view, 0.0)
        seen_any = seen_any or bool(d.conserve_lots)
        advance_zero_days(e, 1)
    assert seen_any


def test_conserver_skips_lots_when_conservation_is_dear() -> None:
    dear = make_config(**{"stock.conservation_cost_per_unit": 10000.0})
    e = start(dear)
    for _ in range(4):
        assert STRATEGIES["conserver"].decide(e, "P1").conserve_lots == ()
        advance_zero_days(e, 1)


def test_conserver_does_not_reconserve_conserved_lots() -> None:
    cheap = make_config(**{"stock.conservation_cost_per_unit": 0.0})
    e = start(cheap)
    d = STRATEGIES["conserver"].decide(e, "P1")
    while not d.conserve_lots and e.day < 5:
        advance_zero_days(e, 1)
        d = STRATEGIES["conserver"].decide(e, "P1")
    assert d.conserve_lots
    e.submit(
        "P1", dataclasses.replace(Decision.zero(e.day), conserve_lots=d.conserve_lots)
    )
    for a in e.aliases[1:]:
        e.submit(a, Decision.zero(e.day))
    e.resolve_day()
    view = e.player_view("P1")
    again = STRATEGIES["conserver"].decide(e, "P1")
    for lot in view["cellar"]:
        if lot["conserved"]:
            assert lot["id"] not in again.conserve_lots


# --------------------------------------------------------------- trader rules


def test_trader_sell_offer_is_none_when_stock_does_not_exceed_demand() -> None:
    # own(1) = 6.25 -> 6 ; stock 6 leaves max_units 0, stock 0 leaves a negative
    for stock in (0, 5, 6):
        e = start(make_config(**{"stock.initial_stock": stock}))
        d = STRATEGIES["trader"].decide(e, "P1")
        assert d.peer_sell is None, stock
        assert d.peer_buy == PeerBuy(30.0, 30)
    e = start(make_config(**{"stock.initial_stock": 7}))
    assert STRATEGIES["trader"].decide(e, "P1").peer_sell == PeerSell(15.0, 60, 1)


def test_trader_prices_stay_in_peer_bounds() -> None:
    cfg = make_config(
        **{
            "demand.retail_price": 10000,
            "supplier.base_price": 9000,
            "supplier.spot_spread": 0.9,
        }
    )
    d = STRATEGIES["trader"].decide(start(cfg), "P1")
    assert d.peer_buy is not None and d.peer_buy.max_price == 10000.0
    cfg = make_config(**{"demand.retail_price": 0.001, "stock.salvage_value": 0.0})
    d = STRATEGIES["trader"].decide(start(cfg), "P1")
    assert d.peer_buy is not None and d.peer_buy.max_price == 0.01


def test_trader_never_sends_empty_offers_over_a_game() -> None:
    spy = Spy(STRATEGIES["trader"])
    play_game(GameConfig.default(), spy, 4, seed=2)
    assert spy.seen
    for d, _view, _out, _cash in spy.seen:
        if d.peer_sell is not None:
            assert 1 <= d.peer_sell.max_units <= MAX_ORDER


# ------------------------------------------------------- acceptance criterion 1


@pytest.mark.parametrize("peer", [True, False])
@pytest.mark.parametrize("n", [2, 12])
@pytest.mark.parametrize("name", ALL)
def test_every_strategy_plays_a_full_game(name: str, n: int, peer: bool) -> None:
    cfg = make_config(**{"peer_market.enabled": peer})
    e = play_game(cfg, STRATEGIES[name], n, seed=1)
    assert e.phase == Phase.FINISHED
    assert len(e.records("P1")) == 14


# ------------------------------------------- acceptance criterion 2 / FM 1, 2


@pytest.mark.parametrize("cash", [5000.0, 100.0, 0.0])
@pytest.mark.parametrize("name", ALL)
def test_decisions_are_affordable_and_respect_windows(name: str, cash: float) -> None:
    cfg = make_config(**{"starting_cash": cash})
    spy = Spy(STRATEGIES[name])
    e = play_game(cfg, spy, 4, seed=4)
    assert e.phase == Phase.FINISHED
    assert len(spy.seen) == 4 * 14
    for d, view, outlay, cash_now in spy.seen:
        assert outlay == 0 or outlay <= cash_now, (name, d.day)
        windows = view["order_windows"]
        if not windows["spot_open"]:
            assert d.order_spot == 0
        if not windows["futures_open"]:
            assert d.order_futures == 0


@pytest.mark.parametrize("name", ALL)
def test_no_spot_order_on_the_last_day(name: str) -> None:
    spy = Spy(STRATEGIES[name])
    play_game(GameConfig.default(), spy, 4, seed=0)
    last = [d for d, view, _o, _c in spy.seen if view["day"] == 14]
    assert len(last) == 4
    assert all(d.order_spot == 0 and d.order_futures == 0 for d in last)


def test_trimming_orders_the_largest_affordable_quantity() -> None:
    cfg = make_config(**{"starting_cash": 100.0, "stock.initial_stock": 0})
    e = start(cfg)
    d = STRATEGIES["naive"].decide(e, "P1")
    assert d.order_spot > 0
    assert e.outlay("P1", d) <= 100.0
    bigger = dataclasses.replace(d, order_spot=d.order_spot + 1)
    assert e.outlay("P1", bigger) > 100.0


def test_trimming_drops_the_tier_and_conservation_last() -> None:
    # Cash covers the orders under EXW only: the DDP fee must not eat the order
    # before the tier is dropped (spot first, then futures, conserve, tier).
    cfg = make_config(**{"starting_cash": 0.0})
    e = start(cfg)
    d = STRATEGIES["insured"].decide(e, "P1")
    assert d.order_spot == 0 and d.order_futures == 0
    assert d.conserve_lots == ()
    assert e.outlay("P1", d) == 0


def test_peer_limits_are_not_trimmed_by_cash() -> None:
    cfg = make_config(**{"starting_cash": 0.0})
    d = STRATEGIES["trader"].decide(start(cfg), "P1")
    assert d.peer_buy == PeerBuy(30.0, 30)


# ------------------------------------------------------------------ FM 3 / AC 7


@pytest.mark.parametrize("name", ALL)
def test_strategies_work_through_a_restricted_proxy(name: str) -> None:
    peeker = Peeker(STRATEGIES[name])
    e = play_game(GameConfig.default(), peeker, 4, seed=0)
    assert e.phase == Phase.FINISHED
    assert peeker.log, "strategy never read its view"


# ------------------------------------------------------------------- evaluate


def _games(
    cfg: GameConfig, name: str, n: int, seeds: Sequence[int]
) -> list[PulkyEngine]:
    return [play_game(cfg, STRATEGIES[name], n, s) for s in seeds]


def test_evaluate_summarises_standings() -> None:
    cfg = GameConfig.default()
    seeds = range(3)
    res = evaluate(cfg, STRATEGIES["order_up_to"], 4, seeds)
    assert isinstance(res, StrategyResult)
    assert res.strategy == "order_up_to" and res.shared is False and res.games == 3
    profits, fills, shares, backlog = [], [], [], []
    wastes: list[float] = []
    for g in _games(cfg, "order_up_to", 4, seeds):
        for p in g.standings()["players"].values():
            profits.append(p["total_profit"])
            fills.append(p["fill_rate"])
            shares.append(p["overall_market_share"])
            backlog.append(p["days_with_backlog"])
            if p["waste_rate"] is not None:
                wastes.append(p["waste_rate"])
    assert res.profit_mean == pytest.approx(statistics.fmean(profits))
    assert res.profit_sd in (
        pytest.approx(statistics.pstdev(profits)),
        pytest.approx(statistics.stdev(profits)),
    )
    assert res.waste_rate_mean == pytest.approx(statistics.fmean(wastes))
    assert res.waste_rate_median == pytest.approx(statistics.median(wastes))
    assert res.fill_rate_mean == pytest.approx(
        statistics.fmean(f for f in fills if f is not None)
    )
    assert res.market_share_mean == pytest.approx(statistics.fmean(shares))
    assert res.backlog_days_mean == pytest.approx(statistics.fmean(backlog))


def test_evaluate_marks_shared_transport_from_the_config() -> None:
    cfg = make_config(**{"transport.shared": True})
    assert evaluate(cfg, STRATEGIES["insured"], 3, range(2)).shared is True


def test_evaluate_leaves_out_missing_ratios() -> None:
    # nobody ever buys and nothing is in stock: waste_rate is None for everyone
    cfg = make_config(**{"stock.initial_stock": 0})
    res = evaluate(cfg, STRATEGIES["naive"], 3, range(1))
    assert res.waste_rate_mean == res.waste_rate_mean  # not NaN


def test_disruption_metrics_none_without_disruptions() -> None:
    cfg = make_config(**{"supplier.disruption_probability": 0.0})
    res = evaluate(cfg, STRATEGIES["trader"], 4, range(2))
    assert res.disruption_fill_mean is None
    assert res.disruption_sold_mean is None
    assert res.disruption_trade_day_share is None


def test_disruption_metrics_match_an_independent_computation() -> None:
    cfg = make_config(**{"supplier.disruption_probability": 0.5})
    seeds = range(3)
    n = 4
    games = _games(cfg, "trader", n, seeds)
    fills, sold, normal = [], [], []
    dis_days = trade_days = 0
    peak = set()
    lo, hi = cfg.demand.weekday_peak.from_, cfg.demand.weekday_peak.to
    peak_bl, off_bl = [], []
    for g in games:
        market = {m.day: m for m in g.market_history()}
        dis_weekdays = {m.weekday for m in market.values() if m.disrupted}
        for m in market.values():
            if m.disrupted:
                dis_days += 1
                trade_days += m.peer_units > 0
        for alias in g.aliases:
            for r in g.records(alias):
                if market[r.day].disrupted:
                    fills.append(r.fill)
                    sold.append(r.sold)
                elif r.weekday in dis_weekdays:
                    normal.append(r.sold)
                is_peak = weekday_multiplier(r.weekday, lo, hi) == 1.0
                (peak_bl if is_peak else off_bl).append(r.backlog_end)
    assert dis_days > 0
    res = evaluate(cfg, STRATEGIES["trader"], n, seeds)
    assert res.disruption_fill_mean == pytest.approx(statistics.fmean(fills))
    assert res.disruption_sold_mean == pytest.approx(statistics.fmean(sold))
    assert res.disruption_trade_day_share == pytest.approx(trade_days / dis_days)
    assert res.normal_sold_mean == pytest.approx(statistics.fmean(normal))
    assert res.peak_backlog_mean == pytest.approx(statistics.fmean(peak_bl))
    assert res.offpeak_backlog_mean == pytest.approx(statistics.fmean(off_bl))
    del peak


# ---------------------------------------------------------------- check_targets


def _res(strategy: str, shared: bool = False, **over: Any) -> StrategyResult:
    base: dict[str, Any] = dict(
        strategy=strategy,
        shared=shared,
        games=10,
        profit_mean=1000.0,
        profit_sd=100.0,
        waste_rate_mean=0.10,
        waste_rate_median=0.10,
        fill_rate_mean=0.8,
        market_share_mean=0.25,
        backlog_days_mean=2.0,
        disruption_fill_mean=0.5,
        disruption_sold_mean=10.0,
        disruption_trade_day_share=0.5,
        normal_sold_mean=20.0,
        peak_backlog_mean=5.0,
        offpeak_backlog_mean=1.0,
    )
    base.update(over)
    return StrategyResult(**base)


def passing_results() -> dict[tuple[str, bool], StrategyResult]:
    r = {
        ("naive", False): _res("naive", profit_mean=800.0),
        ("order_up_to", False): _res("order_up_to", profit_mean=1000.0),
        ("hedger", False): _res(
            "hedger", profit_mean=1000.0, profit_sd=80.0, disruption_fill_mean=0.6
        ),
        ("insured", False): _res("insured", profit_mean=970.0, profit_sd=80.0),
        ("conserver", False): _res(
            "conserver", profit_mean=1010.0, waste_rate_mean=0.05
        ),
        ("coop", False): _res("coop"),
        ("trader", False): _res(
            "trader", disruption_trade_day_share=0.6, disruption_fill_mean=0.7
        ),
        ("insured", True): _res("insured", True, profit_mean=900.0),
        ("order_up_to", True): _res("order_up_to", True, profit_mean=1000.0),
    }
    return r


def run(results: dict[tuple[str, bool], StrategyResult]) -> dict[int, TargetResult]:
    out = check_targets(results)
    assert [t.number for t in out] == [1, 2, 3, 4, 5, 6, 7]
    for t in out:
        assert isinstance(t.detail, str) and t.detail
        assert "\n" not in t.detail
    return {t.number: t for t in out}


def test_check_targets_all_pass_on_a_good_map() -> None:
    assert all(t.passed for t in run(passing_results()).values())


def _mutate(
    key: tuple[str, bool], **over: Any
) -> dict[tuple[str, bool], StrategyResult]:
    r = passing_results()
    r[key] = dataclasses.replace(r[key], **over)
    return r


@pytest.mark.parametrize(
    ("key", "over", "failing"),
    [
        # 1: needs >= 1.10 x naive
        (("naive", False), {"profit_mean": 950.0}, 1),
        # 2: median waste in [0.05, 0.20]
        (("order_up_to", False), {"waste_rate_median": 0.04}, 2),
        (("order_up_to", False), {"waste_rate_median": 0.21}, 2),
        # 3: hedger within 5%, lower sd, higher disruption fill
        (("hedger", False), {"profit_mean": 940.0}, 3),
        (("hedger", False), {"profit_sd": 100.0}, 3),
        (("hedger", False), {"disruption_fill_mean": 0.5}, 3),
        # 4 (not shared)
        (("insured", False), {"profit_mean": 940.0}, 4),
        (("insured", False), {"profit_mean": 1001.0}, 4),
        (("insured", False), {"profit_sd": 100.0}, 4),
        # 4 (shared): must be more than 5% below
        (("insured", True), {"profit_mean": 960.0}, 4),
        # 5
        (("conserver", False), {"waste_rate_mean": 0.10}, 5),
        (("conserver", False), {"profit_mean": 999.0}, 5),
        # 6
        (("order_up_to", False), {"disruption_sold_mean": 0.0}, 6),
        (("order_up_to", False), {"disruption_sold_mean": 18.0}, 6),
        (("naive", False), {"peak_backlog_mean": 1.0}, 6),
        # 7
        (("trader", False), {"disruption_trade_day_share": 0.49}, 7),
        (("trader", False), {"disruption_fill_mean": 0.5}, 7),
    ],
)
def test_each_target_fails_for_its_own_condition(
    key: tuple[str, bool], over: dict[str, Any], failing: int
) -> None:
    out = run(_mutate(key, **over))
    assert out[failing].passed is False
    for number, t in out.items():
        if number != failing:
            assert t.passed is True, number


@pytest.mark.parametrize(
    ("key", "over", "number"),
    [
        (("naive", False), {"profit_mean": 950.0}, 1),
        (("order_up_to", False), {"waste_rate_median": 0.05}, 2),
        (("order_up_to", False), {"waste_rate_median": 0.20}, 2),
    ],
)
def test_inclusive_bounds(
    key: tuple[str, bool], over: dict[str, Any], number: int
) -> None:
    r = passing_results()
    if number == 1:
        r[("naive", False)] = dataclasses.replace(r[key], profit_mean=1000.0 / 1.10)
        assert run(r)[1].passed is True
        r[("naive", False)] = dataclasses.replace(
            r[key], profit_mean=over["profit_mean"]
        )
        assert run(r)[1].passed is False
    else:
        assert run(_mutate(key, **over))[number].passed is True


def test_target_four_shared_variant_and_missing_inputs() -> None:
    r = passing_results()
    del r[("insured", True)]
    out = run(r)
    assert out[4].passed is False  # not a silent pass
    r = passing_results()
    del r[("order_up_to", True)]
    assert run(r)[4].passed is False


def test_missing_trader_fails_target_seven_only() -> None:
    r = passing_results()
    del r[("trader", False)]
    out = run(r)
    assert out[7].passed is False
    assert all(out[n].passed for n in range(1, 7))


@pytest.mark.parametrize(
    ("key", "field", "number"),
    [
        (("hedger", False), "disruption_fill_mean", 3),
        (("order_up_to", False), "disruption_sold_mean", 6),
        (("trader", False), "disruption_trade_day_share", 7),
        (("trader", False), "disruption_fill_mean", 7),
    ],
)
def test_none_inputs_fail_the_target(
    key: tuple[str, bool], field: str, number: int
) -> None:
    out = run(_mutate(key, **{field: None}))
    assert out[number].passed is False
    assert out[number].detail


@pytest.mark.parametrize(
    "key",
    [
        ("naive", False),
        ("order_up_to", False),
        ("hedger", False),
        ("conserver", False),
        ("insured", False),
    ],
)
def test_missing_strategy_never_passes_silently(key: tuple[str, bool]) -> None:
    r = passing_results()
    del r[key]
    out = run(r)
    assert any(not t.passed for t in out.values())


def test_empty_results_fail_every_target() -> None:
    out = run({})
    assert not any(t.passed for t in out.values())


# ----------------------------------------------------------------------- main


def test_main_json_lists_every_pair_and_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--seeds", "2", "--json"])
    out = json.loads(capsys.readouterr().out)
    results = out["results"]
    targets = out["targets"]
    pairs = {(r["strategy"], r["shared"]) for r in results}
    assert pairs == {(n, False) for n in ALL} | {
        ("insured", True),
        ("order_up_to", True),
    }
    assert len(results) == 9
    assert [t["number"] for t in targets] == [1, 2, 3, 4, 5, 6, 7]
    for t in targets:
        assert isinstance(t["passed"], bool) and t["detail"]
    assert code == (0 if all(t["passed"] for t in targets) else 1)
    assert all(r["games"] == 2 for r in results)


def test_main_strategy_filter(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--seeds", "1", "--players", "3", "--strategy", "naive", "coop", "--json"]
    )
    out = json.loads(capsys.readouterr().out)
    assert {r["strategy"] for r in out["results"]} == {"naive", "coop"}
    assert code == 1  # targets lacking inputs fail


def test_main_table_output_without_json(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--seeds", "1", "--strategy", "naive"])
    text = capsys.readouterr().out
    assert "naive" in text


def test_module_exposes_main() -> None:
    assert callable(balance.main)
