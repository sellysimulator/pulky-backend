"""Black-box tests for app.core.transport (plan 08, spec section 6.1-6.2)."""

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.rng import stream
from app.core.transport import draw_failure, transport_losses

SHIP = {"P1": 60, "P2": 30, "P3": 10}


# --- draw_failure ---------------------------------------------------------


def test_draw_failure_consumes_exactly_one_draw():
    rng = random.Random(123)
    twin = random.Random(123)
    draw_failure(rng, 0.5)
    twin.random()
    assert rng.random() == twin.random()


@pytest.mark.parametrize("p", [0.0, 0.5, 1.0])
def test_draw_failure_consumes_one_draw_for_any_probability(p):
    rng = random.Random(9)
    twin = random.Random(9)
    draw_failure(rng, p)
    twin.random()
    assert rng.random() == twin.random()


@pytest.mark.parametrize("seed", range(40))
def test_draw_failure_certain_and_impossible(seed):
    assert draw_failure(random.Random(seed), 1.0) is True
    assert draw_failure(random.Random(seed), 0.0) is False


def test_draw_failure_is_strictly_less_than():
    class Fixed(random.Random):
        def random(self) -> float:
            return 0.25

    assert draw_failure(Fixed(), 0.25) is False
    assert draw_failure(Fixed(), 0.2500001) is True


def test_draw_failure_ignores_global_random():
    # spec 14 #20: only the rng passed in may be used.
    expected = stream(7, 3, "transport").random() < 0.5
    for global_seed in range(60):
        random.seed(global_seed)
        assert draw_failure(stream(7, 3, "transport"), 0.5) is expected


def test_draw_failure_does_not_reseed_or_touch_global_random():
    random.seed(5)
    state = random.getstate()
    draw_failure(random.Random(1), 0.5)
    assert random.getstate() == state


# --- transport_losses: worked examples ------------------------------------


def test_losses_not_shared_example():
    assert transport_losses(SHIP, True, 0.3, False) == {"P1": 18, "P2": 9, "P3": 3}


def test_losses_shared_example_float_noise_guard():
    # 60 * (0.3 / 3) is 5.999... in floating point and must lose 6, not 5.
    assert transport_losses(SHIP, True, 0.3, True) == {"P1": 6, "P2": 3, "P3": 1}


def test_losses_shared_counts_only_shippers():
    ship = {"P1": 60, "P2": 0, "P3": 10, "P4": 30}
    assert transport_losses(ship, True, 0.3, True) == {
        "P1": 6,
        "P2": 0,
        "P3": 1,
        "P4": 3,
    }


def test_losses_no_failure_loses_nothing():
    assert transport_losses({"P1": 60}, False, 0.3, True) == {"P1": 0}


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("loss", [0.0, 0.3, 1.0])
def test_losses_without_failure_are_zero_whatever_the_fraction(shared, loss):
    out = transport_losses(SHIP, False, loss, shared)
    assert out == {"P1": 0, "P2": 0, "P3": 0}


def test_losses_floor_gives_zero_for_small_shipment():
    assert transport_losses({"P1": 3}, True, 0.3, False) == {"P1": 0}


def test_losses_returns_exactly_input_keys():
    ship = {"P1": 0, "P2": 5, "P7": 40}
    for failure in (False, True):
        for shared in (False, True):
            assert set(transport_losses(ship, failure, 0.5, shared)) == set(ship)


def test_losses_all_zero_shipments_with_sharing_does_not_divide_by_zero():
    ship = {"P1": 0, "P2": 0}
    assert transport_losses(ship, True, 0.3, True) == {"P1": 0, "P2": 0}


def test_losses_shared_single_shipper_equals_unshared():
    ship = {"P1": 50, "P2": 0, "P3": 0}
    assert transport_losses(ship, True, 0.3, True) == transport_losses(
        ship, True, 0.3, False
    )


def test_losses_full_fraction_loses_everything_when_alone():
    assert transport_losses({"P1": 37}, True, 1.0, False) == {"P1": 37}


# --- failure modes ---------------------------------------------------------


def test_fm1_sharing_does_not_count_non_shippers():
    # spec 14 #19: n = 3 shippers, so P1 loses 6; counting P2 (n = 4) gives 4.
    ship = {"P1": 60, "P2": 0, "P3": 10, "P4": 30}
    assert transport_losses(ship, True, 0.3, True)["P1"] == 6


def test_fm2_float_noise_does_not_drop_a_unit():
    assert transport_losses(SHIP, True, 0.3, True)["P1"] == 6


def test_fm3_no_loss_without_failure():
    out = transport_losses(SHIP, False, 0.9, False)
    assert sum(out.values()) == 0


# --- properties -------------------------------------------------------------

_shipments = st.dictionaries(
    st.sampled_from(["P1", "P2", "P3", "P4", "P5", "P6"]),
    st.integers(min_value=0, max_value=500),
    min_size=1,
)
_fractions = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@given(_shipments, _fractions, st.booleans())
def test_property_loss_within_shipment(ship, loss, shared):
    out = transport_losses(ship, True, loss, shared)
    assert set(out) == set(ship)
    for alias, lost in out.items():
        assert 0 <= lost <= ship[alias]


@given(_shipments, _fractions)
def test_property_shared_never_larger_than_unshared(ship, loss):
    shared = transport_losses(ship, True, loss, True)
    alone = transport_losses(ship, True, loss, False)
    for alias in ship:
        assert shared[alias] <= alone[alias]


@given(_shipments, _fractions, st.booleans())
def test_property_no_failure_no_loss(ship, loss, shared):
    assert set(transport_losses(ship, False, loss, shared).values()) <= {0}
