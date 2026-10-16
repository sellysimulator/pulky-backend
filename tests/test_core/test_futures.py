"""Black-box tests for app.core.futures (section 07)."""

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.futures import Contract, FuturesBook, fill_futures, make_contract
from app.core.supply import hamilton

FIELDS = {"contract_day", "ships_day", "delivery_day", "qty"}


def _book_3_4() -> FuturesBook:
    return FuturesBook([make_contract(3, 10, 7, 1), make_contract(4, 20, 7, 1)])


# --- make_contract ----------------------------------------------------------


def test_make_contract_worked_examples() -> None:
    assert make_contract(5, 25, 7, 1) == Contract(
        contract_day=5, ships_day=11, delivery_day=12, qty=25
    )
    c = make_contract(5, 25, 7, 0)
    assert (c.ships_day, c.delivery_day) == (12, 12)


@pytest.mark.parametrize(
    "qty,h,lead", [(25, 1, 1), (25, 3, 5), (0, 7, 1), (-3, 7, 1), (25, 7, 7)]
)
def test_make_contract_rejects_bad_input(qty: int, h: int, lead: int) -> None:
    with pytest.raises(ValueError):
        make_contract(5, qty, h, lead)


@given(
    day=st.integers(1, 100),
    qty=st.integers(1, 1000),
    h=st.integers(1, 30),
    data=st.data(),
)
def test_make_contract_day_ordering_and_failure_modes(
    day: int, qty: int, h: int, data: st.DataObject
) -> None:
    lead = data.draw(st.integers(0, h - 1))
    c = make_contract(day, qty, h, lead)
    # AC1 / failure mode 1: never ships on the day it is bought
    assert c.contract_day < c.ships_day <= c.delivery_day
    assert c.contract_day == day and c.qty == qty
    assert c.delivery_day == day + h
    # failure mode 2: it arrives on its delivery day, L days after shipping
    assert c.delivery_day - c.ships_day == lead
    assert c.ships_day == day + h - lead


def test_contract_payload_roundtrip() -> None:
    c = make_contract(5, 25, 7, 1)
    p = c.to_payload()
    assert set(p) == FIELDS
    assert p == {"contract_day": 5, "ships_day": 11, "delivery_day": 12, "qty": 25}
    assert Contract.from_payload(p) == c


# --- FuturesBook ------------------------------------------------------------


def test_book_worked_example() -> None:
    b = _book_3_4()
    assert (b.due(9), b.due(10), b.open_units()) == (10, 20, 30)
    assert b.due(11) == 0
    assert b.take_due(9) == 10
    assert b.open_units() == 20
    assert b.cancel_all() == 20
    assert b.open_units() == 0
    assert b.contracts == ()


def test_empty_book() -> None:
    b = FuturesBook()
    assert b.due(1) == 0 and b.take_due(1) == 0
    assert b.open_units() == 0 and b.cancel_all() == 0
    assert b.view() == [] and b.to_payload() == []


def test_due_sums_contracts_shipping_same_day() -> None:
    b = FuturesBook([make_contract(1, 5, 7, 1), make_contract(2, 8, 6, 0)])
    # ships_day 7 and 8 respectively
    assert b.due(7) == 5 and b.due(8) == 8
    b2 = FuturesBook([make_contract(1, 5, 7, 1), make_contract(2, 8, 7, 2)])
    assert b2.due(7) == 5 + 8
    assert b2.take_due(7) == 13
    assert b2.open_units() == 0


@given(
    specs=st.lists(
        st.tuples(st.integers(1, 40), st.integers(1, 50), st.integers(0, 5)),
        min_size=0,
        max_size=12,
        unique_by=lambda t: t[0],
    ),
    day=st.integers(1, 60),
)
def test_take_due_returns_exactly_due_and_removes(specs, day: int) -> None:
    b = FuturesBook([make_contract(d, q, 7, lead) for d, q, lead in specs])
    total = b.open_units()
    due = b.due(day)
    assert b.take_due(day) == due
    # failure mode 3: the contract is gone after shipping, filled or not
    assert b.due(day) == 0
    assert b.take_due(day) == 0
    assert b.open_units() == total - due
    assert all(c.ships_day != day for c in b.contracts)


def test_shipped_contract_does_not_ship_remainder_later() -> None:
    # failure mode 3: after take_due, nothing for this contract on any later day
    b = FuturesBook([make_contract(3, 10, 7, 1)])
    assert b.take_due(9) == 10
    assert all(b.due(d) == 0 for d in range(0, 40))
    assert b.open_units() == 0
    assert b.cancel_all() == 0


def test_add_orders_by_contract_day() -> None:
    cs = [make_contract(d, d, 7, 1) for d in (5, 2, 9, 1, 7)]
    b = FuturesBook()
    for c in cs:
        b.add(c)
    assert [c.contract_day for c in b.contracts] == [1, 2, 5, 7, 9]
    assert [p["contract_day"] for p in b.view()] == [1, 2, 5, 7, 9]
    b2 = FuturesBook(cs)
    assert [c.contract_day for c in b2.contracts] == [1, 2, 5, 7, 9]


def test_add_rejects_duplicate_contract_day() -> None:
    b = FuturesBook([make_contract(3, 10, 7, 1)])
    with pytest.raises(ValueError):
        b.add(make_contract(3, 4, 7, 1))
    assert b.open_units() == 10 and len(b.contracts) == 1


def test_payload_roundtrip_and_view() -> None:
    b = FuturesBook([make_contract(d, 3 * d, 7, 1) for d in (6, 2, 4)])
    p = b.to_payload()
    assert b.view() == p
    assert [x["contract_day"] for x in p] == [2, 4, 6]
    assert all(set(x) == FIELDS for x in p)
    b2 = FuturesBook.from_payload(p)
    assert b2.contracts == b.contracts
    assert b2.to_payload() == p
    assert FuturesBook.from_payload([]).contracts == ()


def test_view_is_a_snapshot() -> None:
    b = _book_3_4()
    v = b.view()
    b.cancel_all()
    assert len(v) == 2 and b.view() == []


# --- fill_futures -----------------------------------------------------------


def test_fill_worked_examples() -> None:
    assert fill_futures({"P1": 30, "P2": 20, "P3": 0}, 40, random.Random(0)) == {
        "P1": 24,
        "P2": 16,
        "P3": 0,
    }
    assert fill_futures({"P1": 25, "P2": 20}, 30, random.Random(0)) == {
        "P1": 17,
        "P2": 13,
    }
    assert fill_futures({"P1": 25, "P2": 20}, 90, random.Random(0)) == {
        "P1": 25,
        "P2": 20,
    }


def test_fill_no_draw_when_not_scarce() -> None:
    # failure mode 5
    rng = random.Random(123)
    before = rng.getstate()
    assert fill_futures({"P1": 25, "P2": 20}, 90, rng) == {"P1": 25, "P2": 20}
    assert rng.getstate() == before
    # exactly at capacity is also not scarce
    assert fill_futures({"P1": 25, "P2": 20}, 45, rng) == {"P1": 25, "P2": 20}
    assert rng.getstate() == before


def test_fill_scarce_consumes_the_stream() -> None:
    # sanity for the no-draw test: scarcity with a tie does draw
    rng = random.Random(5)
    before = rng.getstate()
    fill_futures({"P1": 1, "P2": 1}, 1, rng)
    assert rng.getstate() != before


@pytest.mark.parametrize("seed", range(10))
def test_fill_equals_hamilton(seed: int) -> None:
    due = {"P1": 7, "P2": 7, "P3": 7, "P4": 3}
    for cap in (0, 5, 10, 20, 24, 100):
        assert fill_futures(due, cap, random.Random(seed)) == hamilton(
            due, cap, random.Random(seed)
        )


def test_fill_never_exceeds_due_failure_mode() -> None:
    # failure mode 4: due 5 never receives 6
    out = fill_futures({"P1": 5, "P2": 1}, 6, random.Random(0))
    assert out == {"P1": 5, "P2": 1}
    out = fill_futures({"P1": 5, "P2": 5}, 9, random.Random(1))
    assert sum(out.values()) == 9 and all(v <= 5 for v in out.values())


@settings(max_examples=200, deadline=None)
@given(
    due=st.dictionaries(
        st.sampled_from(["P1", "P2", "P3", "P4", "P5", "P6"]),
        st.integers(0, 200),
        max_size=6,
    ),
    capacity=st.integers(0, 600),
    seed=st.integers(0, 10_000),
    perm_seed=st.integers(0, 10_000),
)
def test_fill_properties(due: dict, capacity: int, seed: int, perm_seed: int) -> None:
    fill = fill_futures(due, capacity, random.Random(seed))
    assert set(fill) == set(due)
    assert all(0 <= fill[a] <= due[a] for a in due)
    assert sum(fill.values()) == min(capacity, sum(due.values()))
    keys = list(due)
    random.Random(perm_seed).shuffle(keys)
    shuffled = {k: due[k] for k in keys}
    assert fill_futures(shuffled, capacity, random.Random(seed)) == fill
