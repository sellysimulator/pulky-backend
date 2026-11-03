"""Lead time 0 (the default) and 1 or more (spec 4.2, 5.6, 6.3, 9.1), and the
Paid-price card against the engine's refund (spec 6.3 example)."""

import copy

import pytest

from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.engine import PulkyEngine
from app.core.enums import IncotermTier
from app.core.insurance import shipment_unit_cost
from app.core.money import round_cents
from app.core.records import Decision, InvalidDecision

CIP = IncotermTier.CIP
SPOT_CLOSED = "Spot orders closed: they would arrive after the game ends"


def config(**over: object) -> GameConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    base = {
        "max_players": 2,
        "duration_days": 7,
        "futures__horizon_days": 3,
        "supplier__capacity_sd_per_player": 0,
        "supplier__disruption_probability": 0,
        "transport__failure_probability": 0,
    }
    for path, value in {**base, **over}.items():
        keys = path.split("__")
        node = data
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return GameConfig.from_dict(data)


def start(cfg: GameConfig, seed: int = 7) -> PulkyEngine:
    return PulkyEngine.start(cfg, {"P1": "La Chula", "P2": "Don Agave"}, seed)


def play(e: PulkyEngine, p1: Decision | None = None) -> None:
    day = e.day
    e.submit("P1", p1 or Decision.zero(day))
    e.submit("P2", Decision.zero(day))
    e.resolve_day()


def spot(day: int, qty: int, fut: int = 0, tier: IncotermTier = CIP) -> Decision:
    return Decision(day, qty, fut, tier, (), False)


def tier(e: PulkyEngine, name: str) -> dict:
    return next(r for r in e.player_view("P1")["tier_plans"] if r["tier"] == name)


def test_the_default_lead_time_is_zero() -> None:
    assert DEFAULT_CONFIG["supplier"]["lead_time_days"] == 0
    assert GameConfig.default().supplier.lead_time_days == 0


def test_at_lead_time_zero_spot_never_closes_and_nothing_is_in_transit() -> None:
    e = start(config(supplier__lead_time_days=0))
    while e.phase != "finished":
        view = e.player_view("P1")
        assert view["order_windows"]["spot_open"] is True
        assert view["brief"]["spot"]["arrives_day"] == e.day
        assert view["in_transit"] == []
        play(e, spot(e.day, 5))  # an order on the last day is accepted too
        assert e.player_view("P1")["in_transit"] == []
    assert e.records("P1")[-1].delivered == 5


def test_at_lead_time_two_orders_wait_and_spot_closes_for_the_last_two_days() -> None:
    e = start(config(supplier__lead_time_days=2))
    play(e, spot(1, 5))
    view = e.player_view("P1")
    assert view["brief"]["spot"]["arrives_day"] == 4
    assert [
        (s["shipped_day"], s["arrives_day"], s["qty"]) for s in view["in_transit"]
    ] == [(1, 3, 5)]
    while e.day < 6:
        play(e)
    view = e.player_view("P1")
    assert view["order_windows"]["spot_open"] is False
    assert view["order_windows"]["spot_message"] == SPOT_CLOSED
    with pytest.raises(InvalidDecision) as info:
        e.submit("P1", spot(6, 1))
    assert info.value.message == SPOT_CLOSED


def test_the_initial_pipeline_arrives_only_with_a_lead_time() -> None:
    none = start(config(supplier__lead_time_days=0, supplier__initial_pipeline_qty=10))
    assert none.player_view("P1")["in_transit"] == []
    play(none)
    assert none.records("P1")[0].delivered == 0
    two = start(config(supplier__lead_time_days=2, supplier__initial_pipeline_qty=10))
    assert [s["qty"] for s in two.player_view("P1")["in_transit"]] == [10, 10]
    play(two)
    assert two.records("P1")[0].delivered == 10


def test_the_market_price_card_shows_todays_spot_price_only_at_lead_time_zero() -> None:
    same_day = start(config(supplier__lead_time_days=0))
    later = start(config(supplier__lead_time_days=1))
    today = same_day.brief()["supplier"]["spot_price"]
    assert tier(same_day, "dap")["refund_value_today"] == today
    assert tier(later, "dap")["refund_value_today"] is None


def test_the_paid_price_card_shows_what_the_engine_refunds() -> None:
    # Spec 6.3 example, played: 20 futures bought on day 1 ship on day 4 with a
    # spot order of 20; every unit of a failure is refunded at the shipment's
    # average price paid, and the card that morning says exactly that.
    e = start(
        config(
            supplier__spot_spread=0.25,
            transport__failure_probability=1.0,
            transport__loss_fraction=0.3,
        ),
        seed=3,
    )
    play(e, spot(1, 0, fut=20))
    play(e, spot(2, 20))
    play(e, spot(3, 20))
    assert e.day == 4
    card = tier(e, "cip")
    spot_price = e.brief()["supplier"]["spot_price"]
    assert spot_price != 12.0  # a real blend, not the base price twice
    assert card["refund_shipment"] == {"spot_units": 20, "futures_units": 20}
    assert card["refund_value_today"] == shipment_unit_cost(20, spot_price, 20, 12.0)
    assert card["refund_value_today"] != spot_price  # the old card's number
    play(e, spot(4, 20))
    record = e.records("P1")[-1]
    assert (record.shipped, record.lost_transport) == (40, 12)
    assert record.refund_insurance == round_cents(12 * card["refund_value_today"])
    assert record.refund_insurance != round_cents(12 * spot_price)
