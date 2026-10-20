"""Black-box tests for section 11: submit, outlay and the decision checks."""

import copy

import pytest

from app.core.calendar import order_windows
from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.engine import PulkyEngine
from app.core.enums import IncotermTier, Phase
from app.core.limits import MAX_ORDER, MAX_PEER_PRICE
from app.core.money import round_cents
from app.core.records import (
    MSG_CONSERVE_INVALID,
    MSG_PEER_OFF,
    MSG_QUALITY_BOUNDS,
    MSG_TIER_UNAVAILABLE,
    AlreadySubmitted,
    Decision,
    InvalidDecision,
    PeerBuy,
    PeerSell,
    StaleDay,
    msg_offer_units_bounds,
    msg_peer_price_bounds,
    msg_unaffordable,
)

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
SPOT_CLOSED = "Spot orders closed: they would arrive after the game ends"
FUTURES_CLOSED = "Futures closed: deliveries would arrive after the game ends"


def rejected(e, alias, decision):
    with pytest.raises(InvalidDecision) as info:
        e.submit(alias, decision)
    assert info.value.code == "INVALID_DECISION"
    assert isinstance(info.value.message, str) and info.value.message
    assert str(info.value) == info.value.message
    return info.value.message


def last_day_engine(**over):
    """Duration 7, H 3, L 1: on day 7 spot is closed, from day 5 futures are."""
    return start(
        quiet(
            2,
            duration_days=7,
            futures__horizon_days=3,
            supplier__lead_time_days=1,
            **over,
        )
    )


def advance(e, to_day):
    while e.day < to_day:
        play(e)


# ---------------------------------------------------------------- bookkeeping


def test_submit_tracks_awaiting_and_all_in():
    e = start(quiet(3))
    assert e.awaiting() == ["P1", "P2", "P3"]
    assert not e.has_submitted("P2")
    e.submit("P2", D(1))
    assert e.has_submitted("P2") and not e.has_submitted("P1")
    assert e.awaiting() == ["P1", "P3"]
    assert e.all_in() is False
    e.submit("P3", D(1))
    e.submit("P1", D(1))
    assert e.awaiting() == []
    assert e.all_in() is True
    assert e.player_view("P1")["has_submitted"] is True
    assert e.player_view("P1")["awaiting"] == []


def test_resolve_requires_every_player_to_have_submitted():
    e = start(quiet(2))
    e.submit("P1", D(1))
    with pytest.raises(ValueError):
        e.resolve_day()
    assert e.day == 1


def test_unseated_alias_is_a_programming_error():
    e = start(quiet(2))
    with pytest.raises(ValueError):
        e.submit("P9", D(1))


def test_submitting_after_the_game_finished_is_a_programming_error():
    e = start(quiet(2))
    e.end_early()
    assert e.phase == Phase.FINISHED
    with pytest.raises(ValueError):
        e.submit("P1", D(1))


def test_accepted_decision_is_stored_and_used_at_resolution():
    e = start(quiet(2))
    decision = D(1, 7, 3, CIP, (), True)
    e.submit("P1", decision)
    e.submit("P2", D(1))
    out = e.resolve_day()
    r = out.records["P1"]
    assert (r.order_spot, r.order_futures, r.incoterm_tier) == (7, 3, CIP)
    assert r.cooperate is True and r.conserve_lots == ()
    assert r.outlay == round_cents(12 * 7 + 12 * 3 + (1.0 + 0.55) * 7)


def test_second_submission_is_refused_and_changes_nothing():
    e = start(quiet(2))
    e.submit("P1", D(1, 5))
    before = e.to_payload()
    with pytest.raises(AlreadySubmitted) as info:
        e.submit("P1", D(1, 9))
    assert info.value.code == "ALREADY_SUBMITTED"
    assert e.to_payload() == before
    e.submit("P2", D(1))
    assert e.resolve_day().records["P1"].order_spot == 5


def test_stale_day_is_refused_whatever_else_is_wrong():
    e = start(quiet(2))
    play(e)
    for day in (1, 3, 0):
        with pytest.raises(StaleDay) as info:
            e.submit("P1", D(day))
        assert info.value.code == "STALE_DAY"
    before = e.to_payload()
    with pytest.raises(StaleDay):
        e.submit("P1", D(1, 5000, 0, DAP, ("L99",)))
    assert e.to_payload() == before
    assert e.awaiting() == ["P1", "P2"]


def test_stale_day_beats_already_submitted():
    e = start(quiet(2))
    e.submit("P1", D(1))
    with pytest.raises(StaleDay):
        e.submit("P1", D(2))


def test_already_submitted_beats_every_content_check():
    e = start(quiet(2))
    e.submit("P1", D(1))
    with pytest.raises(AlreadySubmitted):
        e.submit("P1", D(1, 5000, 0, DAP, ("L99",)))


# ------------------------------------------------------------------ rejections


def test_spot_order_while_spot_is_closed():
    e = last_day_engine()
    advance(e, 7)
    assert order_windows(7, 7, 1, 3, 12.0).spot_open is False
    assert rejected(e, "P1", D(7, 1)) == SPOT_CLOSED
    e.submit("P1", D(7, 0))  # zero orders are fine on a closed day


def test_futures_order_while_futures_are_closed():
    e = last_day_engine()
    advance(e, 5)
    assert rejected(e, "P1", D(5, 0, 1)) == FUTURES_CLOSED
    assert rejected(e, "P1", D(5, 0, 1)) == (
        order_windows(5, 7, 1, 3, 12.0).futures_message
    )
    e.submit("P1", D(5, 4, 0))  # spot is still open on day 5


def test_futures_still_open_on_the_last_day_to_buy():
    e = last_day_engine()
    advance(e, 4)
    e.submit("P1", D(4, 0, 5))


def test_disabled_tier_is_refused_and_enabled_tiers_pass():
    e = start(quiet(2, insurance__tiers__dap__enabled=False))
    assert rejected(e, "P1", D(1, 1, 0, DAP)) == MSG_TIER_UNAVAILABLE
    e.submit("P1", D(1, 1, 0, DDP))
    e.submit("P2", D(1, 1, 0, CIP))


def test_peer_limits_are_refused_while_the_peer_market_is_off():
    e = start(quiet(2, peer_market__enabled=False))
    assert rejected(e, "P1", D(1, buy=PeerBuy(10.0, 50))) == MSG_PEER_OFF
    assert rejected(e, "P1", D(1, sell=PeerSell(10.0, 50, 5))) == MSG_PEER_OFF
    assert MSG_PEER_OFF == "The peer market is off in this game."
    e.submit("P1", D(1))


@pytest.mark.parametrize(
    "limit",
    [
        PeerBuy(0.0, 50),
        PeerBuy(-1.0, 50),
        PeerBuy(0.005, 50),
        PeerBuy(12.345, 50),
        PeerBuy(MAX_PEER_PRICE + 0.01, 50),
    ],
)
def test_peer_buy_price_bounds(limit):
    e = start(quiet(2))
    assert rejected(e, "P1", D(1, buy=limit)) == msg_peer_price_bounds()
    assert not e.has_submitted("P1")


@pytest.mark.parametrize("quality", [-1, 101])
def test_peer_buy_quality_bounds(quality):
    e = start(quiet(2))
    assert rejected(e, "P1", D(1, buy=PeerBuy(10.0, quality))) == MSG_QUALITY_BOUNDS


@pytest.mark.parametrize("price", [0.0, 0.004, 12.345, MAX_PEER_PRICE + 0.01])
def test_peer_sell_price_bounds(price):
    e = start(quiet(2))
    assert rejected(e, "P1", D(1, sell=PeerSell(price, 50, 5))) == (
        msg_peer_price_bounds()
    )


@pytest.mark.parametrize("quality", [-1, 101])
def test_peer_sell_quality_bounds(quality):
    e = start(quiet(2))
    assert rejected(e, "P1", D(1, sell=PeerSell(10.0, quality, 5))) == (
        MSG_QUALITY_BOUNDS
    )


@pytest.mark.parametrize("units", [0, -3, MAX_ORDER + 1])
def test_peer_sell_unit_bounds(units):
    e = start(quiet(2))
    assert rejected(e, "P1", D(1, sell=PeerSell(10.0, 50, units))) == (
        msg_offer_units_bounds()
    )


def test_peer_limits_at_their_bounds_are_accepted():
    e = start(quiet(2))
    e.submit("P1", D(1, buy=PeerBuy(0.01, 0), sell=PeerSell(MAX_PEER_PRICE, 100, 1)))
    e.submit(
        "P2", D(1, buy=PeerBuy(MAX_PEER_PRICE, 100), sell=PeerSell(0.01, 0, MAX_ORDER))
    )
    assert e.all_in()


def test_conserve_list_must_name_unconserved_lots_of_the_cellar():
    e = start(quiet(2, demand__per_player_peak=1))
    assert rejected(e, "P1", D(1, conserve=("L99",))) == MSG_CONSERVE_INVALID
    e.submit("P1", D(1, conserve=("L0",)))
    e.submit("P2", D(1))
    e.resolve_day()
    # L0 is conserved now: it may not be conserved twice
    assert e.player_view("P1")["cellar"][0]["id"] == "L0"
    assert e.player_view("P1")["cellar"][0]["conserved"] is True
    assert rejected(e, "P1", D(2, conserve=("L0",))) == MSG_CONSERVE_INVALID
    # another player's unconserved lot is still in its own cellar only
    e.submit("P2", D(2, conserve=("L0",)))


def test_a_lot_of_another_cellar_cannot_be_conserved():
    e = start(
        quiet(
            2,
            stock__initial_stock=0,
            supplier__lead_time_days=0,
            demand__per_player_peak=1,
        )
    )
    # P2 will own L1 after day 1; P1 never gets one
    e.submit("P1", D(1))
    e.submit("P2", D(1, 20))
    e.resolve_day()
    assert [x["id"] for x in e.player_view("P2")["cellar"]] == ["L1"]
    assert e.player_view("P1")["cellar"] == []
    assert rejected(e, "P1", D(2, conserve=("L1",))) == MSG_CONSERVE_INVALID
    e.submit("P2", D(2, conserve=("L1",)))


# ---------------------------------------------------------- validation order


def test_validation_order_spot_closed_before_tier():
    e = last_day_engine(insurance__tiers__dap__enabled=False)
    advance(e, 7)
    assert rejected(e, "P1", D(7, 1, 0, DAP)) == SPOT_CLOSED


def test_validation_order_tier_before_peer_off():
    e = start(
        quiet(2, peer_market__enabled=False, insurance__tiers__dap__enabled=False)
    )
    assert rejected(e, "P1", D(1, 0, 0, DAP, buy=PeerBuy(10.0, 50))) == (
        MSG_TIER_UNAVAILABLE
    )


def test_validation_order_peer_off_before_peer_bounds():
    e = start(quiet(2, peer_market__enabled=False))
    assert rejected(e, "P1", D(1, buy=PeerBuy(0.0, 500))) == MSG_PEER_OFF
    assert rejected(e, "P1", D(1, sell=PeerSell(0.0, 500, 0))) == MSG_PEER_OFF


def test_validation_order_peer_bounds_before_conserve():
    e = start(quiet(2))
    assert rejected(e, "P1", D(1, conserve=("L99",), buy=PeerBuy(0.0, 50))) == (
        msg_peer_price_bounds()
    )
    assert rejected(e, "P1", D(1, conserve=("L99",), sell=PeerSell(5.0, 50, 0))) == (
        msg_offer_units_bounds()
    )


def test_validation_order_peer_price_before_quality_before_units():
    e = start(quiet(2))
    assert rejected(e, "P1", D(1, buy=PeerBuy(0.0, 500))) == msg_peer_price_bounds()
    assert rejected(e, "P1", D(1, sell=PeerSell(0.0, 500, 0))) == (
        msg_peer_price_bounds()
    )
    assert rejected(e, "P1", D(1, sell=PeerSell(5.0, 500, 0))) == MSG_QUALITY_BOUNDS


def test_validation_order_conserve_before_affordability():
    e = start(quiet(2, starting_cash=10))
    assert rejected(e, "P1", D(1, 100, conserve=("L99",))) == MSG_CONSERVE_INVALID


# ------------------------------------------------------------- affordability


def test_affordability_worked_example():
    e = start(quiet(2, starting_cash=500))
    over = D(1, 40)
    assert e.outlay("P1", over) == pytest.approx(520.0)
    message = rejected(e, "P1", over)
    assert message == msg_unaffordable(520.0, 500.0)
    assert message == "Today's purchases cost $520.00 but you only have $500.00"
    assert e.outlay("P1", D(1, 38)) == pytest.approx(494.0)
    e.submit("P1", D(1, 38))
    e.submit("P2", Decision.zero(1))
    assert e.all_in()


def test_rejected_decision_changes_nothing():
    e = start(quiet(2, starting_cash=500))
    before = e.to_payload()
    awaiting = e.awaiting()
    rejected(e, "P1", D(1, 40))
    rejected(e, "P1", D(1, 1, 0, EXW, ("L99",)))
    assert e.to_payload() == before
    assert e.awaiting() == awaiting
    assert not e.has_submitted("P1")
    e.submit("P1", D(1, 38))
    assert e.has_submitted("P1")


def test_outlay_equal_to_cash_is_accepted_and_one_cent_over_is_rejected():
    exact = start(quiet(2, starting_cash=520))
    exact.submit("P1", D(1, 40))
    assert exact.has_submitted("P1")
    over = start(quiet(2, starting_cash=519.99))
    assert rejected(over, "P1", D(1, 40)) == msg_unaffordable(520.0, 519.99)
    assert not over.has_submitted("P1")


def test_the_cash_check_compares_the_outlay_rounded_to_cents():
    # outlay = 12 + 0.333 = 12.333 -> $12.33, which a cash of $12.33 covers
    e = start(quiet(2, starting_cash=12.33, transport__unit_cost=0.333))
    assert e.outlay("P1", D(1, 1)) == pytest.approx(12.333)
    e.submit("P1", D(1, 1))
    assert e.has_submitted("P1")


def test_outlay_formula_terms():
    e = start(quiet(2, futures__horizon_days=3))
    spot = e.brief()["supplier"]["spot_price"]
    assert spot == 12.0
    # spot x price + futures x base + (transport + fee) x spot + conservation
    assert e.outlay("P1", D(1, 10, 5, CIP, ("L0",))) == pytest.approx(
        12 * 10 + 12 * 5 + (1.0 + 0.55) * 10 + 1.0 * 30
    )
    assert e.outlay("P1", D(1, 10, 0, DDP)) == pytest.approx(12 * 10 + 2.39 * 10)
    assert e.outlay("P1", D(1, 0, 8, DDP)) == pytest.approx(96.0)
    assert e.outlay("P1", Decision.zero(1)) == 0.0


def test_outlay_uses_todays_spot_price():
    e = start(cfg(max_players=2, supplier__spot_spread=0.25), seed=11)
    spot = e.brief()["supplier"]["spot_price"]
    assert spot != 12.0
    assert e.outlay("P1", D(1, 10)) == pytest.approx(spot * 10 + 10)


def test_outlay_charges_fee_on_futures_due_but_never_transport():
    e = start(quiet(2, futures__horizon_days=3, supplier__lead_time_days=1))
    e.submit("P1", D(1, 0, 10))
    e.submit("P2", D(1))
    e.resolve_day()
    play(e)
    assert e.day == 3
    assert e.player_view("P1")["futures_book"][0]["ships_day"] == 3
    assert e.outlay("P1", D(3, 0, 0, EXW)) == 0.0
    # fee x due only: the 10 units' transport was committed at purchase
    assert e.outlay("P1", D(3, 0, 0, CIP)) == pytest.approx(0.55 * 10)
    assert e.outlay("P1", D(3, 5, 0, DDP)) == pytest.approx(
        (12 + 1.0 + 1.39) * 5 + 1.39 * 10
    )
    assert e.outlay("P2", D(3, 0, 0, CIP)) == 0.0


def test_outlay_conservation_sums_the_quantities_of_all_lots():
    e = start(quiet(2, demand__per_player_peak=1))
    e.submit("P1", D(1, 30))
    e.submit("P2", D(1))
    e.resolve_day()
    lots = {x["id"]: x["qty"] for x in e.player_view("P1")["cellar"]}
    assert "L0" in lots
    e.submit("P2", D(2))
    assert e.outlay("P1", D(2, conserve=tuple(lots))) == pytest.approx(
        1.0 * sum(lots.values())
    )


def test_negative_cash_allows_only_the_zero_outlay_decision():
    # spec 14: a broke player cannot block the day (huge holding cost drives cash < 0)
    e = start(
        quiet(
            2,
            starting_cash=1000,
            stock__holding_cost=100,
            demand__per_player_peak=1,
            futures__horizon_days=3,
            supplier__lead_time_days=1,
        )
    )
    e.submit("P1", D(1, 0, 30))
    e.submit("P2", D(1))
    e.resolve_day()
    play(e)
    assert e.day == 3
    assert e.player_view("P1")["cash"] < 0
    assert e.player_view("P1")["futures_book"][0]["qty"] == 30
    cash = e.player_view("P1")["cash"]
    bad = D(3, 1)
    assert rejected(e, "P1", bad) == msg_unaffordable(
        round_cents(e.outlay("P1", bad)), cash
    )
    insured = D(3, 0, 0, CIP)  # fee on the 30 futures due: outlay 12 > cash
    assert e.outlay("P1", insured) > 0
    rejected(e, "P1", insured)
    e.submit("P1", Decision.zero(3))  # zero outlay, futures due: accepted
    e.submit("P2", Decision.zero(3))
    out = e.resolve_day()
    assert out.records["P1"].futures_due == 30
    assert out.records["P1"].futures_filled == 30
    assert out.records["P1"].cost_transport == 30.0


def test_outlay_mock_of_910_dollars():
    # 40 x $13.40 + 25 x $12.00 + 40 x ($1.00 + $0.55) + $12 conservation = $910
    config = cfg(
        max_players=2,
        supplier__capacity_sd_per_player=0,
        supplier__disruption_probability=0,
        stock__initial_stock=12,
        starting_cash=6120,
    )
    e = start(config, seed=730)
    assert e.brief()["supplier"]["spot_price"] == 13.4
    decision = D(1, 40, 25, CIP, ("L0",))
    assert e.outlay("P1", decision) == pytest.approx(910.0)
    e.submit("P1", decision)
    assert e.has_submitted("P1")
