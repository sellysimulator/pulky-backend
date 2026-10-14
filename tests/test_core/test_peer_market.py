"""Black-box tests for section 06 clear_peer_market (spec 5.7)."""

import copy
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.peer_market import Bid, Offer, OfferLot, Trade, clear_peer_market

BIG = 10**7


def _clear(bids, offers, aliases, seed=0):  # type: ignore[no-untyped-def]
    return clear_peer_market(bids, offers, aliases, random.Random(seed))


def _ref(bids, offers, aliases, rng):  # type: ignore[no-untyped-def]
    """Spec 5.7's code, verbatim."""
    tiebreak = {a: rng.random() for a in sorted(aliases)}
    lot_left = {(s, lot.lot_id): lot.qty for s, o in offers.items() for lot in o.lots}
    units_left = {s: o.max_units for s, o in offers.items()}
    trades = []
    for b in sorted(
        bids, key=lambda a: (-bids[a].max_price_cents, -bids[a].shortfall, tiebreak[a])
    ):
        bid = bids[b]
        need, cash = bid.shortfall, bid.cash_cents
        candidates = [
            (s, i, lot)
            for s, o in offers.items()
            if s != b
            for i, lot in enumerate(o.lots)
            if lot.quality >= bid.min_quality and o.price_cents <= bid.max_price_cents
        ]
        candidates.sort(
            key=lambda c: (
                -c[2].quality,
                offers[c[0]].price_cents,
                -c[2].days_left,
                tiebreak[c[0]],
                c[1],
            )
        )
        for s, _, lot in candidates:
            price = offers[s].price_cents
            take = min(need, lot_left[(s, lot.lot_id)], units_left[s], cash // price)
            if take == 0:
                continue
            lot_left[(s, lot.lot_id)] -= take
            units_left[s] -= take
            need -= take
            cash -= take * price
            trades.append(Trade(b, s, lot.lot_id, take, lot.quality, price))
            if need == 0:
                break
    return trades


# ---------------------------------------------------------------- examples


def test_spec_example() -> None:
    bids = {"P1": Bid(16, 1600, 50, BIG), "P2": Bid(12, 1400, 50, BIG)}
    offers = {
        "P3": Offer(1500, 20, (OfferLot("L3", 12, 23, 2), OfferLot("L4", 10, 62, 3))),
        "P4": Offer(1300, 30, (OfferLot("L4", 8, 55, 3),)),
    }
    assert _clear(bids, offers, ["P1", "P2", "P3", "P4"]) == [
        Trade("P1", "P3", "L4", 10, 62, 1500),
        Trade("P1", "P4", "L4", 6, 55, 1300),
        Trade("P2", "P4", "L4", 2, 55, 1300),
    ]


def _quality_offers() -> dict[str, Offer]:
    return {
        "P2": Offer(2000, 50, (OfferLot("L5", 5, 90, 4),)),
        "P3": Offer(1200, 50, (OfferLot("L4", 5, 60, 3),)),
        "P4": Offer(1500, 50, (OfferLot("L4", 5, 60, 3), OfferLot("B3-1", 5, 60, 5))),
    }


def test_quality_before_price_then_cheapest_then_days_left() -> None:
    """Failure mode 8: a price-first matcher buys P3's $12 units before P2's
    90% units."""
    got = _clear(
        {"P1": Bid(12, 2500, 50, BIG)}, _quality_offers(), ["P1", "P2", "P3", "P4"]
    )
    assert got == [
        Trade("P1", "P2", "L5", 5, 90, 2000),
        Trade("P1", "P3", "L4", 5, 60, 1200),
        Trade("P1", "P4", "B3-1", 2, 60, 1500),
    ]
    assert got[0].seller == "P2"


def test_cash_skips_unaffordable_offer_and_buys_cheaper() -> None:
    """Failure mode 9: a matcher that breaks at the first unaffordable offer
    buys nothing."""
    got = _clear(
        {"P1": Bid(5, 2000, 0, 1000)},
        {
            "P2": Offer(1500, 9, (OfferLot("L4", 9, 80, 3),)),
            "P3": Offer(900, 9, (OfferLot("L3", 9, 40, 2),)),
        },
        ["P1", "P2", "P3"],
    )
    assert got == [Trade("P1", "P3", "L3", 1, 40, 900)]


def test_buyer_order_larger_shortfall_first_and_never_from_itself() -> None:
    got = _clear(
        {"P1": Bid(3, 1500, 0, BIG), "P2": Bid(5, 1500, 0, BIG)},
        {"P1": Offer(1000, 4, (OfferLot("L2", 4, 50, 2),))},
        ["P1", "P2"],
    )
    assert got == [Trade("P2", "P1", "L2", 4, 50, 1000)]


def test_higher_max_price_served_first_regardless_of_insertion_order() -> None:
    """Failure mode 7: join-order buyers."""
    offers = {"P3": Offer(1000, 5, (OfferLot("L1", 5, 70, 2),))}
    aliases = ["P1", "P2", "P3"]
    lo = Bid(5, 1000, 0, BIG)
    hi = Bid(5, 1500, 0, BIG)
    for bids in ({"P1": lo, "P2": hi}, {"P2": hi, "P1": lo}):
        assert _clear(bids, offers, aliases) == [Trade("P2", "P3", "L1", 5, 70, 1000)]
    # and the larger shortfall does not beat a higher cap
    for bids in (
        {"P1": Bid(9, 1000, 0, BIG), "P2": Bid(2, 1500, 0, BIG)},
        {"P2": Bid(2, 1500, 0, BIG), "P1": Bid(9, 1000, 0, BIG)},
    ):
        got = _clear(bids, offers, aliases)
        assert got == [
            Trade("P2", "P3", "L1", 2, 70, 1000),
            Trade("P1", "P3", "L1", 3, 70, 1000),
        ]


def test_equal_budget_ties_use_the_seeded_draw() -> None:
    offers = {"P3": Offer(1000, 5, (OfferLot("L1", 5, 70, 2),))}
    bids = {"P1": Bid(5, 1500, 0, BIG), "P2": Bid(5, 1500, 0, BIG)}
    winners = set()
    for seed in range(30):
        a = _clear(bids, offers, ["P1", "P2", "P3"], seed)
        b = _clear(dict(reversed(list(bids.items()))), offers, ["P3", "P2", "P1"], seed)
        assert a == b
        r = random.Random(seed)
        d = {k: r.random() for k in ("P1", "P2", "P3")}
        want = "P1" if d["P1"] < d["P2"] else "P2"
        assert [t.buyer for t in a] == [want]
        winners.add(want)
    assert winners == {"P1", "P2"}


def test_seller_cap_applies_across_buyers() -> None:
    """Failure mode 10: max_units 5, two buyers wanting 4: the second gets 1."""
    offers = {"P3": Offer(1000, 5, (OfferLot("L1", 10, 70, 2),))}
    bids = {"P1": Bid(4, 2000, 0, BIG), "P2": Bid(4, 1500, 0, BIG)}
    for b in (bids, dict(reversed(list(bids.items())))):
        got = _clear(b, offers, ["P1", "P2", "P3"])
        assert got == [
            Trade("P1", "P3", "L1", 4, 70, 1000),
            Trade("P2", "P3", "L1", 1, 70, 1000),
        ]


def test_seller_cap_spans_lots_of_one_seller() -> None:
    offers = {
        "P3": Offer(1000, 6, (OfferLot("L1", 4, 80, 2), OfferLot("L2", 4, 70, 2)))
    }
    got = _clear({"P1": Bid(9, 1000, 0, BIG)}, offers, ["P1", "P3"])
    assert got == [
        Trade("P1", "P3", "L1", 4, 80, 1000),
        Trade("P1", "P3", "L2", 2, 70, 1000),
    ]


def test_self_trade_never_happens() -> None:
    """Failure mode 11: P1 offers the best and cheapest lot and also buys."""
    bids = {"P1": Bid(5, 3000, 0, BIG)}
    offers = {
        "P1": Offer(100, 50, (OfferLot("L1", 50, 100, 5),)),
        "P2": Offer(2000, 50, (OfferLot("L2", 50, 30, 1),)),
    }
    got = _clear(bids, offers, ["P1", "P2"])
    assert got == [Trade("P1", "P2", "L2", 5, 30, 2000)]
    assert all(t.buyer != t.seller for t in got)
    only_self = _clear(bids, {"P1": offers["P1"]}, ["P1", "P2"])
    assert only_self == []


def test_zero_cash_buys_nothing_and_cash_caps_units() -> None:
    """Failure mode 12 (spec 14 #23)."""
    offers = {"P2": Offer(1, 100, (OfferLot("L1", 100, 90, 3),))}
    assert _clear({"P1": Bid(10, 5000, 0, 0)}, offers, ["P1", "P2"]) == []
    got = _clear(
        {"P1": Bid(10, 5000, 0, 7)},
        {"P2": Offer(3, 100, offers["P2"].lots)},
        ["P1", "P2"],
    )
    assert got == [Trade("P1", "P2", "L1", 2, 90, 3)]
    got = _clear(
        {"P1": Bid(10, 5000, 0, 2999)},
        {"P2": Offer(300, 100, offers["P2"].lots)},
        ["P1", "P2"],
    )
    assert sum(t.qty for t in got) == 9


def test_limits_min_quality_and_max_price_respected() -> None:
    offers = {
        "P2": Offer(1000, 9, (OfferLot("L1", 9, 49, 2), OfferLot("L2", 9, 50, 2))),
        "P3": Offer(1001, 9, (OfferLot("L3", 9, 99, 2),)),
    }
    got = _clear({"P1": Bid(20, 1000, 50, BIG)}, offers, ["P1", "P2", "P3"])
    assert got == [Trade("P1", "P2", "L2", 9, 50, 1000)]


def test_empty_cases_still_consume_draws() -> None:
    aliases = ["P3", "P1", "P2"]
    offers = {"P2": Offer(100, 5, (OfferLot("L1", 5, 50, 2),))}
    lotless = {"P2": Offer(100, 5, ())}
    bids = {"P1": Bid(5, 500, 0, BIG)}
    cases = [
        ({}, {}),
        (bids, {}),
        ({}, offers),
        (bids, lotless),
        ({"P1": Bid(5, 500, 99, BIG)}, offers),
        ({"P1": Bid(5, 500, 0, 0)}, offers),
    ]
    for b, o in cases:
        r = random.Random(3)
        assert clear_peer_market(b, o, aliases, r) == []
        ref = random.Random(3)
        for _ in aliases:
            ref.random()
        assert r.random() == ref.random()


def test_does_not_mutate_inputs() -> None:
    bids = {"P1": Bid(16, 1600, 50, BIG), "P2": Bid(12, 1400, 50, BIG)}
    offers = {
        "P3": Offer(1500, 20, (OfferLot("L3", 12, 23, 2), OfferLot("L4", 10, 62, 3))),
        "P4": Offer(1300, 30, (OfferLot("L4", 8, 55, 3),)),
    }
    aliases = ["P4", "P3", "P2", "P1"]
    b0, o0, a0 = copy.deepcopy(bids), copy.deepcopy(offers), list(aliases)
    first = _clear(bids, offers, aliases)
    assert (bids, offers, aliases) == (b0, o0, a0)
    assert _clear(bids, offers, aliases) == first


# ---------------------------------------------------------------- validation

BAD_BIDS = [
    lambda: Bid(0, 100, 0, 0),
    lambda: Bid(-1, 100, 0, 0),
    lambda: Bid(1, 0, 0, 0),
    lambda: Bid(1, 100, -1, 0),
    lambda: Bid(1, 100, 101, 0),
    lambda: Bid(1, 100, 0, -1),
]
BAD_OFFERS = [
    lambda: Offer(0, 1, ()),
    lambda: Offer(100, 0, ()),
    lambda: Offer(100, 1, (OfferLot("L", 0, 50, 1),)),
    lambda: Offer(100, 1, (OfferLot("L", 1, 101, 1),)),
    lambda: Offer(100, 1, (OfferLot("L", 1, -1, 1),)),
    lambda: Offer(100, 1, (OfferLot("L", 1, 50, 0),)),
]


@pytest.mark.parametrize("make", BAD_BIDS)
def test_bad_bid_raises_value_error(make) -> None:  # type: ignore[no-untyped-def]
    offers = {"P2": Offer(100, 5, (OfferLot("L1", 5, 50, 2),))}
    with pytest.raises(ValueError):
        clear_peer_market({"P1": make()}, offers, ["P1", "P2"], random.Random(0))


@pytest.mark.parametrize("make", BAD_OFFERS)
def test_bad_offer_raises_value_error(make) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        clear_peer_market(
            {"P1": Bid(1, 100, 0, BIG)}, {"P2": make()}, ["P1", "P2"], random.Random(0)
        )


def test_key_missing_from_aliases_raises_value_error() -> None:
    bid = Bid(1, 100, 0, BIG)
    offer = Offer(100, 5, (OfferLot("L1", 5, 50, 2),))
    with pytest.raises(ValueError):
        clear_peer_market({"P9": bid}, {"P2": offer}, ["P1", "P2"], random.Random(0))
    with pytest.raises(ValueError):
        clear_peer_market({"P1": bid}, {"P9": offer}, ["P1", "P2"], random.Random(0))


def test_offer_without_lots_is_valid() -> None:
    assert (
        _clear({"P1": Bid(1, 100, 0, BIG)}, {"P2": Offer(100, 5, ())}, ["P1", "P2"])
        == []
    )


# ---------------------------------------------------------------- properties


@st.composite
def markets(draw):  # type: ignore[no-untyped-def]
    n = draw(st.integers(1, 12))
    aliases = [f"P{i}" for i in range(1, n + 1)]
    bids: dict[str, Bid] = {}
    offers: dict[str, Offer] = {}
    for a in aliases:
        if draw(st.booleans()):
            bids[a] = Bid(
                draw(st.integers(1, 40)),
                draw(st.integers(1, 40)) * 50,
                draw(st.integers(0, 100)),
                draw(st.integers(0, 6000)),
            )
        if draw(st.booleans()):
            nl = draw(st.integers(0, 3))
            lots = tuple(
                OfferLot(
                    f"L{j}",
                    draw(st.integers(1, 25)),
                    draw(st.integers(0, 100)),
                    draw(st.integers(1, 6)),
                )
                for j in range(nl)
            )
            offers[a] = Offer(
                draw(st.integers(1, 40)) * 50, draw(st.integers(1, 40)), lots
            )
    items_b = list(bids.items())
    items_o = list(offers.items())
    return aliases, items_b, items_o, draw(st.integers(0, 10**6))


@settings(max_examples=300, deadline=None)
@given(markets())
def test_property_invariants_and_greedy_optimality(m) -> None:  # type: ignore[no-untyped-def]
    aliases, items_b, items_o, seed = m
    bids, offers = dict(items_b), dict(items_o)
    trades = clear_peer_market(bids, offers, aliases, random.Random(seed))
    bought: dict[str, int] = {}
    spent: dict[str, int] = {}
    sold: dict[str, int] = {}
    lot_taken: dict[tuple[str, str], int] = {}
    for t in trades:
        bid, offer = bids[t.buyer], offers[t.seller]
        assert t.qty >= 1
        assert t.buyer != t.seller
        assert t.quality >= bid.min_quality
        assert t.price_cents <= bid.max_price_cents
        assert t.price_cents == offer.price_cents
        lot = next(x for x in offer.lots if x.lot_id == t.seller_lot)
        assert lot.quality == t.quality
        bought[t.buyer] = bought.get(t.buyer, 0) + t.qty
        spent[t.buyer] = spent.get(t.buyer, 0) + t.qty * t.price_cents
        sold[t.seller] = sold.get(t.seller, 0) + t.qty
        key = (t.seller, t.seller_lot)
        lot_taken[key] = lot_taken.get(key, 0) + t.qty
    for b, q in bought.items():
        assert q <= bids[b].shortfall
        assert spent[b] <= bids[b].cash_cents
    for s, q in sold.items():
        assert q <= offers[s].max_units
    for (s, lid), q in lot_taken.items():
        lot = next(x for x in offers[s].lots if x.lot_id == lid)
        assert q <= lot.qty
    # one trade per (buyer, seller lot)
    assert len({(t.buyer, t.seller, t.seller_lot) for t in trades}) == len(trades)
    # greedy optimality
    for b, bid in bids.items():
        need = bid.shortfall - bought.get(b, 0)
        cash = bid.cash_cents - spent.get(b, 0)
        if need <= 0:
            continue
        for s, o in offers.items():
            if s == b or sold.get(s, 0) >= o.max_units:
                continue
            for lot in o.lots:
                if lot.quality < bid.min_quality or o.price_cents > bid.max_price_cents:
                    continue
                if lot_taken.get((s, lot.lot_id), 0) >= lot.qty:
                    continue
                assert cash < o.price_cents


@settings(max_examples=300, deadline=None)
@given(markets(), st.randoms())
def test_property_order_independence_draw_count_and_reference(m, shuffler) -> None:  # type: ignore[no-untyped-def]
    aliases, items_b, items_o, seed = m
    bids, offers = dict(items_b), dict(items_o)
    base_rng = random.Random(seed)
    base = clear_peer_market(bids, offers, aliases, base_rng)

    sb, so, sa = list(items_b), list(items_o), list(aliases)
    shuffler.shuffle(sb)
    shuffler.shuffle(so)
    shuffler.shuffle(sa)
    rng2 = random.Random(seed)
    assert clear_peer_market(dict(sb), dict(so), sa, rng2) == base

    ref_rng = random.Random(seed)
    for _ in aliases:
        ref_rng.random()
    nxt = ref_rng.random()
    assert base_rng.random() == nxt
    assert rng2.random() == nxt

    assert base == _ref(bids, offers, aliases, random.Random(seed))
