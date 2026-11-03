"""The decision form's server figures (spec 12.4): ``PulkyEngine.decision_preview``.

The form computes none of them: the outlay, affordability, the tier cards for the
draft shipment (the Paid-price card follows the draft spot order, spec 6.3), the
units a sell offer puts up now and every field the server would refuse.
"""

import copy
import json
import random
from typing import Any

import pytest

from app.core.config_models import GameConfig
from app.core.defaults import DEFAULT_CONFIG
from app.core.engine import PulkyEngine, offer_lots
from app.core.enums import IncotermTier
from app.core.insurance import shipment_unit_cost
from app.core.limits import MAX_ORDER
from app.core.lots import Cellar
from app.core.money import round_cents
from app.core.records import (
    MSG_QUALITY_BOUNDS,
    Decision,
    InvalidDecision,
    msg_offer_units_bounds,
    msg_order_bounds,
    msg_peer_price_bounds,
    msg_unaffordable,
)
from app.core.supply import AbstractSupplier


def cfg(**over: Any) -> GameConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    for path, value in over.items():
        node = data
        keys = path.split("__")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return GameConfig.from_dict(data)


def seats(n: int) -> dict[str, str]:
    return {f"P{i}": f"Bot {i}" for i in range(1, n + 1)}


def draft(day: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "day": day,
        "order_spot": 0,
        "order_futures": 0,
        "incoterm_tier": "exw",
        "conserve_lots": [],
        "cooperate": False,
        "peer_buy": None,
        "peer_sell": None,
    }
    base.update(over)
    return base


def zero_day(e: PulkyEngine, decisions: dict[str, Decision] | None = None) -> None:
    for alias in e.aliases:
        e.submit(alias, (decisions or {}).get(alias, Decision.zero(e.day)))
    e.resolve_day()


def seed_with_spot_price(config: GameConfig, n: int, day: int, price: float) -> int:
    """The first seed whose ``day`` draws this spot price (the supplier's own draw)."""
    s = config.supplier
    for seed in range(200_000):
        supplier = AbstractSupplier(
            seed=seed,
            n_players=n,
            capacity_mean_per_player=s.capacity_mean_per_player,
            capacity_sd_per_player=s.capacity_sd_per_player,
            disruption_probability=s.disruption_probability,
            disruption_factor=s.disruption_factor,
            base_price=s.base_price,
            spot_spread=s.spot_spread,
        )
        if supplier.draw_day(day).spot_price == price:
            return seed
    raise AssertionError("no seed draws that price")


@pytest.fixture(scope="module")
def mixed_day() -> PulkyEngine:
    """Spec 6.3's mixed shipment: on day 8, 20 futures units bought on day 1 ship and
    the spot price is $14.50 (base $12.00)."""
    config = cfg(max_players=2, supplier__disruption_probability=0.0)
    seed = seed_with_spot_price(config, 2, 8, 14.50)
    e = PulkyEngine.start(config, seats(2), seed)
    buy = Decision(1, 0, 20, IncotermTier.EXW, (), False, None, None)
    zero_day(e, {"P1": buy})
    while e.day < 8:
        zero_day(e)
    assert e.brief()["supplier"]["spot_price"] == 14.50
    return e


def cip(preview: dict[str, Any]) -> dict[str, Any]:
    return next(p for p in preview["tier_plans"] if p["tier"] == "cip")


# ------------------------------------------------- the Paid-price card (task 1)


def test_paid_price_card_follows_the_draft_spot_order(mixed_day: PulkyEngine) -> None:
    # Spec 6.3 example: 20 spot at $14.50 and 20 futures at $12.00 -> $13.25.
    p = mixed_day.decision_preview("P1", draft(8, order_spot=20))
    assert cip(p)["refund_value_today"] == 13.25
    assert cip(p)["refund_shipment"] == {"spot_units": 20, "futures_units": 20}
    # the figure moves with the draft, not with the order the server holds
    p = mixed_day.decision_preview("P1", draft(8, order_spot=60))
    assert cip(p)["refund_value_today"] == shipment_unit_cost(60, 14.50, 20, 12.0)
    assert cip(p)["refund_value_today"] == pytest.approx(13.875)
    p = mixed_day.decision_preview("P1", draft(8, order_spot=0))
    assert cip(p)["refund_value_today"] == 12.0
    assert cip(p)["refund_shipment"] == {"spot_units": 0, "futures_units": 20}
    # the held order (none submitted, prefill 0) is still what player_view shows
    held = next(
        p for p in mixed_day.player_view("P1")["tier_plans"] if p["tier"] == "cip"
    )
    assert held["refund_value_today"] == 12.0


def test_paid_price_card_without_futures_is_the_spot_price(
    mixed_day: PulkyEngine,
) -> None:
    for spot in (0, 7, 80):
        p = mixed_day.decision_preview("P2", draft(8, order_spot=spot))
        assert cip(p)["refund_value_today"] == 14.50
        assert cip(p)["refund_shipment"] is None


def test_fee_for_shipment_is_the_fee_on_spot_plus_futures_due(
    mixed_day: PulkyEngine,
) -> None:
    p = mixed_day.decision_preview("P1", draft(8, order_spot=15, incoterm_tier="ddp"))
    assert p["shipment_units"] == 35
    for row in p["tier_plans"]:
        assert row["fee_for_shipment"] == round_cents(row["fee_per_unit"] * 35)
    assert (
        next(r for r in p["tier_plans"] if r["tier"] == "exw")["fee_for_shipment"] == 0
    )


# ------------------------------------------------------- outlay and its gate


def _random_draft(rng: random.Random, e: PulkyEngine, alias: str) -> dict[str, Any]:
    lots = [lot["id"] for lot in e.player_view(alias)["cellar"]]
    return draft(
        e.day,
        order_spot=rng.randint(0, 600),
        order_futures=rng.randint(0, 600) if e.day <= 7 else 0,
        incoterm_tier=rng.choice(["exw", "cip", "dap", "ddp"]),
        conserve_lots=rng.sample(lots, rng.randint(0, len(lots))),
    )


def test_outlay_and_affordability_are_the_engines_own(mixed_day: PulkyEngine) -> None:
    rng = random.Random(4)
    for _ in range(60):
        data = _random_draft(rng, mixed_day, "P1")
        p = mixed_day.decision_preview("P1", data)
        decision = Decision(
            data["day"],
            data["order_spot"],
            data["order_futures"],
            IncotermTier(data["incoterm_tier"]),
            tuple(data["conserve_lots"]),
            False,
            None,
            None,
        )
        parts = mixed_day.outlay_parts("P1", decision)
        total = mixed_day.outlay("P1", decision)
        assert p["outlay"] == {
            **{k: round_cents(v) for k, v in parts.items()},
            "total": round_cents(total),
        }
        # the gate is the one submit applies (spec 8.3)
        engine = PulkyEngine.from_payload(mixed_day.to_payload(), mixed_day.config)
        try:
            engine.submit("P1", decision)
            accepted = True
        except InvalidDecision as exc:
            accepted = False
            assert exc.message == p["unaffordable_message"]
        assert p["affordable"] is accepted
        if accepted:
            assert p["unaffordable_message"] is None


def test_unaffordable_draft_carries_the_spec_message(mixed_day: PulkyEngine) -> None:
    cash = mixed_day.player_view("P1")["cash"]
    p = mixed_day.decision_preview("P1", draft(8, order_spot=MAX_ORDER))
    assert p["affordable"] is False
    assert p["unaffordable_message"] == msg_unaffordable(p["outlay"]["total"], cash)


def test_a_zero_outlay_is_always_affordable() -> None:
    e = PulkyEngine.start(cfg(starting_cash=0.0), seats(2), 3)
    p = e.decision_preview("P1", draft(1))
    assert p["outlay"]["total"] == 0 and p["affordable"] is True


def test_closed_orders_count_as_zero() -> None:
    e = PulkyEngine.start(cfg(max_players=2), seats(2), 5)
    while e.day < e.config.duration_days:
        zero_day(e)
    assert not e.player_view("P1")["order_windows"]["futures_open"]
    p = e.decision_preview("P1", draft(e.day, order_futures=50))
    assert p["outlay"]["futures"] == 0


# ----------------------------------------------------------- field messages


@pytest.mark.parametrize(
    ("over", "field", "message"),
    [
        ({"order_spot": None}, "order_spot", msg_order_bounds()),
        ({"order_spot": -1}, "order_spot", msg_order_bounds()),
        ({"order_spot": 2.5}, "order_spot", msg_order_bounds()),
        ({"order_futures": MAX_ORDER + 1}, "order_futures", msg_order_bounds()),
        (
            {"peer_buy": {"max_price": 0.005, "min_quality": 50}},
            "peer_buy.max_price",
            msg_peer_price_bounds(),
        ),
        (
            {"peer_buy": {"max_price": 14.0, "min_quality": None}},
            "peer_buy.min_quality",
            MSG_QUALITY_BOUNDS,
        ),
        (
            {"peer_sell": {"price": 12.345, "max_quality": 40, "max_units": 3}},
            "peer_sell.price",
            msg_peer_price_bounds(),
        ),
        (
            {"peer_sell": {"price": 12.0, "max_quality": 101, "max_units": 3}},
            "peer_sell.max_quality",
            MSG_QUALITY_BOUNDS,
        ),
        (
            {"peer_sell": {"price": 12.0, "max_quality": 40, "max_units": 0}},
            "peer_sell.max_units",
            msg_offer_units_bounds(),
        ),
    ],
)
def test_each_field_the_server_refuses_has_its_spec_message(
    over: dict[str, Any], field: str, message: str
) -> None:
    e = PulkyEngine.start(cfg(), seats(2), 1)
    p = e.decision_preview("P1", draft(1, **over))
    assert p["errors"] == [{"field": field, "message": message}]


def test_a_bad_order_leaves_the_outlay_unknown() -> None:
    e = PulkyEngine.start(cfg(), seats(2), 1)
    p = e.decision_preview("P1", draft(1, order_spot=None))
    assert p["outlay"] is None and p["affordable"] is None
    assert p["tier_plans"] is None and p["shipment_units"] is None


def test_a_valid_draft_has_no_errors_and_every_part() -> None:
    e = PulkyEngine.start(cfg(), seats(2), 1)
    p = e.decision_preview(
        "P1",
        draft(
            1,
            order_spot=10,
            peer_buy={"max_price": 14.0, "min_quality": 40},
            peer_sell={"price": 15.0, "max_quality": 100, "max_units": 5},
        ),
    )
    assert p["errors"] == []
    assert p["day"] == 1
    assert set(p) == {
        "day",
        "outlay",
        "affordable",
        "unaffordable_message",
        "shipment_units",
        "tier_plans",
        "offer",
        "errors",
    }


def test_garbage_is_a_preview_of_nothing_never_an_exception() -> None:
    e = PulkyEngine.start(cfg(), seats(2), 1)
    for junk in (None, [], "x", {"order_spot": "ten"}, {"conserve_lots": "L0"}):
        p = e.decision_preview("P1", junk)
        assert isinstance(p, dict)


# ------------------------------------------------------ units on offer


def test_units_on_offer_leave_out_lots_ticked_to_conserve_and_cap() -> None:
    e = PulkyEngine.start(cfg(max_players=2, stock__initial_stock=30), seats(2), 2)
    sell = {"price": 15.0, "max_quality": 100, "max_units": 12}
    p = e.decision_preview("P1", draft(1, peer_sell=sell))
    assert p["offer"] == {"units": 12, "lot_ids": ["L0"]}
    p = e.decision_preview("P1", draft(1, peer_sell=sell, conserve_lots=["L0"]))
    assert p["offer"] == {"units": 0, "lot_ids": []}
    # the lots are the ones resolution offers: quality at or below the maximum
    lots = Cellar.from_payload(e.to_payload()["players"]["P1"]["cellar"]).lots
    low = {**sell, "max_quality": 50}
    p = e.decision_preview("P1", draft(1, peer_sell=low))
    assert p["offer"]["lot_ids"] == [lot.id for lot in offer_lots(lots, 50)]


def test_no_offer_without_a_sell_limit_or_with_the_market_off() -> None:
    e = PulkyEngine.start(cfg(), seats(2), 2)
    assert e.decision_preview("P1", draft(1))["offer"] is None
    off = PulkyEngine.start(cfg(peer_market__enabled=False), seats(2), 2)
    sell = {"price": 15.0, "max_quality": 100, "max_units": 12}
    assert off.decision_preview("P1", draft(1, peer_sell=sell))["offer"] is None


# ------------------------------------------------------------- read-only


def test_a_preview_changes_nothing(mixed_day: PulkyEngine) -> None:
    before = json.dumps(mixed_day.to_payload(), sort_keys=True)
    mixed_day.decision_preview("P1", draft(8, order_spot=40, conserve_lots=["L0"]))
    assert json.dumps(mixed_day.to_payload(), sort_keys=True) == before
    assert mixed_day.has_submitted("P1") is False
