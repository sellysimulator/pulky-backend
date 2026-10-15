"""Black-box tests for section 09: reputation (spec 7.6)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.reputation import fill_ratio, update_reputation, waste_ratio

unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


def test_fill_and_waste_worked_example() -> None:
    assert fill_ratio(40, 50) == pytest.approx(0.8, abs=1e-9)
    assert waste_ratio(10, 40) == pytest.approx(0.2, abs=1e-9)


def test_fill_ratio_empty_day_is_one() -> None:
    # Failure mode 4: a day with nothing owed must not lower reputation.
    assert fill_ratio(0, 0) == 1.0
    day_rep = update_reputation(0.7, fill_ratio(0, 0), waste_ratio(0, 0), 0.3, 0.3)
    assert day_rep == pytest.approx(0.7 * 0.7 + 0.3 * 1.0, abs=1e-9)
    assert day_rep >= 0.7


def test_waste_ratio_zero_when_nothing_sold_or_spoiled() -> None:
    # Failure mode 5: no ZeroDivisionError.
    assert waste_ratio(0, 0) == 0.0


def test_waste_ratio_all_spoiled() -> None:
    assert waste_ratio(5, 0) == pytest.approx(1.0, abs=1e-9)


def test_update_reputation_worked_example() -> None:
    assert update_reputation(0.60, 0.8, 0.2, 0.3, 0.3) == pytest.approx(0.66, abs=1e-9)


def test_update_reputation_smoothing_direction() -> None:
    # Failure mode 6: backwards smoothing would give 0.74.
    got = update_reputation(0.6, 0.8, 0.2, 0.3, 0.3)
    assert got != pytest.approx(0.74, abs=1e-6)
    # rho = 0 keeps the old value; rho = 1 takes today's value.
    assert update_reputation(0.6, 0.8, 0.2, 0.0, 0.3) == pytest.approx(0.6, abs=1e-9)
    assert update_reputation(0.6, 0.8, 0.2, 1.0, 0.3) == pytest.approx(0.8, abs=1e-9)


def test_update_reputation_waste_weight_extremes() -> None:
    # w = 0: only fill counts; w = 1: only (1 - waste) counts.
    assert update_reputation(0.0, 0.4, 0.9, 1.0, 0.0) == pytest.approx(0.4, abs=1e-9)
    assert update_reputation(0.0, 0.4, 0.9, 1.0, 1.0) == pytest.approx(0.1, abs=1e-9)


def test_perfect_day_from_one_stays_one() -> None:
    assert update_reputation(1.0, 1.0, 0.0, 0.3, 0.3) == pytest.approx(1.0, abs=1e-9)


@given(unit, unit, unit, unit, unit)
def test_update_reputation_stays_in_unit_interval(
    rep: float, fill: float, waste: float, rho: float, w: float
) -> None:
    got = update_reputation(rep, fill, waste, rho, w)
    assert -1e-12 <= got <= 1.0 + 1e-12


@given(st.integers(0, 5_000), st.integers(0, 5_000))
def test_ratios_in_unit_interval(a: int, b: int) -> None:
    sold, owed = min(a, b), max(a, b)
    assert 0.0 <= fill_ratio(sold, owed) <= 1.0
    assert 0.0 <= waste_ratio(a, b) <= 1.0
