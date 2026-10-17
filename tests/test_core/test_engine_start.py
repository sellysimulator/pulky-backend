"""Black-box tests for section 11: ``PulkyEngine.start`` and ``brief`` (spec 4.2)."""

import copy

import pytest

from app.core.calendar import order_windows, weekday_of
from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.demand import market_lambda
from app.core.engine import PulkyEngine
from app.core.enums import IncotermTier, Phase, Weekday
from app.core.kernels import weekday_multiplier, weekday_multipliers
from app.core.lots import Cellar, Lot
from app.core.money import round_cents
from app.core.records import Decision
from app.core.sampling import halfup
from app.core.supply import AbstractSupplier

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


def supplier_for(config, n, seed):
    s = config.supplier
    return AbstractSupplier(
        seed=seed,
        n_players=n,
        capacity_mean_per_player=s.capacity_mean_per_player,
        capacity_sd_per_player=s.capacity_sd_per_player,
        disruption_probability=s.disruption_probability,
        disruption_factor=s.disruption_factor,
        base_price=s.base_price,
        spot_spread=s.spot_spread,
    )


def test_start_builds_the_opening_state():
    config = quiet(2)
    e = start(config, seed=11)
    assert e.config is config or e.config == config
    assert e.seed == 11
    assert e.day == 1
    assert e.phase == Phase.DECIDING
    assert e.ended_early is False
    assert e.aliases == ("P1", "P2")
    assert e.display_names == {"P1": NAMES[0], "P2": NAMES[1]}
    assert e.awaiting() == ["P1", "P2"]
    assert e.all_in() is False
    assert e.settlements() == {}
    assert e.public_history() == []
    assert e.market_history() == []
    assert e.records("P1") == []


def test_aliases_follow_alias_numbers_not_text_order():
    names = seats(10)
    shuffled = dict(reversed(list(names.items())))
    e = PulkyEngine.start(quiet(10), shuffled, 3)
    assert e.aliases == tuple(f"P{i}" for i in range(1, 11))
    assert e.awaiting() == list(e.aliases)
    assert set(e.brief()["reputation"]) == set(e.aliases)


def test_starting_cash_is_applied_to_every_player():
    # spec 14 #6: the starting-cash setting must not be ignored
    e = start(quiet(2, starting_cash=1234))
    assert e.player_view("P1")["cash"] == 1234
    assert e.player_view("P2")["cash"] == 1234
    out = play(e)
    assert out.records["P1"].cash_open == 1234
    assert out.records["P2"].cash_open == 1234


def test_starting_reputation_is_applied():
    e = start(quiet(3, reputation__starting=0.37))
    for alias in e.aliases:
        assert e.player_view(alias)["reputation"] == pytest.approx(0.37)
    assert all(
        v == pytest.approx(0.37, abs=0.005) for v in e.brief()["reputation"].values()
    )


def test_initial_stock_is_lot_l0_at_quality_max():
    config = quiet(
        2,
        stock__initial_stock=25,
        supplier__quality_min=70,
        supplier__quality_max=90,
        stock__fresh_days=2,
        stock__max_days=5,
    )
    e = start(config)
    lot = Lot("L0", 25, 0, 12.0, 90, 2, 5, False)
    for alias in e.aliases:
        view = e.player_view(alias)
        assert view["cellar"] == Cellar([lot]).view(1)
        assert view["stock_by_age"] == Cellar([lot]).stock_by_age()
        assert view["cellar"][0]["initial_quality"] == 90
        assert view["cellar"][0]["unit_cost"] == 12.0
        assert view["cellar"][0]["conserved"] is False
        assert "fresh_days" not in view["cellar"][0]
        assert "max_days" not in view["cellar"][0]


def test_no_initial_stock_means_no_l0():
    e = start(quiet(2, stock__initial_stock=0))
    view = e.player_view("P1")
    assert view["cellar"] == []
    assert view["stock_by_age"] == []
    assert e.host_view()["players"]["P1"]["stock_units"] == 0


def test_initial_pipeline_is_l_slots_front_first():
    e = start(quiet(2, supplier__lead_time_days=3, supplier__initial_pipeline_qty=5))
    expected = [
        {
            "shipped_day": k - 3,
            "arrives_day": k,
            "qty": 5,
            "lost": 0,
            "incoterm_tier": "exw",
        }
        for k in (1, 2, 3)
    ]
    assert e.player_view("P1")["in_transit"] == expected
    assert e.host_view()["players"]["P1"]["in_transit_units"] == 15


def test_empty_initial_pipeline_shows_nothing_in_transit():
    e = start(quiet(2, supplier__lead_time_days=3, supplier__initial_pipeline_qty=0))
    assert e.player_view("P1")["in_transit"] == []
    assert e.host_view()["players"]["P1"]["in_transit_units"] == 0


def test_initial_pipeline_arrives_free_at_quality_max():
    config = quiet(
        2,
        supplier__lead_time_days=2,
        supplier__initial_pipeline_qty=5,
        supplier__quality_min=50,
        supplier__quality_max=90,
        demand__per_player_peak=1,
    )
    e = start(config)
    out1 = play(e)
    r = out1.records["P1"]
    assert r.delivered == 5
    lots = {x["id"]: x for x in e.player_view("P1")["cellar"]}
    assert lots["L1"]["initial_quality"] == 90
    assert lots["L1"]["unit_cost"] == 12.0
    assert lots["L1"]["age"] == 1
    out2 = play(e)
    assert out2.records["P1"].delivered == 5
    lots = {x["id"]: x for x in e.player_view("P1")["cellar"]}
    assert lots["L2"]["initial_quality"] == 90
    assert lots["L0"]["initial_quality"] == 90
    out3 = play(e)
    assert out3.records["P1"].delivered == 0
    # AC 16: nothing but sales, salvage and holding/backlog costs touches cash
    for out in (out1, out2, out3):
        for rec in out.records.values():
            for line in (
                rec.cost_spot,
                rec.cost_futures,
                rec.cost_peer,
                rec.cost_transport,
                rec.cost_insurance,
                rec.cost_conservation,
                rec.refund_insurance,
                rec.refund_futures,
                rec.revenue_peer,
            ):
                assert line == 0.0
            expected = round_cents(
                30 * rec.sold
                + 2 * rec.spoiled
                - 0.2 * rec.closing_stock
                - 1.5 * rec.backlog_end
            )
            assert rec.profit == expected


def test_day_one_supplier_draw_is_made_at_start():
    config = cfg(max_players=4)
    e = start(config, seed=11)
    draw = supplier_for(config, 4, 11).draw_day(1)
    supplier = e.brief()["supplier"]
    assert supplier["capacity"] == draw.capacity
    assert supplier["disrupted"] == draw.disrupted
    assert supplier["spot_price"] == draw.spot_price


def test_brief_has_the_spec_shape_on_day_five_with_defaults():
    config = cfg(max_players=4)
    e = start(config, seed=11)
    for _ in range(4):
        play(e)
    assert e.day == 5
    brief = e.brief()
    assert set(brief) == {
        "day",
        "duration_days",
        "weekday",
        "weekday_multiplier",
        "weekday_multipliers",
        "expected_demand",
        "supplier",
        "spot",
        "futures",
        "transport",
        "reputation",
    }
    draw = supplier_for(config, 4, 11).draw_day(5)
    assert brief["day"] == 5
    assert brief["duration_days"] == 14
    assert brief["weekday"] == "fri"
    assert brief["weekday_multiplier"] == 1.0
    assert brief["weekday_multipliers"] == {
        "mon": 0.25,
        "tue": 0.5,
        "wed": 0.75,
        "thu": 1.0,
        "fri": 1.0,
        "sat": 1.0,
        "sun": 0.75,
    }
    assert brief["expected_demand"] == 100.0
    message = (
        None
        if not draw.disrupted
        else f"Day 5 (Fri): supplier capacity cut to {halfup(0.4 * 100)}%"
    )
    assert brief["supplier"] == {
        "capacity": draw.capacity,
        "disrupted": draw.disrupted,
        "disruption_message": message,
        "spot_price": draw.spot_price,
        "base_price": 12.0,
        "lead_time_days": 1,
    }
    assert brief["spot"] == {"open": True, "arrives_day": 6}
    assert brief["futures"] == {
        "open": True,
        "price": 12.0,
        "horizon_days": 7,
        "delivery_day": 12,
        "last_day_to_buy": 7,
        "days_left_to_buy": 3,
    }
    assert brief["transport"] == {"shared": False}
    assert set(brief["reputation"]) == {"P1", "P2", "P3", "P4"}
    for alias, value in brief["reputation"].items():
        live = e.player_view(alias)["reputation"]
        assert value == pytest.approx(live, abs=0.005)
        assert value != pytest.approx(0.5, abs=0.001)


def test_disruption_message_names_the_day_and_the_cut_factor():
    config = quiet(
        2, supplier__disruption_probability=1, supplier__disruption_factor=0.125
    )
    e = start(config)
    brief = e.brief()
    assert brief["supplier"]["disrupted"] is True
    # halfup(0.125 * 100) = 13, not the banker's-rounded 12
    assert brief["supplier"]["disruption_message"] == (
        "Day 1 (Mon): supplier capacity cut to 13%"
    )
    draw = supplier_for(config, 2, 11).draw_day(1)
    assert brief["supplier"]["capacity"] == draw.capacity
    for _ in range(4):
        play(e)
    assert e.brief()["supplier"]["disruption_message"] == (
        "Day 5 (Fri): supplier capacity cut to 13%"
    )


def test_no_disruption_means_a_null_message():
    e = start(quiet(2))
    assert e.brief()["supplier"]["disrupted"] is False
    assert e.brief()["supplier"]["disruption_message"] is None


def test_expected_demand_is_market_lambda_rounded_to_two_decimals():
    config = quiet(3, demand__per_player_peak=10.123)
    e = start(config)
    lam = market_lambda(
        10.123, 3, weekday_multiplier(Weekday.MON, Weekday.THU, Weekday.SAT)
    )
    assert e.brief()["expected_demand"] == round(lam, 2)
    assert e.brief()["expected_demand"] != lam
    assert e.brief()["weekday_multiplier"] == 0.25
    assert e.brief()["weekday_multipliers"] == {
        w.value: m for w, m in weekday_multipliers(Weekday.THU, Weekday.SAT).items()
    }


def test_brief_follows_the_peak_window_and_transport_switch():
    config = quiet(
        2,
        demand__weekday_peak__from="tue",
        demand__weekday_peak__to="tue",
        transport__shared=True,
    )
    e = start(config)
    brief = e.brief()
    assert brief["transport"] == {"shared": True}
    assert brief["weekday_multiplier"] == pytest.approx(
        weekday_multiplier(Weekday.MON, Weekday.TUE, Weekday.TUE)
    )
    play(e)
    assert e.brief()["weekday"] == "tue"
    assert e.brief()["weekday_multiplier"] == 1.0
    assert weekday_of(2) == Weekday.TUE


def test_ordering_windows_in_the_brief_and_the_view():
    config = quiet(2, duration_days=7, futures__horizon_days=3)
    e = start(config)
    for day in range(1, 8):
        assert e.day == day
        windows = order_windows(day, 7, 1, 3, 12.0)
        brief = e.brief()
        assert brief["spot"]["open"] is windows.spot_open
        assert brief["spot"]["arrives_day"] == windows.spot_arrives_day
        assert brief["futures"]["open"] is windows.futures_open
        assert brief["futures"]["delivery_day"] == windows.futures_delivery_day
        assert brief["futures"]["last_day_to_buy"] == windows.last_day_to_buy
        assert brief["futures"]["days_left_to_buy"] == windows.days_left_to_buy
        assert e.player_view("P1")["order_windows"] == {
            "spot_open": windows.spot_open,
            "futures_open": windows.futures_open,
            "futures_days_left_to_buy": windows.days_left_to_buy,
            "futures_message": windows.futures_message,
            "spot_message": windows.spot_message,
        }
        play(e)
    # day 7: spot would arrive on day 8, after the game ends
    assert order_windows(7, 7, 1, 3, 12.0).spot_open is False
