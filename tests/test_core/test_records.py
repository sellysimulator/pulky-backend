"""Section 03: records (black-box, from the documents)."""

import copy
import dataclasses
import json
import math
from typing import Any

import pytest

from app.core.enums import IncotermTier, TradeSide, Weekday
from app.core.records import (
    MSG_CONSERVE_INVALID,
    MSG_PEER_OFF,
    MSG_QUALITY_BOUNDS,
    MSG_TIER_UNAVAILABLE,
    MSG_UNREADABLE,
    AlreadySubmitted,
    DayRecord,
    Decision,
    DecisionError,
    FinalSettlement,
    InvalidDecision,
    MarketDay,
    PeerBuy,
    PeerSell,
    PeerTrade,
    PublicDayResults,
    PublicPlayerDay,
    Shipment,
    StaleDay,
    msg_conserve_too_many,
    msg_offer_units_bounds,
    msg_order_bounds,
    msg_peer_price_bounds,
    msg_unaffordable,
    parse_decision,
)


def good(**over: Any) -> dict:
    base: dict = {
        "room_id": "ABC234",
        "day": 5,
        "order_spot": 40,
        "order_futures": 25,
        "incoterm_tier": "cip",
        "conserve_lots": ["L3"],
        "cooperate": True,
        "peer_buy": {"max_price": 16, "min_quality": 90},
        "peer_sell": {"price": 15.0, "max_quality": 60, "max_units": 10},
    }
    base.update(over)
    return base


def rejected(**over: Any) -> str:
    with pytest.raises(InvalidDecision) as info:
        parse_decision(good(**over))
    assert info.value.code == "INVALID_DECISION"
    assert str(info.value) == info.value.message
    return info.value.message


# ---------------------------------------------------------------- messages


def test_message_copy_is_verbatim() -> None:
    assert MSG_UNREADABLE == (
        "Your decision could not be read. Reload the page and try again."
    )
    assert MSG_TIER_UNAVAILABLE == (
        "That shipping insurance is not available in this game."
    )
    assert MSG_CONSERVE_INVALID == (
        "You can only conserve lots in your cellar that are not conserved yet."
    )
    assert MSG_PEER_OFF == "The peer market is off in this game."
    assert MSG_QUALITY_BOUNDS == ("Quality limits must be whole numbers from 0 to 100.")
    assert msg_order_bounds() == "Orders must be whole numbers from 0 to 9,999."
    assert msg_conserve_too_many() == "You can conserve at most 30 lots a day."
    assert msg_peer_price_bounds() == (
        "Peer prices must be from $0.01 to $10,000.00, in whole cents."
    )
    assert msg_offer_units_bounds() == "You can offer from 1 to 9,999 units."
    assert msg_unaffordable(904, 500) == (
        "Today's purchases cost $904.00 but you only have $500.00"
    )
    assert msg_unaffordable(1234.5, 0) == (
        "Today's purchases cost $1,234.50 but you only have $0.00"
    )


def test_decision_errors() -> None:
    assert StaleDay.code == "STALE_DAY"
    assert AlreadySubmitted.code == "ALREADY_SUBMITTED"
    assert InvalidDecision.code == "INVALID_DECISION"
    stale = StaleDay()
    assert stale.code == "STALE_DAY"
    assert stale.message == "That day is over. Your screen has been updated."
    assert str(stale) == stale.message
    again = AlreadySubmitted()
    assert again.message == "You have already submitted today's decision."
    assert str(again) == again.message
    bad = InvalidDecision("nope")
    assert bad.message == "nope" and str(bad) == "nope"
    for cls in (StaleDay, AlreadySubmitted, InvalidDecision):
        assert issubclass(cls, DecisionError)
        assert issubclass(cls, Exception)


# ---------------------------------------------------------------- parse_decision


def test_parse_worked_example() -> None:
    assert parse_decision(good()) == Decision(
        day=5,
        order_spot=40,
        order_futures=25,
        incoterm_tier=IncotermTier.CIP,
        conserve_lots=("L3",),
        cooperate=True,
        peer_buy=PeerBuy(16.0, 90),
        peer_sell=PeerSell(15.0, 60, 10),
    )
    parsed = parse_decision(good())
    assert isinstance(parsed.conserve_lots, tuple)
    assert isinstance(parsed.peer_buy.max_price, float)  # type: ignore[union-attr]


def test_parse_null_peers_and_extra_keys_ignored() -> None:
    d = parse_decision(good(peer_buy=None, peer_sell=None, other="x"))
    assert d.peer_buy is None and d.peer_sell is None
    d = parse_decision({k: v for k, v in good().items() if k != "room_id"})
    assert d.day == 5


def test_parse_boundaries_accepted() -> None:
    d = parse_decision(
        good(
            order_spot=0,
            order_futures=9999,
            conserve_lots=[f"L{i}" for i in range(30)],
            peer_buy={"max_price": 0.01, "min_quality": 0},
            peer_sell={"price": 10000, "max_quality": 100, "max_units": 9999},
        )
    )
    assert d.order_futures == 9999 and len(d.conserve_lots) == 30
    assert d.peer_buy == PeerBuy(0.01, 0)
    assert d.peer_sell == PeerSell(10000.0, 100, 9999)


def test_bool_not_accepted_as_int() -> None:
    assert rejected(order_spot=True) == MSG_UNREADABLE
    assert rejected(order_futures=False) == MSG_UNREADABLE
    assert rejected(day=True) == MSG_UNREADABLE


def test_float_order_rejected_not_truncated() -> None:
    assert rejected(order_spot=40.0) == MSG_UNREADABLE
    assert rejected(order_futures=1.5) == MSG_UNREADABLE
    assert rejected(day=5.0) == MSG_UNREADABLE


def test_tier_exact_match_only() -> None:
    for bad in ("EXW", "Exw", "CIP", " exw", "exw ", "fob", ""):
        assert rejected(incoterm_tier=bad) == MSG_TIER_UNAVAILABLE
    for ok in ("exw", "cip", "dap", "ddp"):
        assert parse_decision(good(incoterm_tier=ok)).incoterm_tier == IncotermTier(ok)
    assert rejected(incoterm_tier=1) == MSG_UNREADABLE
    assert rejected(incoterm_tier=None) == MSG_UNREADABLE


def test_truthy_cooperate_rejected() -> None:
    for bad in (1, 0, "yes", "true", None, [], {}):
        assert rejected(cooperate=bad) == MSG_UNREADABLE
    assert parse_decision(good(cooperate=False)).cooperate is False


def test_order_bounds() -> None:
    assert rejected(order_spot=10000) == "Orders must be whole numbers from 0 to 9,999."
    assert rejected(order_futures=-1) == msg_order_bounds()
    assert rejected(order_spot=-5) == msg_order_bounds()


def test_conserve_checks() -> None:
    assert rejected(conserve_lots=["L3", "L3"]) == MSG_CONSERVE_INVALID
    assert rejected(conserve_lots=[f"L{i}" for i in range(31)]) == (
        msg_conserve_too_many()
    )
    assert rejected(conserve_lots="L3") == MSG_UNREADABLE
    assert rejected(conserve_lots=("L3",)) == MSG_UNREADABLE
    assert rejected(conserve_lots=[3]) == MSG_UNREADABLE
    assert rejected(conserve_lots=None) == MSG_UNREADABLE
    assert parse_decision(good(conserve_lots=[])).conserve_lots == ()


def test_peer_object_must_be_exact() -> None:
    assert rejected(peer_buy={"max_price": 16}) == MSG_UNREADABLE
    assert rejected(peer_buy={"min_quality": 90}) == MSG_UNREADABLE
    assert (
        rejected(peer_buy={"max_price": 16, "min_quality": 90, "x": 1})
        == MSG_UNREADABLE
    )
    assert rejected(peer_buy={"max_price": 16, "min_quality": True}) == MSG_UNREADABLE
    assert rejected(peer_buy={"max_price": True, "min_quality": 90}) == MSG_UNREADABLE
    assert rejected(peer_buy={"max_price": "16", "min_quality": 90}) == MSG_UNREADABLE
    assert rejected(peer_buy={"max_price": 16, "min_quality": 90.0}) == MSG_UNREADABLE
    assert rejected(peer_buy=[16, 90]) == MSG_UNREADABLE
    sell = {"price": 15, "max_quality": 60, "max_units": 10}
    assert rejected(peer_sell={k: v for k, v in sell.items() if k != "price"}) == (
        MSG_UNREADABLE
    )
    assert rejected(peer_sell={**sell, "extra": 1}) == MSG_UNREADABLE
    assert rejected(peer_sell={**sell, "max_units": True}) == MSG_UNREADABLE
    assert rejected(peer_sell={**sell, "max_quality": True}) == MSG_UNREADABLE
    assert rejected(peer_sell={**sell, "price": False}) == MSG_UNREADABLE
    assert rejected(peer_sell={**sell, "max_units": 10.0}) == MSG_UNREADABLE


def test_missing_key_and_non_dict() -> None:
    for key in (
        "day",
        "order_spot",
        "order_futures",
        "incoterm_tier",
        "conserve_lots",
        "cooperate",
        "peer_buy",
        "peer_sell",
    ):
        data = good()
        del data[key]
        with pytest.raises(InvalidDecision) as info:
            parse_decision(data)
        assert info.value.message == MSG_UNREADABLE
    for junk in (None, [], "x", 5, [good()]):
        with pytest.raises(InvalidDecision) as info:
            parse_decision(junk)
        assert info.value.message == MSG_UNREADABLE


def test_peer_price_whole_cents_rejected_not_rounded() -> None:
    bounds = msg_peer_price_bounds()
    assert rejected(peer_buy={"max_price": 15.005, "min_quality": 90}) == bounds
    assert (
        rejected(peer_sell={"price": 0, "max_quality": 60, "max_units": 10}) == bounds
    )
    assert rejected(peer_buy={"max_price": 0.009, "min_quality": 1}) == bounds
    assert rejected(peer_buy={"max_price": -1, "min_quality": 1}) == bounds
    assert rejected(peer_buy={"max_price": 10000.01, "min_quality": 1}) == bounds
    assert rejected(peer_buy={"max_price": math.nan, "min_quality": 1}) == bounds
    assert rejected(peer_buy={"max_price": math.inf, "min_quality": 1}) == bounds
    assert (
        rejected(peer_sell={"price": 15.001, "max_quality": 1, "max_units": 1})
        == bounds
    )


def test_peer_price_float_noise_in_cents_is_accepted_and_rounded() -> None:
    d = parse_decision(
        good(peer_buy={"max_price": 0.1 + 0.2, "min_quality": 1}, peer_sell=None)
    )
    assert d.peer_buy is not None and d.peer_buy.max_price == 0.3
    d = parse_decision(
        good(
            peer_buy=None, peer_sell={"price": 19.99, "max_quality": 1, "max_units": 1}
        )
    )
    assert d.peer_sell is not None and d.peer_sell.price == 19.99


def test_quality_and_units_bounds() -> None:
    assert (
        rejected(peer_buy={"max_price": 16, "min_quality": 101}) == MSG_QUALITY_BOUNDS
    )
    assert rejected(peer_buy={"max_price": 16, "min_quality": -1}) == MSG_QUALITY_BOUNDS
    assert (
        rejected(peer_sell={"price": 15, "max_quality": 101, "max_units": 5})
        == MSG_QUALITY_BOUNDS
    )
    assert (
        rejected(peer_sell={"price": 15, "max_quality": 60, "max_units": 0})
        == "You can offer from 1 to 9,999 units."
    )
    assert (
        rejected(peer_sell={"price": 15, "max_quality": 60, "max_units": 10000})
        == msg_offer_units_bounds()
    )


def test_checks_run_in_order() -> None:
    """A payload failing two checks raises the earlier one's message."""
    bad_tier = "EXW"
    # 1 before 2
    assert rejected(order_spot=True, incoterm_tier=bad_tier) == MSG_UNREADABLE
    # 2 before 3
    assert rejected(incoterm_tier=bad_tier, order_spot=10000) == MSG_TIER_UNAVAILABLE
    # 3 before 4
    many = [f"L{i}" for i in range(31)]
    assert rejected(order_spot=10000, conserve_lots=many) == msg_order_bounds()
    # 4 before 5 (31 ids with a duplicate)
    assert rejected(conserve_lots=many + ["L0"]) == msg_conserve_too_many()
    # 5 before 6
    badprice = {"max_price": 15.005, "min_quality": 90}
    assert rejected(conserve_lots=["a", "a"], peer_buy=badprice) == MSG_CONSERVE_INVALID
    # 6 before 7
    both = {"max_price": 15.005, "min_quality": 101}
    assert rejected(peer_buy=both) == msg_peer_price_bounds()
    # 7 before 8
    sell = {"price": 15, "max_quality": 101, "max_units": 0}
    assert rejected(peer_buy=None, peer_sell=sell) == MSG_QUALITY_BOUNDS
    # 6 (sell price) before buy quality 7: price checks come first overall
    assert (
        rejected(
            peer_buy={"max_price": 16, "min_quality": 101},
            peer_sell={"price": 0, "max_quality": 60, "max_units": 10},
        )
        == msg_peer_price_bounds()
    )
    # 7 (sell quality) before 8 (buy ok)
    assert (
        rejected(
            peer_buy=None,
            peer_sell={"price": 15, "max_quality": -1, "max_units": 99999},
        )
        == MSG_QUALITY_BOUNDS
    )


# ---------------------------------------------------------------- Decision


def test_decision_zero() -> None:
    assert Decision.zero(1).to_payload() == {
        "day": 1,
        "order_spot": 0,
        "order_futures": 0,
        "incoterm_tier": "exw",
        "conserve_lots": [],
        "cooperate": False,
        "peer_buy": None,
        "peer_sell": None,
    }
    assert Decision.zero(3).day == 3


def test_decision_payload_round_trip() -> None:
    d = parse_decision(good())
    payload = d.to_payload()
    assert list(payload) == [
        "day",
        "order_spot",
        "order_futures",
        "incoterm_tier",
        "conserve_lots",
        "cooperate",
        "peer_buy",
        "peer_sell",
    ]
    assert payload["incoterm_tier"] == "cip" and payload["conserve_lots"] == ["L3"]
    assert payload["peer_buy"] == {"max_price": 16.0, "min_quality": 90}
    assert payload["peer_sell"] == {"price": 15.0, "max_quality": 60, "max_units": 10}
    assert json.loads(json.dumps(payload)) == payload
    assert Decision.from_payload(json.loads(json.dumps(payload))) == d
    z = Decision.zero(2)
    assert Decision.from_payload(z.to_payload()) == z
    # the stored payload also parses as untrusted wire input
    assert parse_decision(payload) == d


def test_peer_payloads() -> None:
    assert PeerBuy(16.0, 90).to_payload() == {"max_price": 16.0, "min_quality": 90}
    assert PeerSell(15.0, 60, 10).to_payload() == {
        "price": 15.0,
        "max_quality": 60,
        "max_units": 10,
    }
    assert PeerBuy.from_payload(PeerBuy(1.5, 2).to_payload()) == PeerBuy(1.5, 2)
    assert PeerSell.from_payload(PeerSell(1.5, 2, 3).to_payload()) == PeerSell(
        1.5, 2, 3
    )


def test_peer_trade_payload() -> None:
    t = PeerTrade(TradeSide.BUY, "P3", "B5-1", 10, 62, 15.0)
    assert t.to_payload() == {
        "side": "buy",
        "counterparty": "P3",
        "lot_id": "B5-1",
        "qty": 10,
        "quality": 62,
        "price": 15.0,
    }
    assert PeerTrade.from_payload(json.loads(json.dumps(t.to_payload()))) == t
    sell = PeerTrade(TradeSide.SELL, "P1", "L4", 3, 80, 12.25)
    assert PeerTrade.from_payload(sell.to_payload()) == sell
    assert sell.to_payload()["side"] == "sell"


# ---------------------------------------------------------------- Shipment


def test_shipment_round_trip() -> None:
    s = Shipment(
        qty=40, lost=5, unit_cost=12.4375, incoterm_tier=IncotermTier.DAP, shipped_day=3
    )
    p = s.to_payload()
    assert p["incoterm_tier"] == "dap"
    assert set(p) == {"qty", "lost", "unit_cost", "incoterm_tier", "shipped_day"}
    assert p["unit_cost"] == 12.4375
    assert Shipment.from_payload(json.loads(json.dumps(p))) == s


# ---------------------------------------------------------------- DayRecord

DAY_RECORD_FIELDS = [
    "day", "weekday",
    "order_spot", "order_futures", "incoterm_tier", "conserve_lots",
    "lots_conserved", "cooperate", "peer_buy", "peer_sell",
    "outlay",
    "opening_stock", "futures_due", "futures_filled", "futures_unfilled",
    "allocated_spot", "shortfall_spot", "shipped", "lost_transport", "delivered",
    "peer_bought", "peer_sold", "peer_trades",
    "demand", "backlog_open", "sold", "backlog_end", "spoiled", "closing_stock",
    "revenue_sales", "revenue_salvage", "revenue_peer",
    "refund_insurance", "refund_futures",
    "cost_spot", "cost_futures", "cost_peer", "cost_transport", "cost_insurance",
    "cost_conservation", "cost_holding", "cost_backlog",
    "profit", "cash_open", "cash_end", "futures_vs_spot",
    "fill", "waste", "reputation_open", "reputation_end",
    "market_share", "refrigeration_failure",
]  # fmt: skip


def make_record(with_peers: bool = True) -> DayRecord:
    """Every field gets a distinct, non-trivial value."""
    values: dict[str, Any] = {}
    n = 0
    for name in DAY_RECORD_FIELDS:
        n += 1
        values[name] = n + 0.37 if isinstance(n, int) else n
    ints = [
        "day", "order_spot", "order_futures", "lots_conserved", "opening_stock",
        "futures_due", "futures_filled", "futures_unfilled", "allocated_spot",
        "shortfall_spot", "shipped", "lost_transport", "delivered", "peer_bought",
        "peer_sold", "demand", "backlog_open", "sold", "backlog_end", "spoiled",
        "closing_stock",
    ]  # fmt: skip
    for name in ints:
        values[name] = int(values[name]) + 100
    values["weekday"] = Weekday.THU
    values["incoterm_tier"] = IncotermTier.DDP
    values["conserve_lots"] = ("L1", "L7")
    values["cooperate"] = True
    values["refrigeration_failure"] = True
    values["peer_buy"] = PeerBuy(16.25, 90) if with_peers else None
    values["peer_sell"] = PeerSell(15.5, 60, 12) if with_peers else None
    values["peer_trades"] = (
        PeerTrade(TradeSide.BUY, "P3", "B5-1", 10, 62, 15.0),
        PeerTrade(TradeSide.SELL, "P2", "L4", 4, 71, 15.5),
    )
    return DayRecord(**values)


def test_day_record_payload_keys_and_order() -> None:
    p = make_record().to_payload()
    assert list(p) == DAY_RECORD_FIELDS
    assert [f.name for f in dataclasses.fields(DayRecord)] == DAY_RECORD_FIELDS


def test_day_record_json_round_trip_every_field() -> None:
    for with_peers in (True, False):
        rec = make_record(with_peers)
        wire = json.loads(json.dumps(rec.to_payload()))
        back = DayRecord.from_payload(wire)
        assert back == rec
        for f in dataclasses.fields(DayRecord):
            assert getattr(back, f.name) == getattr(rec, f.name), f.name
        assert isinstance(back.conserve_lots, tuple)
        assert isinstance(back.peer_trades, tuple)
        assert back.weekday is Weekday.THU and back.incoterm_tier is IncotermTier.DDP
        assert len(back.peer_trades) == 2
        assert isinstance(back.peer_trades[0], PeerTrade)
        if not with_peers:
            assert back.peer_buy is None and back.peer_sell is None


def test_day_record_payload_forms() -> None:
    p = make_record().to_payload()
    assert p["weekday"] == "thu" and p["incoterm_tier"] == "ddp"
    assert p["conserve_lots"] == ["L1", "L7"]
    assert p["peer_buy"] == {"max_price": 16.25, "min_quality": 90}
    assert p["peer_trades"][0]["side"] == "buy"
    assert type(p["weekday"]) is str and type(p["incoterm_tier"]) is str
    q = make_record(False).to_payload()
    assert q["peer_buy"] is None and q["peer_sell"] is None


def test_day_record_floats_not_rounded() -> None:
    rec = dataclasses.replace(make_record(), profit=1.0049999, fill=0.123456789)
    p = rec.to_payload()
    assert p["profit"] == 1.0049999 and p["fill"] == 0.123456789


# ---------------------------------------------------------------- MarketDay


def make_market(avg: float | None = 14.5) -> MarketDay:
    return MarketDay(
        day=5,
        weekday=Weekday.FRI,
        weekday_multiplier=1.0,
        expected_demand=100.5,
        demand_total=97,
        capacity=81,
        disrupted=True,
        spot_price=13.4,
        cooperation=True,
        cooperators=4,
        refrigeration_failure=True,
        units_sold_total=90,
        peer_units=12,
        peer_avg_price=avg,
    )


def test_market_day_round_trip() -> None:
    for avg in (14.5, None):
        m = make_market(avg)
        p = m.to_payload()
        assert p["weekday"] == "fri" and p["peer_avg_price"] == avg
        assert MarketDay.from_payload(json.loads(json.dumps(p))) == m
    assert list(make_market().to_payload()) == [
        f.name for f in dataclasses.fields(MarketDay)
    ]
    assert [f.name for f in dataclasses.fields(MarketDay)] == [
        "day", "weekday", "weekday_multiplier", "expected_demand", "demand_total",
        "capacity", "disrupted", "spot_price", "cooperation", "cooperators",
        "refrigeration_failure", "units_sold_total", "peer_units", "peer_avg_price",
    ]  # fmt: skip


# ---------------------------------------------------------------- Public results


def make_public(avg: float | None = 14.5) -> PublicDayResults:
    return PublicDayResults(
        day=5,
        weekday=Weekday.FRI,
        demand_total=97,
        units_sold_total=90,
        spot_price=13.4,
        disrupted=False,
        cooperation=True,
        cooperators=4,
        refrigeration_failure=False,
        peer_units=12,
        peer_avg_price=avg,
        players={
            "P1": PublicPlayerDay(units_sold=50, market_share=0.5555, reputation=0.62),
            "P2": PublicPlayerDay(units_sold=40, market_share=0.4444, reputation=0.55),
        },
    )


def test_public_results_without_display_names() -> None:
    p = make_public().to_payload()
    assert p["weekday"] == "fri"
    assert p["players"]["P1"] == {
        "units_sold": 50,
        "market_share": 0.5555,
        "reputation": 0.62,
    }
    for entry in p["players"].values():
        assert "display_name" not in entry
    assert "display_name" not in make_public().to_payload(None)["players"]["P1"]


def test_public_results_with_display_names() -> None:
    names = {"P1": "Pulquería Uno", "P2": "La Tinaca"}
    p = make_public().to_payload(names)
    for alias, name in names.items():
        assert p["players"][alias]["display_name"] == name
        assert p["players"][alias]["units_sold"] in (50, 40)
    assert json.loads(json.dumps(p)) == p


def test_public_results_round_trip_ignores_display_name() -> None:
    for avg in (14.5, None):
        r = make_public(avg)
        plain = r.to_payload()
        assert PublicDayResults.from_payload(json.loads(json.dumps(plain))) == r
        named = r.to_payload({"P1": "A", "P2": "B"})
        assert PublicDayResults.from_payload(named) == r
        assert PublicDayResults.from_payload(named).players["P1"] == r.players["P1"]


def test_public_results_carry_no_capacity_and_load_snapshots_that_did() -> None:
    plain = make_public().to_payload()
    assert "capacity" not in plain
    older = {**plain, "capacity": 81}
    assert PublicDayResults.from_payload(older) == make_public()


def test_public_results_does_not_mutate_input_on_payload() -> None:
    r = make_public()
    before = copy.deepcopy(r.to_payload())
    r.to_payload({"P1": "A", "P2": "B"})
    assert r.to_payload() == before


# ---------------------------------------------------------------- FinalSettlement


def test_final_settlement_round_trip() -> None:
    f = FinalSettlement(
        liquidated_units=30,
        revenue_liquidation=45.5,
        refund_insurance_in_transit=12.25,
        futures_cancelled_units=8,
        refund_futures_cancelled=96.0,
        settlement_total=153.75,
    )
    p = f.to_payload()
    assert list(p) == [
        "liquidated_units",
        "revenue_liquidation",
        "refund_insurance_in_transit",
        "futures_cancelled_units",
        "refund_futures_cancelled",
        "settlement_total",
    ]
    assert FinalSettlement.from_payload(json.loads(json.dumps(p))) == f


# ---------------------------------------------------------------- frozen


def test_records_are_frozen() -> None:
    samples: list[tuple[object, str, Any]] = [
        (Decision.zero(1), "day", 2),
        (PeerBuy(1.0, 1), "max_price", 2.0),
        (PeerSell(1.0, 1, 1), "price", 2.0),
        (PeerTrade(TradeSide.BUY, "P1", "L1", 1, 1, 1.0), "qty", 2),
        (Shipment(1, 0, 1.0, IncotermTier.EXW, 1), "qty", 2),
        (make_record(), "profit", 0.0),
        (make_market(), "capacity", 1),
        (PublicPlayerDay(1, 0.5, 0.5), "units_sold", 2),
        (make_public(), "day", 9),
        (FinalSettlement(1, 1.0, 1.0, 1, 1.0, 1.0), "settlement_total", 0.0),
    ]
    for obj, name, value in samples:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, name, value)


def test_decision_field_order() -> None:
    assert [f.name for f in dataclasses.fields(Decision)] == [
        "day", "order_spot", "order_futures", "incoterm_tier", "conserve_lots",
        "cooperate", "peer_buy", "peer_sell",
    ]  # fmt: skip
    assert [f.name for f in dataclasses.fields(Shipment)] == [
        "qty", "lost", "unit_cost", "incoterm_tier", "shipped_day",
    ]  # fmt: skip
    assert [f.name for f in dataclasses.fields(PublicDayResults)] == [
        "day", "weekday", "demand_total", "units_sold_total", "spot_price",
        "disrupted", "cooperation", "cooperators",
        "refrigeration_failure", "peer_units", "peer_avg_price", "players",
    ]  # fmt: skip
