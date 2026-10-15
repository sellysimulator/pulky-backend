"""Black-box tests for section 05 (stock): ``app/core/lots.py``."""

from __future__ import annotations

import json
import math
import random
from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.lots import Cellar, Lot, days_left, hazard, quality, quality_guide

THIRD = 1 / 3


def mk(
    lot_id: str = "L1",
    qty: int = 10,
    age: int = 0,
    unit_cost: float = 12.0,
    q0: int = 100,
    fresh: int = 1,
    max_days: int = 4,
    conserved: bool = False,
) -> Lot:
    return Lot(lot_id, qty, age, unit_cost, q0, fresh, max_days, conserved)


def ids(cellar: Cellar) -> list[str]:
    return [lot.id for lot in cellar.lots]


def ref_guide(day: int, fresh: int, max_days: int) -> list[dict]:
    """Spec 3.5's quality guide, written out from the text."""
    r = max_days - fresh
    rows = []
    for q in range(100, -1, -5):
        survival = 1.0
        j = 0
        half_j = None
        while half_j is None:
            j += 1
            qj = max(0.0, q - (j - 1) * 100 / r)
            survival *= qj / 100
            if survival <= 0.5 + 1e-9:
                half_j = j
        k = (q * r + 99) // 100 + 1
        rows.append(
            {
                "quality": q,
                "half_spoiled_day": day + half_j - 1,
                "last_day": day + k - 1,
            }
        )
    return rows


# --------------------------------------------------------------- hazard (AC 1)


def test_hazard_plain_table() -> None:
    got = [hazard(a, 1, 4) for a in (1, 2, 3, 4, 5)]
    assert got[0] == 0.0
    assert got[1] == pytest.approx(THIRD, abs=1e-9)
    assert got[2] == pytest.approx(2 * THIRD, abs=1e-9)
    assert got[3] == 1.0
    assert got[4] == 1.0


def test_hazard_conserved_table() -> None:
    got = [hazard(a, 3, 6) for a in (1, 2, 3, 4, 5, 6)]
    assert got[:3] == [0.0, 0.0, 0.0]
    assert got[3] == pytest.approx(THIRD, abs=1e-9)
    assert got[4] == pytest.approx(2 * THIRD, abs=1e-9)
    assert got[5] == 1.0


def test_hazard_zero_fresh_days() -> None:
    assert hazard(0, 0, 3) == 0.0


def test_hazard_default_initial_quality_is_100() -> None:
    for a in range(0, 6):
        assert hazard(a, 1, 4) == hazard(a, 1, 4, 100)


def test_hazard_80_percent_table() -> None:
    got = [hazard(a, 1, 4, 80) for a in (1, 2, 3, 4)]
    assert got[0] == pytest.approx(0.2, abs=1e-9)
    assert got[1] == pytest.approx(0.2 + THIRD, abs=1e-9)
    assert got[2] == pytest.approx(0.2 + 2 * THIRD, abs=1e-9)
    assert got[3] == 1.0


def test_hazard_60_percent_table_reaches_exactly_one_early() -> None:
    got = [hazard(a, 1, 4, 60) for a in (1, 2, 3)]
    assert got[0] == pytest.approx(0.4, abs=1e-9)
    assert got[1] == pytest.approx(0.4 + THIRD, abs=1e-9)
    assert got[2] == 1.0
    assert hazard(3, 1, 4, 60) == 1.0


def test_hazard_base_risk_holds_through_fresh_period() -> None:
    assert hazard(0, 1, 4, 90) == pytest.approx(0.1, abs=1e-9)
    assert hazard(1, 1, 4, 90) == pytest.approx(0.1, abs=1e-9)


@settings(max_examples=300, deadline=None, derandomize=True)
@given(
    fresh=st.integers(0, 6),
    ramp=st.integers(1, 8),
    q0=st.integers(0, 100),
    age=st.integers(0, 20),
)
def test_hazard_exact_cases_and_bounds(fresh: int, ramp: int, q0: int, age: int):
    max_days = fresh + ramp
    h = hazard(age, fresh, max_days, q0)
    assert 0.0 <= h <= 1.0
    k = max(0, age - fresh)
    if 100 * k >= q0 * ramp:
        assert h == 1.0
    if q0 == 100 and age <= fresh:
        assert h == 0.0
    assert hazard(age + 1, fresh, max_days, q0) >= h


# ------------------------------------------- quality / days_left / guide (AC 1)


def test_quality_and_days_left_spec_example() -> None:
    cases = ((2, 90), (1, 95), (0, 85))
    assert [quality(a, 1, 4, q) for a, q in cases] == [23, 62, 85]
    assert [days_left(a, 1, 4, q) for a, q in cases] == [2, 3, 4]


def test_quality_conserved_lot_is_back_to_full() -> None:
    assert quality(1, 1, 4, 100) == 67
    assert quality(2, 3, 6, 100) == 100


def test_quality_never_exceeds_initial_after_conservation() -> None:
    assert quality(2, 3, 6, 90) == 90


@settings(max_examples=300, deadline=None, derandomize=True)
@given(
    fresh=st.integers(0, 5),
    ramp=st.integers(1, 8),
    q0=st.integers(1, 100),
    data=st.data(),
)
def test_quality_and_days_left_follow_integer_formulas(
    fresh: int, ramp: int, q0: int, data: st.DataObject
):
    max_days = fresh + ramp
    age = data.draw(st.integers(0, max_days - 1))
    k = max(0, age + 1 - fresh)
    expected_q = max(0, (2 * ramp * q0 - 200 * k + ramp) // (2 * ramp))
    assert quality(age, fresh, max_days, q0) == expected_q
    a_star = max(age + 1, fresh + (q0 * ramp + 99) // 100)
    assert days_left(age, fresh, max_days, q0) == a_star - age
    assert hazard(a_star, fresh, max_days, q0) == 1.0
    for a in range(age + 1, a_star):
        assert hazard(a, fresh, max_days, q0) < 1.0


def test_quality_guide_spec_values() -> None:
    g = quality_guide(5, 1, 4)
    assert len(g) == 21
    assert g[0] == {"quality": 100, "half_spoiled_day": 7, "last_day": 8}
    assert g[2] == {"quality": 90, "half_spoiled_day": 7, "last_day": 8}
    assert g[10] == {"quality": 50, "half_spoiled_day": 5, "last_day": 7}
    assert g[20] == {"quality": 0, "half_spoiled_day": 5, "last_day": 5}


@pytest.mark.parametrize(
    ("day", "fresh", "max_days"),
    [(5, 1, 4), (1, 1, 4), (12, 0, 5), (3, 2, 3), (7, 1, 9), (2, 3, 6)],
)
def test_quality_guide_matches_spec_formula(day: int, fresh: int, max_days: int):
    g = quality_guide(day, fresh, max_days)
    assert [row["quality"] for row in g] == list(range(100, -1, -5))
    assert all(set(row) == {"quality", "half_spoiled_day", "last_day"} for row in g)
    assert g == ref_guide(day, fresh, max_days)


def test_quality_guide_depends_on_ramp_length_only() -> None:
    assert quality_guide(5, 1, 4) == quality_guide(5, 3, 6)


# --------------------------------------------------------------- Lot methods


def test_lot_methods_delegate_to_formulas() -> None:
    lot = mk(age=2, q0=90)
    assert lot.risk_tonight() == hazard(3, 1, 4, 90)
    assert lot.quality() == 23
    assert lot.days_left() == 2


def test_lot_payload_round_trip() -> None:
    lot = mk("B5-1", 7, 2, 13.4, 85, 3, 6, True)
    payload = lot.to_payload()
    assert set(payload) == {
        "id",
        "qty",
        "age",
        "unit_cost",
        "initial_quality",
        "fresh_days",
        "max_days",
        "conserved",
    }
    assert Lot.from_payload(json.loads(json.dumps(payload))) == lot


# ------------------------------------------------- add / insert / take (AC 8a)


def test_add_appends_newest_last_and_total() -> None:
    c = Cellar()
    c.add(mk("L0", 5, age=1))
    c.add(mk("L1", 7, age=0))
    assert ids(c) == ["L0", "L1"]
    assert c.total() == 12
    assert c.get("L1") is not None
    assert c.get("nope") is None


def test_lots_view_is_a_tuple() -> None:
    assert isinstance(Cellar([mk()]).lots, tuple)


@pytest.mark.parametrize("method", ["add", "insert"])
@pytest.mark.parametrize("bad_qty", [0, -3])
def test_add_insert_reject_non_positive_qty(method: str, bad_qty: int) -> None:
    c = Cellar([mk("L0", 5)])
    before = c.to_payload()
    with pytest.raises(ValueError):
        getattr(c, method)(mk("L9", bad_qty))
    assert c.to_payload() == before


@pytest.mark.parametrize("method", ["add", "insert"])
def test_add_insert_reject_duplicate_id(method: str) -> None:
    c = Cellar([mk("L0", 5)])
    before = c.to_payload()
    with pytest.raises(ValueError):
        getattr(c, method)(mk("L0", 3))
    assert c.to_payload() == before


def test_insert_between_equal_age_and_younger() -> None:
    """Failure mode 11: a bought lot of age 1 goes between L4 and L5."""
    c = Cellar([mk("L4", 5, age=1), mk("L5", 8, age=0)])
    c.insert(mk("B5-1", 3, age=1))
    assert ids(c) == ["L4", "B5-1", "L5"]


def test_insert_spec_example_with_take() -> None:
    seller = Cellar([mk("L4", 10, 1, 12.5, 95), mk("L5", 30, 0, 13.0, 85)])
    part = seller.take("L4", 10)
    assert part == mk("L4", 10, 1, 12.5, 95)
    assert seller.get("L4") is None
    buyer = Cellar([mk("L4", 5, 1, 12.0, 90), mk("L5", 8, 0, 13.0, 88)])
    buyer.insert(replace(part, id="B5-1", unit_cost=15.0))
    assert ids(buyer) == ["L4", "B5-1", "L5"]


def test_insert_positions_by_age() -> None:
    c = Cellar([mk("A", 1, age=3), mk("B", 1, age=1), mk("C", 1, age=0)])
    c.insert(mk("B5-1", 1, age=1))
    c.insert(mk("B5-2", 1, age=1))
    assert ids(c) == ["A", "B", "B5-1", "B5-2", "C"]
    c.insert(mk("OLD", 1, age=9))
    c.insert(mk("NEW", 1, age=0))
    assert ids(c) == ["OLD", "A", "B", "B5-1", "B5-2", "C", "NEW"]
    ages = [lot.age for lot in c.lots]
    assert ages == sorted(ages, reverse=True)


def test_insert_into_empty_cellar() -> None:
    c = Cellar()
    c.insert(mk("B1-1", 2, age=2))
    assert ids(c) == ["B1-1"]


def test_take_partial_keeps_remainder_and_other_fields() -> None:
    c = Cellar([mk("L3", 12, 2, 13.0, 90, 3, 6, True)])
    part = c.take("L3", 5)
    assert part == mk("L3", 5, 2, 13.0, 90, 3, 6, True)
    left = c.get("L3")
    assert left == mk("L3", 7, 2, 13.0, 90, 3, 6, True)
    part.qty = 99
    assert c.total() == 7


def test_take_whole_lot_drops_it() -> None:
    c = Cellar([mk("L3", 12), mk("L4", 4)])
    part = c.take("L3", 12)
    assert part.qty == 12
    assert ids(c) == ["L4"]
    assert c.total() == 4


@pytest.mark.parametrize(
    ("lot_id", "qty"), [("nope", 1), ("L3", 0), ("L3", -1), ("L3", 13)]
)
def test_take_is_all_or_nothing(lot_id: str, qty: int) -> None:
    c = Cellar([mk("L3", 12), mk("L4", 4)])
    before = c.to_payload()
    with pytest.raises(ValueError):
        c.take(lot_id, qty)
    assert c.to_payload() == before


# ------------------------------------------------------------ consume (AC 2, 3)


def test_consume_is_fifo_spec_example() -> None:
    """Failure mode 1: LIFO selling would leave L0 with 20 units and L1 with 5."""
    c = Cellar([mk("L0", 20, 1, 12.0, 100), mk("L1", 30, 0, 13.4, 90)])
    c.consume(25)
    assert [(lot.id, lot.qty) for lot in c.lots] == [("L1", 25)]


def test_consume_partial_first_lot_and_exact_drain() -> None:
    c = Cellar([mk("L0", 20, 2), mk("L1", 30, 1), mk("L2", 5, 0)])
    c.consume(7)
    assert [(lot.id, lot.qty) for lot in c.lots] == [("L0", 13), ("L1", 30), ("L2", 5)]
    c.consume(13 + 30)
    assert [(lot.id, lot.qty) for lot in c.lots] == [("L2", 5)]
    c.consume(5)
    assert c.lots == ()
    assert c.total() == 0


def test_consume_zero_is_noop() -> None:
    c = Cellar([mk("L0", 20)])
    before = c.to_payload()
    c.consume(0)
    assert c.to_payload() == before


@pytest.mark.parametrize("qty", [-1, 36])
def test_consume_invalid_raises_and_changes_nothing(qty: int) -> None:
    c = Cellar([mk("L0", 20, 1), mk("L1", 15, 0)])
    before = c.to_payload()
    with pytest.raises(ValueError):
        c.consume(qty)
    assert c.to_payload() == before


@settings(max_examples=200, deadline=None, derandomize=True)
@given(
    quantities=st.lists(st.integers(1, 40), min_size=1, max_size=6),
    data=st.data(),
)
def test_consume_property_fifo_and_total(quantities: list[int], data: st.DataObject):
    lots = [mk(f"L{i}", q, age=len(quantities) - i) for i, q in enumerate(quantities)]
    total = sum(quantities)
    take = data.draw(st.integers(0, total))
    c = Cellar(lots)
    c.consume(take)
    assert c.total() == total - take
    remaining = take
    expected = []
    for i, qty in enumerate(quantities):
        used = min(qty, remaining)
        remaining -= used
        if qty - used > 0:
            expected.append((f"L{i}", qty - used))
    assert [(lot.id, lot.qty) for lot in c.lots] == expected


# ---------------------------------------------------------- conserve (AC 4, 5)


def test_conserve_spec_example() -> None:
    c = Cellar([mk("L3", 12, 2, 13.0), mk("L4", 30, 1, 12.5)])
    assert c.conserve(["L3"], 2) == 12
    lot = c.get("L3")
    assert lot is not None
    assert (lot.fresh_days, lot.max_days, lot.conserved) == (3, 6, True)
    other = c.get("L4")
    assert other is not None
    assert (other.fresh_days, other.max_days, other.conserved) == (1, 4, False)
    view = c.view(5)
    assert view[0]["risk_tonight"] == 0.0
    assert view[0]["quality"] == 100
    assert view[1]["risk_tonight"] == 0.3333
    assert view[1]["quality"] == 67
    assert view[1]["last_day"] == 7
    assert c.expected_spoil_tonight() == 10.0


def test_conserve_returns_sum_of_qty_of_named_lots() -> None:
    c = Cellar([mk("L1", 3, 2), mk("L2", 4, 1), mk("L3", 5, 0)])
    assert c.conserve(["L1", "L3"], 2) == 8
    assert c.get("L2") is not None and not c.get("L2").conserved  # type: ignore


def test_conserve_empty_list_returns_zero_and_changes_nothing() -> None:
    c = Cellar([mk("L1", 3)])
    before = c.to_payload()
    assert c.conserve([], 2) == 0
    assert c.to_payload() == before


def test_conserve_keeps_ramp_length() -> None:
    """Failure mode 3: extending only fresh_days (or only max_days) breaks this."""
    c = Cellar([mk("L3", 12, 3)])
    c.conserve(["L3"], 2)
    lot = c.get("L3")
    assert lot is not None
    assert lot.max_days - lot.fresh_days == 3
    # spec 3.4 table: after the two extra safe nights, 33% -> 67% -> 100%
    risks = [replace(lot, age=a).risk_tonight() for a in (2, 3, 4, 5)]
    assert risks[0] == 0.0
    assert risks[1] == pytest.approx(THIRD, abs=1e-9)
    assert risks[2] == pytest.approx(2 * THIRD, abs=1e-9)
    assert risks[3] == 1.0
    # an age-3 plain lot would be certain to spoil tonight; conserved it is 1/3
    assert lot.risk_tonight() == pytest.approx(THIRD, abs=1e-9)


def test_conserve_partial_failure_changes_nothing() -> None:
    """Failure mode 4."""
    c = Cellar([mk("L3", 12, 2), mk("L4", 5, 1)])
    before = c.to_payload()
    with pytest.raises(ValueError):
        c.conserve(["L3", "nope"], 2)
    assert c.to_payload() == before
    lot = c.get("L3")
    assert lot is not None and lot.conserved is False
    assert (lot.fresh_days, lot.max_days) == (1, 4)
    with pytest.raises(ValueError):
        c.conserve(["nope", "L3"], 2)
    assert c.to_payload() == before


def test_conserved_lot_cannot_be_conserved_again() -> None:
    c = Cellar([mk("L3", 12, 2), mk("L4", 5, 1)])
    c.conserve(["L3"], 2)
    before = c.to_payload()
    with pytest.raises(ValueError):
        c.conserve(["L3"], 2)
    with pytest.raises(ValueError):
        c.conserve(["L4", "L3"], 2)
    assert c.to_payload() == before


def test_conserve_never_lifts_above_initial_quality() -> None:
    """Failure mode 10."""
    c = Cellar([mk("L3", 12, 2, q0=90)])
    c.conserve(["L3"], 2)
    assert c.view(5)[0]["quality"] == 90
    assert c.get("L3").quality() == 90  # type: ignore[union-attr]


# ------------------------------------------------------- nightly_spoil (AC 6, 7)


def ref_spoil(lots: list[Lot], rng: random.Random) -> tuple[int, list[tuple]]:
    spoiled = 0
    left = []
    for lot in lots:
        age = lot.age + 1
        p = hazard(age, lot.fresh_days, lot.max_days, lot.initial_quality)
        if p >= 1:
            n = lot.qty
        elif p <= 0:
            n = 0
        else:
            n = sum(1 for _ in range(lot.qty) if rng.random() < p)
        spoiled += n
        if lot.qty - n > 0:
            left.append((lot.id, lot.qty - n, age))
    return spoiled, left


def state(c: Cellar) -> list[tuple]:
    return [(lot.id, lot.qty, lot.age) for lot in c.lots]


def test_spoil_draw_per_unit_on_ramp() -> None:
    c = Cellar([mk("L1", 3, 1)])
    r = random.Random(9)
    expected = sum(1 for _ in range(3) if r.random() < THIRD)
    assert c.nightly_spoil(random.Random(9)) == expected


def test_spoil_base_risk_inside_fresh_period() -> None:
    c = Cellar([mk("L1", 3, 0, q0=90)])
    r = random.Random(4)
    expected = sum(1 for _ in range(3) if r.random() < 0.1)
    assert c.nightly_spoil(random.Random(4)) == expected


def test_spoil_certain_lot_consumes_no_draw() -> None:
    rng = random.Random(1)
    before = random.Random(1).random()
    c = Cellar([mk("L1", 7, 3)])
    assert c.nightly_spoil(rng) == 7
    assert c.total() == 0
    assert rng.random() == before


def test_spoil_mixed_p0_p1_consumes_zero_draws() -> None:
    """Failure mode 5."""
    rng = random.Random(21)
    first = random.Random(21).random()
    c = Cellar([mk("OLD", 50, 3), mk("NEW", 50, 0)])
    assert c.nightly_spoil(rng) == 50
    assert state(c) == [("NEW", 50, 1)]
    assert rng.random() == first


def test_spoil_draw_count_is_one_per_unit_at_risk_only() -> None:
    rng = random.Random(7)
    ref = random.Random(7)
    for _ in range(4):
        ref.random()
    nxt = ref.random()
    c = Cellar([mk("P1", 5, 3), mk("MID", 4, 1), mk("P0", 9, 0)])
    c.nightly_spoil(rng)
    assert rng.random() == nxt


def test_spoil_certain_below_max_days_removed_without_draw() -> None:
    """Failure mode 9."""
    assert hazard(3, 1, 4, 60) == 1.0
    rng = random.Random(5)
    first = random.Random(5).random()
    c = Cellar([mk("L1", 40, 2, q0=60)])
    assert c.nightly_spoil(rng) == 40
    assert c.lots == ()
    assert rng.random() == first


def test_spoil_evaluates_hazard_at_new_age() -> None:
    """Failure mode 2: hazard at the old age would spoil none of age 1 here."""
    c = Cellar([mk("L0", 300, 1)])
    r = random.Random(11)
    expected = sum(1 for _ in range(300) if r.random() < THIRD)
    got = c.nightly_spoil(random.Random(11))
    assert got == expected
    assert 50 < got < 150
    assert c.get("L0").age == 2  # type: ignore[union-attr]
    # a delivered-today plain lot is safe tonight
    fresh = Cellar([mk("L5", 300, 0)])
    assert fresh.lots[0].risk_tonight() == 0.0
    assert fresh.nightly_spoil(random.Random(3)) == 0
    assert fresh.get("L5").age == 1  # type: ignore[union-attr]
    # an age-3 plain lot (M = 4) is already certain to go tonight
    last = Cellar([mk("L1", 300, 3)])
    assert last.nightly_spoil(random.Random(3)) == 300


def test_spoil_ignores_initial_quality_bug() -> None:
    """Failure mode 8: a 60% lot risks 40% on night 1 and is gone after night 3."""
    c = Cellar([mk("L1", 1000, 0, q0=60)])
    assert c.lots[0].risk_tonight() == pytest.approx(0.4, abs=1e-9)
    rng = random.Random(3)
    s1 = c.nightly_spoil(rng)
    assert 300 < s1 < 500
    assert 0 < c.total() < 1000
    s2 = c.nightly_spoil(rng)
    assert s2 > 0
    assert c.total() > 0
    c.nightly_spoil(rng)
    assert c.total() == 0
    assert c.get("L1") is None


def test_spoil_does_not_use_global_random() -> None:
    """Failure mode 7."""
    results = []
    for global_seed in (1, 2, 3):
        random.seed(global_seed)
        c = Cellar([mk("L1", 400, 1), mk("L2", 400, 0, q0=80)])
        results.append((c.nightly_spoil(random.Random(42)), state(c)))
    assert results[0] == results[1] == results[2]
    c = Cellar([mk("L1", 400, 1), mk("L2", 400, 0, q0=80)])
    expected_spoiled, expected_left = ref_spoil(list(c.lots), random.Random(42))
    assert results[0] == (expected_spoiled, expected_left)


def test_spoil_empty_lots_removed_everywhere() -> None:
    """Failure mode 6."""
    c = Cellar([mk("GONE", 5, 3), mk("KEEP", 5, 0)])
    assert c.nightly_spoil(random.Random(0)) == 5
    assert ids(c) == ["KEEP"]
    assert [row["id"] for row in c.view(2)] == ["KEEP"]
    assert [row["age"] for row in c.stock_by_age()] == [1]
    assert c.get("GONE") is None


def test_two_age_zero_lots_both_age_on_first_night() -> None:
    """Acceptance criterion 10 (spec 3.2)."""
    c = Cellar([mk("L0", 20, 0, q0=100), mk("L1", 10, 0, q0=100)])
    assert ids(c) == ["L0", "L1"]
    assert c.total() == 30
    assert c.nightly_spoil(random.Random(1)) == 0
    assert [lot.age for lot in c.lots] == [1, 1]
    assert ids(c) == ["L0", "L1"]


def test_spoil_is_deterministic_and_spares_zero_hazard_lot() -> None:
    def build() -> Cellar:
        return Cellar(
            [
                mk("L2", 200, 1),
                mk("L3", 200, 1, q0=70),
                mk("L4", 50, 0, fresh=3, max_days=6),
            ]
        )

    a, b = build(), build()
    assert a.nightly_spoil(random.Random(8)) == b.nightly_spoil(random.Random(8))
    assert a.to_payload() == b.to_payload()
    safe = a.get("L4")
    assert safe is not None and safe.qty == 50 and safe.age == 1


lot_strategy = st.builds(
    lambda i, qty, fresh, ramp, q0, age_off, conserved: (
        i,
        qty,
        fresh,
        fresh + ramp,
        q0,
        age_off,
        conserved,
    ),
    st.integers(0, 0),
    st.integers(1, 25),
    st.integers(0, 4),
    st.integers(1, 5),
    st.integers(1, 100),
    st.integers(0, 8),
    st.booleans(),
)


@settings(max_examples=150, deadline=None, derandomize=True)
@given(specs=st.lists(lot_strategy, min_size=1, max_size=5), seed=st.integers(0, 10**6))
def test_spoil_property_matches_draw_rule(specs: list[tuple], seed: int) -> None:
    lots = []
    for n, (_, qty, fresh, max_days, q0, age_off, conserved) in enumerate(specs):
        lots.append(
            mk(f"L{n}", qty, age_off % max_days, 12.0, q0, fresh, max_days, conserved)
        )
    lots.sort(key=lambda lot: -lot.age)
    expected_spoiled, expected_left = ref_spoil(lots, random.Random(seed))
    total = sum(lot.qty for lot in lots)
    c = Cellar(lots)
    rng = random.Random(seed)
    got = c.nightly_spoil(rng)
    assert got == expected_spoiled
    assert state(c) == expected_left
    assert c.total() == total - got
    assert all(lot.qty > 0 and lot.age < lot.max_days for lot in c.lots)


# ------------------------------------------------ expected_spoil_tonight (AC 6)


def test_expected_spoil_rounded_to_two_decimals_from_unrounded_risks() -> None:
    assert Cellar().expected_spoil_tonight() == 0.0
    assert Cellar([mk("L1", 1, 1)]).expected_spoil_tonight() == 0.33
    big = Cellar([mk("L1", 10000, 1)])
    assert big.expected_spoil_tonight() == 3333.33
    c = Cellar([mk("A", 7, 1, q0=90), mk("B", 13, 2, q0=95), mk("C", 9, 0)])
    expected = round(sum(lot.qty * lot.risk_tonight() for lot in c.lots), 2)
    assert c.expected_spoil_tonight() == expected


# ----------------------------------------------------------------- view (AC 8)


def test_view_shape_and_roundings() -> None:
    c = Cellar([mk("L1", 10000, 1, 13.456, 100)])
    (row,) = c.view(5)
    assert set(row) == {
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
    assert row["id"] == "L1"
    assert row["qty"] == 10000
    assert row["age"] == 1
    assert row["unit_cost"] == 13.46
    assert row["initial_quality"] == 100
    assert row["conserved"] is False
    assert row["quality"] == 67
    assert row["last_day"] == 7
    assert row["risk_tonight"] == 0.3333
    assert row["expected_loss_tonight"] == 3333.33


def test_view_spec_example_day_five() -> None:
    c = Cellar(
        [
            mk("L3", 12, 2, 13.0, 90),
            mk("L4", 30, 1, 12.5, 95),
            mk("L5", 20, 0, 13.0, 85),
        ]
    )
    rows = c.view(5)
    assert [r["id"] for r in rows] == ["L3", "L4", "L5"]
    assert [r["quality"] for r in rows] == [23, 62, 85]
    assert [r["last_day"] for r in rows] == [6, 7, 8]
    assert [r["risk_tonight"] for r in rows] == [0.7667, 0.3833, 0.15]
    assert [r["expected_loss_tonight"] for r in rows] == [9.2, 11.5, 3.0]


def test_view_empty() -> None:
    assert Cellar().view(3) == []
    assert Cellar().stock_by_age() == []


# --------------------------------------------------------- stock_by_age (AC 8)


def test_stock_by_age_separates_plain_and_conserved() -> None:
    c = Cellar(
        [
            mk("L2", 10, 2, fresh=3, max_days=6, conserved=True),
            mk("L3", 4, 2),
            mk("L4", 6, 0),
        ]
    )
    rows = c.stock_by_age()
    assert [r["age"] for r in rows] == [0, 2]
    assert all(
        set(r)
        == {
            "age",
            "plain_qty",
            "conserved_qty",
            "plain_risk_tonight",
            "conserved_risk_tonight",
        }
        for r in rows
    )
    young, old = rows
    assert (young["plain_qty"], young["conserved_qty"]) == (6, 0)
    assert young["plain_risk_tonight"] == 0.0
    assert young["conserved_risk_tonight"] is None
    assert (old["plain_qty"], old["conserved_qty"]) == (4, 10)
    assert old["plain_risk_tonight"] == 0.6667
    assert old["conserved_risk_tonight"] == 0.0


def test_stock_by_age_ascending_even_if_cellar_is_oldest_first() -> None:
    c = Cellar([mk("A", 1, 3), mk("B", 1, 1), mk("C", 1, 0)])
    assert [r["age"] for r in c.stock_by_age()] == [0, 1, 3]


def test_stock_by_age_weighted_average_of_unrounded_risks() -> None:
    c = Cellar([mk("A", 10, 1, q0=100), mk("B", 30, 1, q0=60)])
    (row,) = c.stock_by_age()
    exact = (10 * THIRD + 30 * (0.4 + THIRD)) / 40
    assert row["plain_qty"] == 40
    assert row["conserved_qty"] == 0
    assert row["plain_risk_tonight"] == pytest.approx(round(exact, 4), abs=1e-9)
    assert row["conserved_risk_tonight"] is None


# ----------------------------------------------------------------- payload (AC 9)


def test_cellar_payload_round_trip_keeps_every_field_and_order() -> None:
    c = Cellar(
        [
            mk("L0", 20, 3, 12.25, 100, 1, 4, False),
            mk("B5-1", 5, 2, 15.0, 83, 3, 6, True),
            mk("L5", 8, 0, 13.5, 91, 1, 4, False),
        ]
    )
    payload = c.to_payload()
    assert isinstance(payload, list) and len(payload) == 3
    again = Cellar.from_payload(json.loads(json.dumps(payload)))
    assert again.lots == c.lots
    assert ids(again) == ["L0", "B5-1", "L5"]
    assert again.to_payload() == payload
    assert [lot.initial_quality for lot in again.lots] == [100, 83, 91]


def test_empty_cellar_payload_round_trip() -> None:
    assert Cellar.from_payload(Cellar().to_payload()).lots == ()


def test_constructor_accepts_any_iterable() -> None:
    c = Cellar(lot for lot in [mk("L0", 2, 1), mk("L1", 3, 0)])
    assert c.total() == 5
    assert math.isclose(c.expected_spoil_tonight(), round(2 * THIRD, 2))
