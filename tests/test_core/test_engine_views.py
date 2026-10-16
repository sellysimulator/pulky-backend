"""Black-box tests for section 11: player_view, host_view, standings and results."""

import copy
import json

import pytest

from app.core.calendar import order_windows
from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.demand import market_lambda, shares
from app.core.engine import PulkyEngine
from app.core.enums import Incoterm, Phase, Weekday
from app.core.insurance import plan_rows
from app.core.kernels import weekday_multiplier
from app.core.lots import Cellar, Lot, quality_guide
from app.core.money import round_cents
from app.core.records import (
    Decision,
    FinalSettlement,
    InvalidDecision,
    PeerBuy,
    PeerSell,
)
from app.core.stats import build_stats

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

PLAYER_KEYS = {
    "alias",
    "display_name",
    "day",
    "duration_days",
    "weekday",
    "phase",
    "has_submitted",
    "awaiting",
    "brief",
    "yesterday",
    "cellar",
    "stock_by_age",
    "quality_guide",
    "expected_spoil_tonight",
    "in_transit",
    "futures_book",
    "backlog",
    "expected_demand_own",
    "reputation",
    "market_share",
    "cash",
    "cumulative_profit",
    "insurance_plans",
    "order_windows",
    "prefill_decision",
    "own_history",
    "public_history",
    "final_settlement",
}
HISTORY_KEYS = {"own_history", "public_history"}
CELLAR_KEYS = {
    "id",
    "qty",
    "age",
    "unit_cost",
    "initial_quality",
    "conserved",
    "quality",
    "last_day",
    "risk_tonight",
    "expected_loss_tonight",
}
YESTERDAY_KEYS = {
    "day",
    "demand",
    "sold",
    "backlog_end",
    "backlog_change",
    "spoiled",
    "revenue_salvage",
    "reputation",
    "reputation_change",
    "profit",
    "cash_end",
    "futures_bought",
    "futures_filled",
    "futures_vs_spot",
    "futures_unfilled",
    "refund_futures",
    "refrigeration_failure",
    "lost_transport",
    "refund_insurance",
    "market_share",
    "shortfall_spot",
    "peer_bought",
    "cost_peer",
    "peer_sold",
    "revenue_peer",
}
HOST_BASE = {
    "day",
    "duration_days",
    "weekday",
    "phase",
    "brief",
    "seed",
    "config",
    "awaiting",
    "players",
    "leaderboard",
}
HOST_PLAYER_BASE = {
    "display_name",
    "has_submitted",
    "stock_units",
    "expected_spoil_tonight",
    "in_transit_units",
    "open_futures_units",
    "backlog",
    "reputation",
    "market_share",
    "cumulative_profit",
    "cash",
    "fill_rate",
    "waste_rate",
    "last_incoterm",
    "last_peer_bought",
    "last_peer_sold",
}
SECRET_LEAVES = {
    "random_seed",
    "capacity_sd_per_player",
    "disruption_probability",
    "failure_probability",
    "loss_fraction",
    "target_margin",
    "reference_shipment",
}


def walk(node):
    """Yield every (key, value) pair and every list item of a JSON-like tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from walk(value)
    elif isinstance(node, list | tuple):
        for item in node:
            yield None, item
            yield from walk(item)


def has_key(tree, names):
    return any(key in names for key, _ in walk(tree))


def has_value(tree, wanted, tol=1e-9):
    for _, value in walk(tree):
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float) and abs(value - wanted) <= tol:
            return True
        if isinstance(value, str) and str(wanted) in value:
            return True
    return False


def view_lots(e, alias):
    return {x["id"]: x for x in e.player_view(alias)["cellar"]}


# ---------------------------------------------------------------- player view


def test_player_view_day_one_has_every_key_with_the_opening_values():
    config = quiet(2)
    e = start(config)
    for alias in e.aliases:
        v = e.player_view(alias)
        assert set(v) == PLAYER_KEYS
        assert v["alias"] == alias and v["display_name"] == e.display_names[alias]
        assert (v["day"], v["duration_days"], v["weekday"]) == (1, 14, "mon")
        assert v["phase"] == "deciding"
        assert v["has_submitted"] is False and v["awaiting"] == ["P1", "P2"]
        assert v["brief"] == e.brief()
        assert v["yesterday"] is None
        assert v["backlog"] == 0 and v["market_share"] == 0
        assert v["cash"] == 5000 and v["cumulative_profit"] == 0
        assert v["reputation"] == 0.5
        assert v["own_history"] == [] and v["public_history"] == []
        assert v["final_settlement"] is None
        assert v["prefill_decision"] == Decision.zero(1).to_payload()
        assert v["futures_book"] == []
        assert v["in_transit"] == []
        json.dumps(v)  # JSON-safe


def test_player_view_cellar_entries_match_the_cellar_view():
    config = quiet(2, supplier__quality_max=90, supplier__quality_min=70)
    e = start(config)
    v = e.player_view("P1")
    lot = Lot("L0", 30, 0, 12.0, 90, 0, 3, False)
    assert v["cellar"] == Cellar([lot]).view(1)
    assert set(v["cellar"][0]) == CELLAR_KEYS
    assert v["stock_by_age"] == Cellar([lot]).stock_by_age()
    assert v["expected_spoil_tonight"] == Cellar([lot]).expected_spoil_tonight()


def test_section_3_5_example_qualities_and_last_days_through_the_engine():
    config = quiet(4, stock__initial_stock=0, demand__per_player_peak=1)
    e = start(config, seed=2)
    for _ in range(4):
        play(e)
    payload = copy.deepcopy(e.to_payload())
    payload["players"]["P3"]["cellar"] = [
        Lot("L3", 12, 2, 12.0, 90, 0, 3, False).to_payload(),
        Lot("L4", 10, 1, 12.0, 95, 0, 3, False).to_payload(),
        Lot("L5", 20, 0, 12.0, 85, 0, 3, False).to_payload(),
    ]
    e = PulkyEngine.from_payload(payload, config)
    cellar = e.player_view("P3")["cellar"]
    assert [x["id"] for x in cellar] == ["L3", "L4", "L5"]
    assert [x["quality"] for x in cellar] == [0, 28, 52]
    assert [x["last_day"] for x in cellar] == [5, 6, 7]
    for x in cellar:
        assert x["risk_tonight"] == pytest.approx(1 - x["quality"] / 100, abs=0.006)
        assert x["expected_loss_tonight"] == pytest.approx(
            x["qty"] * x["risk_tonight"], abs=0.01
        )
    guide = e.player_view("P3")["quality_guide"]
    assert guide == quality_guide(5, 0, 3)
    by_quality = {row["quality"]: row for row in guide}
    assert (by_quality[90]["half_spoiled_day"], by_quality[90]["last_day"]) == (7, 8)
    assert (by_quality[50]["half_spoiled_day"], by_quality[50]["last_day"]) == (5, 7)
    assert by_quality[0]["last_day"] == 5


def test_quality_guide_is_empty_when_the_peer_market_is_off():
    on = start(quiet(2)).player_view("P1")
    off = start(quiet(2, peer_market__enabled=False)).player_view("P1")
    assert on["quality_guide"] == quality_guide(1, 0, 3)
    assert len(on["quality_guide"]) == 21
    assert off["quality_guide"] == []


def test_insurance_plans_come_from_the_tier_terms_and_todays_spot_price():
    config = quiet(2, insurance__tiers__ddp__enabled=False)
    e = start(config)
    expected = plan_rows(
        config.tier_terms(),
        today_spot_price=e.brief()["supplier"]["spot_price"],
        retail_price=30.0,
    )
    assert e.player_view("P1")["insurance_plans"] == expected
    assert [row["enabled"] for row in expected].count(False) >= 1


def test_order_windows_in_the_view_follow_the_calendar():
    config = quiet(2, duration_days=7, futures__horizon_days=3)
    e = start(config)
    for _ in range(6):
        play(e)
    assert e.day == 7
    w = order_windows(7, 7, 1, 3, 12.0)
    assert e.player_view("P1")["order_windows"] == {
        "spot_open": False,
        "futures_open": False,
        "futures_days_left_to_buy": 0,
        "futures_message": w.futures_message,
        "spot_message": w.spot_message,
    }
    assert w.spot_message == "Spot orders closed: they would arrive after the game ends"


def test_in_transit_lists_shipments_front_first_and_skips_empty_ones():
    config = quiet(2, supplier__lead_time_days=3, demand__per_player_peak=1)
    e = start(config)
    play(e, {"P1": D(1, 10)})
    play(e)  # an empty shipment is pushed on day 2
    play(e, {"P1": D(3, 6, 0, CIP)})
    transit = e.player_view("P1")["in_transit"]
    assert transit == [
        {"shipped_day": 1, "arrives_day": 4, "qty": 10, "lost": 0, "incoterm": "exw"},
        {"shipped_day": 3, "arrives_day": 6, "qty": 6, "lost": 0, "incoterm": "cip"},
    ]
    assert e.host_view()["players"]["P1"]["in_transit_units"] == 16


def test_in_transit_keeps_a_shipment_that_lost_everything():
    config = quiet(2, transport__failure_probability=1.0, transport__loss_fraction=1.0)
    e = start(config)
    play(e, {"P1": D(1, 10)})
    assert e.player_view("P1")["in_transit"] == [
        {"shipped_day": 1, "arrives_day": 2, "qty": 0, "lost": 10, "incoterm": "exw"}
    ]


def test_expected_demand_own_is_lambda_times_the_reputation_share():
    config = quiet(3, demand__per_player_peak=33.3)
    e = start(config, seed=5)
    play(e, {"P1": D(1, 30), "P2": D(1, 5)})
    play(e, {"P1": D(2, 30), "P2": D(2, 5)})
    reps = {a: e.player_view(a)["reputation"] for a in e.aliases}
    mult = weekday_multiplier(Weekday.WED, Weekday.THU, Weekday.SAT)
    lam = market_lambda(33.3, 3, mult)
    split = shares(reps)
    assert len(set(round(r, 6) for r in reps.values())) > 1
    for alias in e.aliases:
        assert e.player_view(alias)["expected_demand_own"] == round(
            lam * split[alias], 2
        )


def test_prefill_is_the_zero_decision_on_day_one_and_the_last_decision_after():
    config = quiet(2, duration_days=7, futures__horizon_days=3)
    e = start(config)
    assert e.player_view("P1")["prefill_decision"] == {
        "day": 1,
        "order_spot": 0,
        "order_futures": 0,
        "incoterm": "exw",
        "conserve_lots": [],
        "cooperate": False,
        "peer_buy": None,
        "peer_sell": None,
    }
    buy, sell = PeerBuy(12.5, 40), PeerSell(14.0, 90, 7)
    play(e, {"P1": D(1, 7, 5, CIP, ("L0",), True, buy, sell)})
    assert e.player_view("P1")["prefill_decision"] == {
        "day": 2,
        "order_spot": 7,
        "order_futures": 5,
        "incoterm": "cip",
        "conserve_lots": [],
        "cooperate": True,
        "peer_buy": buy.to_payload(),
        "peer_sell": sell.to_payload(),
    }
    # a player who played the zero decision gets the zero decision, with today's day
    assert e.player_view("P2")["prefill_decision"] == Decision.zero(2).to_payload()


def test_prefill_ignores_a_rejected_decision():
    e = start(quiet(2, starting_cash=500))
    play(e, {"P1": D(1, 20, 0, CIP)})
    with pytest.raises(InvalidDecision):
        e.submit("P1", D(2, 4000))
    assert e.player_view("P1")["prefill_decision"]["order_spot"] == 20


def test_prefill_zeroes_orders_that_have_closed():
    config = quiet(2, duration_days=7, futures__horizon_days=3)
    e = start(config)
    for day in range(1, 5):
        play(e, {"P1": D(day, 2, 1, DDP, buy=PeerBuy(9.0, 10))})
    assert e.day == 5  # futures closed from day 5, spot still open
    pre = e.player_view("P1")["prefill_decision"]
    assert pre["day"] == 5 and pre["order_futures"] == 0 and pre["order_spot"] == 2
    assert pre["incoterm"] == "ddp" and pre["peer_buy"] == {
        "max_price": 9.0,
        "min_quality": 10,
    }
    play(e, {"P1": D(5, 2, 0, DDP)})
    play(e, {"P1": D(6, 2, 0, DDP)})
    assert e.day == 7  # spot closed too
    pre = e.player_view("P1")["prefill_decision"]
    assert pre["order_spot"] == 0 and pre["order_futures"] == 0


def test_yesterday_is_built_from_the_last_record():
    config = quiet(2, futures__horizon_days=3, transport__failure_probability=1.0)
    e = start(config, seed=7)
    for day in (1, 2):
        out = play(e, {"P1": D(day, 25, 4, CIP, (), True), "P2": D(day, 3)})
        r = out.records["P1"]
        y = e.player_view("P1")["yesterday"]
        assert set(y) == YESTERDAY_KEYS
        assert y["day"] == day
        for key in (
            "demand",
            "sold",
            "backlog_end",
            "spoiled",
            "revenue_salvage",
            "profit",
            "cash_end",
            "futures_filled",
            "futures_vs_spot",
            "futures_unfilled",
            "refund_futures",
            "refrigeration_failure",
            "lost_transport",
            "refund_insurance",
            "market_share",
            "shortfall_spot",
            "peer_bought",
            "cost_peer",
            "peer_sold",
            "revenue_peer",
        ):
            assert y[key] == getattr(r, key), key
        assert y["futures_bought"] == r.order_futures == 4
        assert y["backlog_change"] == r.backlog_end - r.backlog_open
        assert y["reputation"] == r.reputation_end
        assert y["reputation_change"] == pytest.approx(
            r.reputation_end - r.reputation_open
        )
        json.dumps(y)
    assert y["refrigeration_failure"] is True and y["lost_transport"] > 0


def test_cash_profit_and_market_share_track_the_last_record():
    e = start(quiet(2), seed=3)
    out = play(e, {"P1": D(1, 20), "P2": D(1, 20)})
    out = play(e, {"P1": D(2, 20), "P2": D(2, 5)})
    for alias in e.aliases:
        v = e.player_view(alias)
        recs = e.records(alias)
        assert v["cash"] == recs[-1].cash_end
        assert v["cumulative_profit"] == round_cents(sum(r.profit for r in recs))
        assert v["market_share"] == out.records[alias].market_share
        assert v["reputation"] == out.records[alias].reputation_end
        assert v["backlog"] == out.records[alias].backlog_end


def test_history_is_full_form_only_and_everything_else_is_identical():
    e = start(quiet(2), seed=3)
    play(e, {"P1": D(1, 10)})
    play(e, {"P1": D(2, 10)})
    for alias in e.aliases:
        full = e.player_view(alias)
        daily = e.player_view(alias, full=False)
        assert e.player_view(alias, True) == full
        assert HISTORY_KEYS.isdisjoint(daily)
        assert set(full) - set(daily) == HISTORY_KEYS
        assert {k: v for k, v in full.items() if k not in HISTORY_KEYS} == daily
        assert full["own_history"] == [r.to_payload() for r in e.records(alias)]
        assert full["public_history"] == [e.public_results(d) for d in (1, 2)]


def test_daily_forms_do_not_grow_the_view_with_the_game():
    e = start(quiet(2), seed=3)
    play(e)
    small = len(json.dumps(e.player_view("P1", full=False)))
    for _ in range(10):
        play(e)
    big = len(json.dumps(e.player_view("P1", full=False)))
    assert big < small * 1.6
    assert len(json.dumps(e.player_view("P1"))) > 3 * big


def test_a_finished_game_has_nulls_for_brief_windows_and_prefill():
    e = start(quiet(2))
    play(e, {"P1": D(1, 10)})
    e.end_early()
    for alias in e.aliases:
        v = e.player_view(alias)
        assert v["phase"] == "finished" and e.phase == Phase.FINISHED
        assert v["brief"] is None
        assert v["order_windows"] is None
        assert v["prefill_decision"] is None
        assert v["has_submitted"] is False and v["awaiting"] == []
        assert v["final_settlement"] == e.settlements()[alias].to_payload()
        assert set(v) == PLAYER_KEYS
    assert e.brief() is None
    assert e.awaiting() == [] and e.has_submitted("P1") is False


# ------------------------------------------------------------------ host view


def test_host_view_day_one_keys_and_values():
    config = quiet(2)
    e = start(config, seed=11)
    v = e.host_view()
    assert set(v) == HOST_BASE | {"public_history", "market_history"}
    assert (v["day"], v["duration_days"], v["weekday"]) == (1, 14, "mon")
    assert v["phase"] == "deciding" and v["seed"] == 11
    assert v["config"] == config.to_dict()
    assert v["brief"] == e.brief() and v["awaiting"] == ["P1", "P2"]
    assert v["public_history"] == [] and v["market_history"] == []
    assert v["leaderboard"] == e.standings()["leaderboard"]
    for alias, p in v["players"].items():
        assert set(p) == HOST_PLAYER_BASE | {"own_history"}
        assert p["display_name"] == e.display_names[alias]
        assert p["has_submitted"] is False
        assert p["stock_units"] == 30 and p["in_transit_units"] == 0
        assert p["open_futures_units"] == 0 and p["backlog"] == 0
        assert (
            p["expected_spoil_tonight"]
            == Cellar(
                [Lot("L0", 30, 0, 12.0, 100, 0, 3, False)]
            ).expected_spoil_tonight()
        )
        assert p["reputation"] == 0.5 and p["market_share"] == 0
        assert p["cumulative_profit"] == 0 and p["cash"] == 5000
        assert p["fill_rate"] is None and p["waste_rate"] is None
        assert p["last_incoterm"] is None
        assert p["last_peer_bought"] is None and p["last_peer_sold"] is None
        assert p["own_history"] == []
    json.dumps(v)
    assert "state" not in v
    assert all("connected" not in p for p in v["players"].values())


def test_host_view_values_after_some_days():
    config = quiet(2, futures__horizon_days=3, demand__per_player_peak=30)
    e = start(config, seed=2)
    play(e, {"P1": D(1, 25, 6, CIP), "P2": D(1, 4)})
    play(e, {"P1": D(2, 20, 0, DDP), "P2": D(2, 4)})
    e.submit("P2", D(3, 1))
    v = e.host_view()
    assert v["players"]["P2"]["has_submitted"] is True
    assert v["players"]["P1"]["has_submitted"] is False and v["awaiting"] == ["P1"]
    for alias, p in v["players"].items():
        recs = e.records(alias)
        view = e.player_view(alias)
        assert p["stock_units"] == sum(x["qty"] for x in view["cellar"])
        assert p["in_transit_units"] == sum(x["qty"] for x in view["in_transit"])
        assert p["open_futures_units"] == sum(x["qty"] for x in view["futures_book"])
        assert p["expected_spoil_tonight"] == view["expected_spoil_tonight"]
        assert p["backlog"] == view["backlog"] and p["cash"] == view["cash"]
        assert p["cumulative_profit"] == view["cumulative_profit"]
        assert p["reputation"] == view["reputation"]
        assert p["market_share"] == view["market_share"]
        demand = sum(r.demand for r in recs)
        sold = sum(r.sold for r in recs)
        spoiled = sum(r.spoiled for r in recs)
        assert p["fill_rate"] == (sold / demand if demand else None)
        assert p["waste_rate"] == (
            spoiled / (sold + spoiled) if sold + spoiled else None
        )
        assert p["last_incoterm"] == recs[-1].incoterm.value
        assert p["last_peer_bought"] == recs[-1].peer_bought
        assert p["last_peer_sold"] == recs[-1].peer_sold
        assert p["own_history"] == [r.to_payload() for r in recs]
    assert v["players"]["P1"]["last_incoterm"] == "ddp"
    assert v["players"]["P1"]["open_futures_units"] == 6
    assert v["public_history"] == [e.public_results(1), e.public_results(2)]
    assert v["market_history"] == [m.to_payload() for m in e.market_history()]
    assert v["leaderboard"] == e.standings()["leaderboard"]


def test_host_daily_form_swaps_histories_for_the_last_entries():
    e = start(quiet(2), seed=2)
    assert e.host_view(full=False)["players"]["P1"]["last_record"] is None
    assert e.host_view(full=False)["last_market_day"] is None
    play(e, {"P1": D(1, 5)})
    play(e, {"P1": D(2, 5)})
    full = e.host_view()
    daily = e.host_view(full=False)
    assert set(daily) == HOST_BASE | {"last_market_day"}
    assert not {"public_history", "market_history"} & set(daily)
    assert daily["last_market_day"] == e.market_history()[-1].to_payload()
    for alias, p in daily["players"].items():
        assert set(p) == HOST_PLAYER_BASE | {"last_record"}
        assert p["last_record"] == e.records(alias)[-1].to_payload()
        rest = {k: v for k, v in full["players"][alias].items() if k != "own_history"}
        assert {k: v for k, v in p.items() if k != "last_record"} == rest
    top = ("last_market_day", "players", "public_history", "market_history")
    assert {k: v for k, v in daily.items() if k not in top} == {
        k: v for k, v in full.items() if k not in top
    }


# -------------------------------------------------------------------- secrecy


def test_a_pending_decision_never_reaches_the_host_or_a_rival():
    # spec 11 failure mode 5
    e = start(quiet(2, starting_cash=100000), seed=11)
    decision = D(
        1,
        4321,
        3210,
        CIP,
        buy=PeerBuy(77.77, 3),
        sell=PeerSell(66.66, 4, 6543),
    )
    e.submit("P1", decision)
    secrets = (4321, 3210, 6543, 77.77, 66.66)
    for tree in (
        e.host_view(),
        e.host_view(full=False),
        e.player_view("P2"),
        e.player_view("P2", full=False),
        e.brief(),
        e.standings(),
    ):
        for wanted in secrets:
            assert not has_value(tree, wanted), wanted
    # control: once resolved, the host (and only the owner) sees the order
    e.submit("P2", D(1))
    e.resolve_day()
    assert has_value(e.host_view(), 4321)
    assert has_value(e.player_view("P1"), 4321)
    for wanted in secrets:
        assert not has_value(e.player_view("P2"), wanted), wanted
    assert not has_value([p.to_payload() for p in e.public_history()], 4321)
    assert not has_value([m.to_payload() for m in e.market_history()], 4321)


def test_secret_config_keys_and_values_never_reach_a_player():
    # spec 14 / 11.1: no key, no value and no spot range in any player view
    seed = 4242424242
    config = quiet(
        2,
        transport__failure_probability=0.137,
        supplier__spot_spread=0.337,
        supplier__capacity_sd_per_player=3.717,
        supplier__disruption_probability=0.0731,
        transport__loss_fraction=0.2113,
        insurance__target_margin=0.1357,
        insurance__reference_shipment=23,
    )
    e = PulkyEngine.start(config, seats(2), seed)
    base = config.supplier.base_price
    forbidden_values = (
        0.137,
        0.0731,
        0.2113,
        0.1357,
        3.717,
        seed,
        base * (1 - 0.337),
        base * (1 + 0.337),
    )
    views = []
    for day in range(1, 5):
        for alias in e.aliases:
            views.append(e.player_view(alias))
            views.append(e.player_view(alias, full=False))
        play(e, {"P1": D(day, 6, 0, CIP), "P2": D(day, 2)})
    for alias in e.aliases:
        views.append(e.player_view(alias))
    for view in views:
        assert not has_key(view, SECRET_LEAVES)
        assert "config" not in view
        for wanted in forbidden_values:
            assert not has_value(view, wanted, tol=0.0051), wanted
    # control: the host view carries all of it
    host = e.host_view()
    assert has_key(host, SECRET_LEAVES - {"random_seed"})
    assert has_value(host, 0.137) and has_value(host, seed)


def test_cellar_entries_never_carry_shelf_life_fields():
    e = start(quiet(2, demand__per_player_peak=1))
    play(e, {"P1": D(1, 10, conserve=("L0",))})
    for alias in e.aliases:
        for entry in e.player_view(alias)["cellar"]:
            assert set(entry) == CELLAR_KEYS
        assert not has_key(e.player_view(alias), {"fresh_days", "max_days"})


# ------------------------------------------------------ standings and results


def test_standings_is_build_stats_over_the_records_so_far():
    config = quiet(2)
    e = start(config, seed=9)
    play(e, {"P1": D(1, 10), "P2": D(1, 3)})
    play(e, {"P1": D(2, 10), "P2": D(2, 3)})
    zero = FinalSettlement(0, 0.0, 0.0, 0, 0.0, 0.0)
    expected = build_stats(
        records={a: e.records(a) for a in e.aliases},
        market=e.market_history(),
        settlements={a: zero for a in e.aliases},
        display_names=e.display_names,
        starting_reputation=0.5,
        seed=9,
    )
    assert e.standings() == expected
    board = e.standings()["leaderboard"]
    assert [row["rank"] for row in board] == [1, 2]
    assert {row["alias"] for row in board} == {"P1", "P2"}


def test_results_are_available_only_once_the_game_is_finished():
    e = start(quiet(2, duration_days=7, futures__horizon_days=3), seed=9)
    with pytest.raises(ValueError):
        e.results()
    for _ in range(7):
        play(e)
    assert e.phase == Phase.FINISHED
    res = e.results()
    assert set(res) == {"days_played", "ended_early", "leaderboard", "stats"}
    assert res["days_played"] == 7 and res["ended_early"] is False
    standings = e.standings()
    assert res["leaderboard"] == standings["leaderboard"]
    assert res["stats"] == {
        "players": standings["players"],
        "market": standings["market"],
    }
    settled = e.settlements()
    expected = build_stats(
        records={a: e.records(a) for a in e.aliases},
        market=e.market_history(),
        settlements=settled,
        display_names=e.display_names,
        starting_reputation=0.5,
        seed=9,
    )
    assert standings == expected
    json.dumps(res)


def test_public_results_carry_display_names_and_only_for_resolved_days():
    e = start(quiet(2), seed=9)
    with pytest.raises(ValueError):
        e.public_results(1)
    out = play(e)
    payload = e.public_results(1)
    assert payload == out.public.to_payload(e.display_names)
    assert payload["players"]["P2"]["display_name"] == NAMES[1]
    assert set(payload["players"]["P1"]) == {
        "display_name",
        "units_sold",
        "market_share",
        "reputation",
    }
    for bad in (0, 2, 3):
        with pytest.raises(ValueError):
            e.public_results(bad)


def test_demand_share_example_of_the_spec():
    # spec 7.3: Friday, 4 players, lambda 100; reputation 0.8 against three at 0.5
    config = quiet(4, stock__initial_stock=0)
    e = start(config, seed=2)
    for _ in range(4):
        play(e)
    payload = copy.deepcopy(e.to_payload())
    for alias in e.aliases:
        payload["players"][alias]["reputation"] = 0.8 if alias == "P1" else 0.5
    e = PulkyEngine.from_payload(payload, config)
    assert e.brief()["expected_demand"] == 100.0
    assert e.player_view("P1")["expected_demand_own"] == 30.23
    for alias in ("P2", "P3", "P4"):
        assert e.player_view(alias)["expected_demand_own"] == 23.26
    assert e.brief()["reputation"]["P1"] == pytest.approx(0.8, abs=0.005)
