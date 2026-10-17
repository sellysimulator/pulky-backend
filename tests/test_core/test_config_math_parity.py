"""Black-box tests for section 11: the config-math parity cases (spec 13.10).

The same cases are mirrored by the frontend; floats compare with an absolute
tolerance of 1e-6, integers exactly.
"""

import json
from pathlib import Path

import pytest

from app.core.enums import IncotermTier, Weekday
from app.core.insurance import break_even_fee
from app.core.kernels import weekday_multiplier
from app.core.lots import days_left, hazard, quality, quality_guide
from app.core.reputation import update_reputation

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "config_math_cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
ABS = 1e-6


def test_fixture_is_the_plan_file_unchanged():
    plan = Path(__file__).resolve().parents[3] / (
        "docs/plan/fixtures/config_math_cases.json"
    )
    if not plan.exists():
        pytest.skip("plan fixture not available in this checkout")
    assert FIXTURE.read_bytes() == plan.read_bytes()


def test_fixture_has_every_case_family():
    for family in (
        "weekday_multiplier",
        "hazard",
        "quality",
        "quality_guide",
        "reputation_step",
        "break_even_fee",
    ):
        assert CASES[family], family


@pytest.mark.parametrize("case", CASES["weekday_multiplier"])
def test_weekday_multiplier_matches(case):
    got = weekday_multiplier(
        Weekday(case["day"]), Weekday(case["from"]), Weekday(case["to"])
    )
    assert got == pytest.approx(case["expected"], abs=ABS)


@pytest.mark.parametrize("case", CASES["hazard"])
def test_hazard_with_initial_quality_matches(case):
    got = hazard(
        case["age"], case["fresh_days"], case["max_days"], case["initial_quality"]
    )
    assert got == pytest.approx(case["expected"], abs=ABS)


@pytest.mark.parametrize("case", CASES["reputation_step"])
def test_reputation_step_matches(case):
    got = update_reputation(
        case["reputation"],
        case["fill"],
        case["waste"],
        case["smoothing"],
        case["waste_weight"],
    )
    assert got == pytest.approx(case["expected"], abs=ABS)


@pytest.mark.parametrize("case", CASES["break_even_fee"])
def test_break_even_fee_matches(case):
    got = break_even_fee(
        IncotermTier(case["tier"]),
        failure_probability=case["failure_probability"],
        loss_fraction=case["loss_fraction"],
        shared=case["shared"],
        n_ref=case["n_ref"],
        reference_shipment=case["reference_shipment"],
        base_price=case["base_price"],
        retail_price=case["retail_price"],
    )
    assert got == pytest.approx(case["expected"], abs=ABS)


@pytest.mark.parametrize("case", CASES["quality"])
def test_quality_and_days_left_match_exactly(case):
    args = (case["age"], case["fresh_days"], case["max_days"], case["initial_quality"])
    assert quality(*args) == case["expected_quality"]
    assert days_left(*args) == case["expected_days_left"]


@pytest.mark.parametrize("case", CASES["quality_guide"])
def test_quality_guide_matches_exactly(case):
    got = quality_guide(case["day"], case["fresh_days"], case["max_days"])
    assert got == case["expected"]
