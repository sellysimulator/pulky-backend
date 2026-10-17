"""Black-box tests for section 11: spec 8.7 invariants, replay, order independence."""

import copy
import json
import random
from collections import Counter, defaultdict

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.engine import PulkyEngine
from app.core.enums import WEEKDAYS, IncotermTier, TradeSide
from app.core.money import round_cents
from app.core.records import Decision, PeerBuy, PeerSell

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
TIERS = (CIP, DAP, DDP)


# ------------------------------------------------------------ random games


def random_config(rnd):
    n = rnd.choice([2, 3, 4])
    lead = rnd.choice([0, 1, 2])
    fresh = rnd.randint(0, 2)
    qmax = rnd.choice([100, 95, 90])
    lo = rnd.randint(0, 6)
    hi = rnd.randint(lo, 6)
    return cfg(
        max_players=n,
        duration_days=7,
        starting_cash=rnd.choice([200, 800, 5000]),
        demand__per_player_peak=rnd.choice([5, 20, 60]),
        demand__weekday_peak__from=WEEKDAYS[lo].value,
        demand__weekday_peak__to=WEEKDAYS[hi].value,
        demand__retail_price=rnd.choice([20, 30]),
        demand__backlog_penalty=rnd.choice([0, 1.5]),
        reputation__starting=rnd.choice([0.3, 0.5, 0.8]),
        reputation__smoothing=rnd.choice([0.1, 0.3]),
        reputation__waste_weight=rnd.choice([0.0, 0.3]),
        supplier__base_price=rnd.choice([8.0, 12.0, 15.5]),
        supplier__spot_spread=rnd.choice([0, 0.25, 0.5]),
        supplier__capacity_mean_per_player=rnd.choice([3, 10, 22]),
        supplier__capacity_sd_per_player=rnd.choice([0, 5]),
        supplier__disruption_probability=rnd.choice([0, 0.3]),
        supplier__lead_time_days=lead,
        supplier__initial_pipeline_qty=rnd.choice([0, 5]),
        supplier__quality_min=min(qmax, rnd.choice([qmax, 80, 60])),
        supplier__quality_max=qmax,
        futures__horizon_days=rnd.randint(lead + 1, 6),
        stock__fresh_days=fresh,
        stock__max_days=fresh + rnd.randint(1, 4),
        stock__initial_stock=rnd.choice([0, 20, 40]),
        stock__salvage_value=rnd.choice([0, 2.0]),
        stock__conservation_extra_fresh_days=rnd.randint(1, 3),
        stock__conservation_cost_per_unit=rnd.choice([0.5, 1.0]),
        peer_market__enabled=rnd.random() < 0.8,
        transport__shared=rnd.random() < 0.5,
        transport__failure_probability=rnd.choice([0, 0.3, 1.0]),
        transport__loss_fraction=rnd.choice([0.3, 0.5]),
        insurance__tiers__cip__enabled=rnd.random() < 0.8,
        insurance__tiers__dap__enabled=rnd.random() < 0.8,
        insurance__tiers__ddp__fee=rnd.choice([0.99, 3.0]),
    )


def choose(e, alias, rnd):
    """A random decision that the engine accepts (shrunk until affordable)."""
    v = e.player_view(alias)
    day, cash, w = e.day, v["cash"], v["order_windows"]
    tiers = [EXW] + [
        t for t in TIERS if getattr(e.config.insurance.tiers, t.value).enabled
    ]
    spot = rnd.choice([0, 3, 10, 25, 60]) if w["spot_open"] else 0
    fut = rnd.choice([0, 0, 5, 20]) if w["futures_open"] else 0
    tier = rnd.choice(tiers)
    free = [x["id"] for x in v["cellar"] if not x["conserved"]]
    conserve = tuple(i for i in free if rnd.random() < 0.3)[:4]
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
    decision = D(day, spot, fut, tier, conserve, coop, buy, sell)
    for _ in range(12):
        outlay = e.outlay(alias, decision)
        if outlay <= 0 or round_cents(outlay) <= cash:
            return decision
        decision = D(
            day,
            decision.order_spot // 2,
            decision.order_futures // 2,
            decision.incoterm_tier if decision.order_spot else EXW,
            decision.conserve_lots[:-1],
            coop,
            buy,
            sell,
        )
    return D(day, 0, 0, EXW, (), coop, buy, sell)


def roundtrip(e):
    return PulkyEngine.from_payload(json.loads(json.dumps(e.to_payload())), e.config)


def play_random(seed, *, shuffle=None, check=None, days=None, config=None, rt=False):
    config = config or random_config(random.Random(seed))
    items = list(seats(config.max_players).items())
    if shuffle is not None:
        random.Random(shuffle).shuffle(items)
    e = PulkyEngine.start(config, dict(items), seed)
    for _ in range(days or config.duration_days):
        day = e.day
        decisions = {
            a: choose(e, a, random.Random(f"{seed}:{day}:{a}")) for a in e.aliases
        }
        order = list(e.aliases)
        if shuffle is not None:
            random.Random(f"{shuffle}:{day}").shuffle(order)
        for alias in order:
            e.submit(alias, decisions[alias])
            if rt:
                e = roundtrip(e)
        out = e.resolve_day()
        if rt:
            e = roundtrip(e)
        if check:
            check(e, out, decisions)
    return e


# ------------------------------------------------------------- the checker


class Checker:
    """Spec 8.7, asserted after every resolution."""

    def __init__(self, e):
        self.cash = {a: e.config.starting_cash for a in e.aliases}
        self.flow = defaultdict(int)  # shipped - lost - delivered, per alias
        lead = e.config.supplier.lead_time_days
        self.initial = lead * e.config.supplier.initial_pipeline_qty
        self.seen = Counter()

    def __call__(self, e, out, decisions):
        day = out.day
        md = e.market_history()[-1]
        assert md.day == day == len(e.market_history())
        recs = out.records
        cap = md.capacity
        for alias, r in recs.items():
            d = decisions[alias]
            assert r.day == day
            assert r.opening_stock + r.delivered + r.peer_bought == (
                r.sold + r.peer_sold + r.spoiled + r.closing_stock
            )
            assert r.backlog_open + r.demand == r.sold + r.backlog_end
            assert r.shipped == r.allocated_spot + r.futures_filled
            assert 0 <= r.lost_transport <= r.shipped
            assert r.shortfall_spot == r.order_spot - r.allocated_spot
            assert 0 <= r.peer_bought <= r.shortfall_spot
            assert r.futures_filled <= r.futures_due
            assert r.futures_filled + r.futures_unfilled == r.futures_due
            assert 0 <= r.allocated_spot <= r.order_spot
            assert min(r.sold, r.backlog_end, r.spoiled, r.closing_stock) >= 0
            assert 0.0 <= r.reputation_end <= 1.0 and 0.0 <= r.fill <= 1.0
            assert 0.0 <= r.waste <= 1.0
            # the decision is echoed
            assert (r.order_spot, r.order_futures, r.incoterm_tier) == (
                d.order_spot,
                d.order_futures,
                d.incoterm_tier,
            )
            assert r.conserve_lots == d.conserve_lots and r.cooperate == d.cooperate
            assert r.peer_buy == d.peer_buy and r.peer_sell == d.peer_sell
            assert r.lots_conserved == len(d.conserve_lots)
            # money
            assert r.outlay == 0 or r.outlay <= r.cash_open + 1e-9
            assert r.cash_open == self.cash[alias]
            assert r.cash_end == round_cents(r.cash_open + r.profit)
            self.cash[alias] = r.cash_end
            assert e.player_view(alias)["cash"] == r.cash_end or out.finished
            lines = (
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
            assert r.profit == pytest.approx(lines, abs=0.07)
            self.flow[alias] += r.shipped - r.lost_transport - r.delivered
            self.seen["unfilled"] += r.futures_unfilled > 0
            self.seen["short"] += r.shortfall_spot > 0
            self.seen["lost"] += r.lost_transport > 0
            self.seen["backlog"] += r.backlog_end > 0
            self.seen["spoiled"] += r.spoiled > 0
            self.seen["conserved"] += r.lots_conserved > 0
        # the supplier
        assert sum(r.futures_filled + r.allocated_spot for r in recs.values()) <= cap
        spot_left = cap - sum(r.futures_filled for r in recs.values())
        orders = sum(r.order_spot for r in recs.values())
        assert sum(r.allocated_spot for r in recs.values()) == min(spot_left, orders)
        sold = sum(r.sold for r in recs.values())
        shares = sum(r.market_share for r in recs.values())
        assert shares == pytest.approx(1.0 if sold else 0.0, abs=1e-9)
        assert md.units_sold_total == sold == out.public.units_sold_total
        assert md.demand_total == sum(r.demand for r in recs.values())
        self.peer_day(e, out, decisions)
        if not out.finished:
            self.state(e)
        else:
            self.finish(e)

    def peer_day(self, e, out, decisions):
        recs = out.records
        day = out.day
        if not e.config.peer_market.enabled:
            for r in recs.values():
                assert r.peer_bought == r.peer_sold == 0
                assert r.cost_peer == r.revenue_peer == 0.0 and r.peer_trades == ()
            return
        buys, sells = [], []
        for alias, r in recs.items():
            mine = [t for t in r.peer_trades if t.side == TradeSide.BUY]
            assert [t.lot_id for t in mine] == [
                f"B{day}-{i}" for i in range(1, len(mine) + 1)
            ]
            assert not any(
                t.lot_id.startswith(f"B{day}-")
                for t in r.peer_trades
                if t.side == TradeSide.SELL
            )
            assert r.peer_bought == sum(t.qty for t in mine)
            assert r.cost_peer == round_cents(sum(t.qty * t.price for t in mine))
            sold_trades = [t for t in r.peer_trades if t.side == TradeSide.SELL]
            assert r.peer_sold == sum(t.qty for t in sold_trades)
            assert r.revenue_peer == round_cents(
                sum(t.qty * t.price for t in sold_trades)
            )
            if r.peer_sold:
                assert r.peer_sold <= decisions[alias].peer_sell.max_units
            for t in mine:
                buy = decisions[alias].peer_buy
                offer = decisions[t.counterparty].peer_sell
                assert buy is not None and offer is not None
                assert t.counterparty != alias
                assert t.quality >= buy.min_quality and t.quality <= offer.max_quality
                assert t.price == offer.price <= buy.max_price
                buys.append((alias, t.counterparty, t.qty, t.quality, t.price))
            for t in sold_trades:
                sells.append((t.counterparty, alias, t.qty, t.quality, t.price))
            cash_market = (
                r.cash_open
                + r.refund_futures
                + r.refund_insurance
                - r.cost_conservation
                - r.cost_futures
                - r.cost_spot
                - r.cost_transport
                - r.cost_insurance
            )
            assert r.cost_peer <= max(0.0, cash_market) + 0.011
            if r.peer_bought:
                assert r.shortfall_spot > 0
            self.seen["peer"] += r.peer_bought
        assert sorted(buys) == sorted(sells)
        assert sum(r.peer_bought for r in recs.values()) == sum(
            r.peer_sold for r in recs.values()
        )
        cost = sum(round(r.cost_peer * 100) for r in recs.values())
        revenue = sum(round(r.revenue_peer * 100) for r in recs.values())
        assert cost == revenue

    def state(self, e):
        config = e.config
        qmin, qmax = config.supplier.quality_min, config.supplier.quality_max
        extra = config.stock.conservation_extra_fresh_days
        for alias in e.aliases:
            v = e.player_view(alias)
            for lot in v["cellar"]:
                assert lot["qty"] > 0 and lot["quality"] >= 0
                assert qmin <= lot["initial_quality"] <= qmax
                limit = config.stock.max_days + (extra if lot["conserved"] else 0)
                assert lot["age"] < limit
            assert 0.0 <= v["reputation"] <= 1.0
            in_transit = sum(x["qty"] for x in v["in_transit"])
            recs = e.records(alias)
            assert self.initial + sum(r.shipped - r.lost_transport for r in recs) == (
                sum(r.delivered for r in recs) + in_transit
            )
            host = e.host_view()["players"][alias]
            base = config.supplier.base_price
            paid = sum(r.cost_futures for r in recs)
            units = sum(r.futures_filled + r.futures_unfilled for r in recs)
            assert paid == pytest.approx(
                base * (units + host["open_futures_units"]), abs=0.01 * len(recs)
            )

    def finish(self, e):
        assert e.phase.value == "finished"
        for alias in e.aliases:
            recs = e.records(alias)
            s = e.settlements()[alias]
            total = sum(r.profit for r in recs) + s.settlement_total
            assert e.player_view(alias)["cash"] == pytest.approx(
                e.config.starting_cash + total, abs=0.011
            )
            assert e.player_view(alias)["cash"] == round_cents(
                recs[-1].cash_end + s.settlement_total
            )
            assert s.futures_cancelled_units == 0
            paid = sum(r.cost_futures for r in recs)
            units = sum(r.futures_filled + r.futures_unfilled for r in recs)
            assert paid == pytest.approx(
                e.config.supplier.base_price * units, abs=0.01 * len(recs)
            )
        json.dumps(e.results())


# ------------------------------------------------------------------ tests


@settings(max_examples=200, deadline=None, derandomize=True)
@given(seed=st.integers(min_value=0, max_value=10**9))
def test_every_spec_invariant_holds_after_every_resolution(seed):
    checkers = []

    def check(e, out, decisions):
        if not checkers:
            checkers.append(Checker(e))
        checkers[0](e, out, decisions)

    e = play_random(seed, check=check)
    assert e.phase.value == "finished"


def test_the_random_games_exercise_the_rules_the_invariants_guard():
    seen = Counter()
    for seed in range(60):
        checkers = []

        def check(e, out, decisions, checkers=checkers):
            if not checkers:
                checkers.append(Checker(e))
            checkers[0](e, out, decisions)

        play_random(seed, check=check)
        seen.update(checkers[0].seen)
    for key in ("unfilled", "short", "lost", "backlog", "spoiled", "conserved", "peer"):
        assert seen[key] > 0, key


def test_peer_market_creates_no_money_and_no_units_over_200_seeds():
    # spec 14 #21
    trading_days = 0
    for seed in range(200):
        rnd = random.Random(seed)
        config = quiet(
            3,
            supplier__capacity_mean_per_player=rnd.choice([1, 3]),
            supplier__lead_time_days=rnd.choice([0, 1]),
            supplier__quality_min=rnd.choice([100, 70]),
            demand__per_player_peak=rnd.choice([3, 20]),
        )
        e = PulkyEngine.start(config, seats(3), seed)
        for _ in range(3):
            day = e.day
            decisions = {}
            for alias in e.aliases:
                r = random.Random(f"{seed}:{day}:{alias}")
                decisions[alias] = D(
                    day,
                    r.randint(0, 20),
                    buy=(
                        PeerBuy(r.randint(800, 2500) / 100, r.choice([0, 40, 80]))
                        if r.random() < 0.7
                        else None
                    ),
                    sell=(
                        PeerSell(
                            r.randint(800, 2500) / 100,
                            r.choice([60, 100]),
                            r.randint(1, 30),
                        )
                        if r.random() < 0.7
                        else None
                    ),
                )
            out = play(e, decisions)
            recs = out.records.values()
            assert sum(r.peer_bought for r in recs) == sum(r.peer_sold for r in recs)
            assert sum(round(r.cost_peer * 100) for r in recs) == sum(
                round(r.revenue_peer * 100) for r in recs
            )
            trading_days += any(r.peer_bought for r in recs)
    assert trading_days >= 20


def test_replay_is_byte_identical():
    for seed in (1, 2, 3, 4, 5):
        a = play_random(seed)
        b = play_random(seed)
        assert json.dumps(a.to_payload()) == json.dumps(b.to_payload())
        for alias in a.aliases:
            assert a.records(alias) == b.records(alias)
        assert a.public_history() == b.public_history()
        assert a.market_history() == b.market_history()
    assert json.dumps(play_random(1).to_payload()) != json.dumps(
        play_random(2).to_payload()
    )


def test_a_payload_round_trips_at_every_point_of_a_game():
    for seed in (4, 8, 15):
        config = random_config(random.Random(seed))
        e = PulkyEngine.start(config, seats(config.max_players), seed)
        for _ in range(config.duration_days):
            day = e.day
            for alias in e.aliases:
                rnd = random.Random(f"{seed}:{day}:{alias}")
                e.submit(alias, choose(e, alias, rnd))
                payload = e.to_payload()
                json.dumps(payload)
                assert PulkyEngine.from_payload(payload, config).to_payload() == payload
            e.resolve_day()
            payload = e.to_payload()
            assert PulkyEngine.from_payload(payload, config).to_payload() == payload
        assert e.phase.value == "finished"


def test_a_game_continued_after_round_trips_equals_one_that_was_not():
    for seed in (4, 8, 15, 16):
        straight = play_random(seed)
        resumed = play_random(seed, rt=True)
        assert json.dumps(straight.to_payload()) == json.dumps(resumed.to_payload())
        for alias in straight.aliases:
            assert straight.records(alias) == resumed.records(alias)
        assert straight.results() == resumed.results()


def test_seat_order_changes_nothing():
    for seed in range(40):
        base = play_random(seed)
        other = play_random(seed, shuffle=seed + 1000)
        assert base.aliases == other.aliases
        for alias in base.aliases:
            assert base.records(alias) == other.records(alias), (seed, alias)
        assert base.public_history() == other.public_history()
        assert base.market_history() == other.market_history()
        assert base.results() == other.results()


@pytest.mark.parametrize("cooperate", [True, False])
def test_scarce_days_ignore_seat_order_in_both_allocation_modes(cooperate):
    config = quiet(5, supplier__capacity_mean_per_player=8, futures__horizon_days=3)
    orders = {"P1": 20, "P2": 20, "P3": 20, "P4": 13, "P5": 7}  # ties everywhere
    results = []
    for shuffle in range(8):
        items = list(seats(5).items())
        random.Random(shuffle).shuffle(items)
        e = PulkyEngine.start(config, dict(items), 17)
        outs = []
        for day in (1, 2, 3, 4):
            decisions = {
                a: D(
                    day,
                    q,
                    5 if day == 1 else 0,
                    coop=cooperate,
                    buy=PeerBuy(15.0, 0),
                    sell=PeerSell(14.0, 100, 6),
                )
                for a, q in orders.items()
            }
            out = play(e, decisions)
            outs.append((out.records, out.public))
        results.append(outs)
    assert all(r == results[0] for r in results[1:])
    allocated = [results[0][0][0][a].allocated_spot for a in orders]
    assert sum(allocated) == 40 < sum(orders.values())  # genuinely scarce


# ------------------------------------------------------------------- sizes


@pytest.fixture(scope="module")
def big_game():
    config = cfg(max_players=12, duration_days=60)
    e = PulkyEngine.start(config, seats(12), 99)
    for _ in range(59):
        day = e.day
        for alias in e.aliases:
            e.submit(alias, choose(e, alias, random.Random(f"99:{day}:{alias}")))
        e.resolve_day()
    assert e.day == 60
    return e


def test_daily_forms_stay_small_on_day_sixty_of_a_twelve_player_game(big_game):
    e = big_game
    host = len(json.dumps(e.host_view(full=False)))
    assert host < 50 * 1024, host
    for alias in e.aliases:
        player = len(json.dumps(e.player_view(alias, full=False)))
        assert player < 10 * 1024, (alias, player)
    assert len(json.dumps(e.host_view())) > 4 * host


def test_the_engine_payload_of_a_sixty_day_twelve_player_game_fits_one_megabyte(
    big_game,
):
    e = big_game
    assert len(json.dumps(e.to_payload())) < 1_000_000
    for alias in e.aliases:
        e.submit(alias, Decision.zero(60))
    e.resolve_day()
    assert e.phase.value == "finished"
    assert len(json.dumps(e.to_payload())) < 1_000_000
    assert len(json.dumps(e.host_view(full=False))) < 50 * 1024


def test_the_engine_payload_of_a_fourteen_day_four_player_game_is_small():
    config = cfg(max_players=4, duration_days=14)
    e = PulkyEngine.start(config, seats(4), 5)
    for _ in range(14):
        day = e.day
        for alias in e.aliases:
            e.submit(alias, choose(e, alias, random.Random(f"5:{day}:{alias}")))
        e.resolve_day()
    assert len(json.dumps(e.to_payload())) < 200 * 1024
