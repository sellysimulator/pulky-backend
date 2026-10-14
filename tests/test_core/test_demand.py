"""Black-box tests for section 09: demand (spec 7.2-7.5, 7.7)."""

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.demand import (
    ServeResult,
    draw_demands,
    market_lambda,
    market_shares,
    serve,
    shares,
)
from app.core.rng import stream
from app.core.sampling import poisson

APPROX = {"abs": 1e-9}

reputation_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
alias_maps = st.dictionaries(
    st.sampled_from([f"P{i}" for i in range(1, 13)]),
    reputation_st,
    min_size=1,
    max_size=12,
)


# --- market_lambda ---------------------------------------------------------


def test_market_lambda_friday_example() -> None:
    assert market_lambda(25, 4, 1.0) == pytest.approx(100.0, **APPROX)


def test_market_lambda_weekday_table() -> None:
    mults = (0.25, 0.5, 0.75, 1.0, 1.0, 1.0, 0.75)
    got = [market_lambda(25, 4, m) for m in mults]
    assert got == pytest.approx([25, 50, 75, 100, 100, 100, 75], **APPROX)


# --- shares ----------------------------------------------------------------


def test_shares_worked_example() -> None:
    got = shares({"P1": 0.8, "P2": 0.5, "P3": 0.5, "P4": 0.5})
    assert set(got) == {"P1", "P2", "P3", "P4"}
    assert got["P1"] == pytest.approx(1.3 / 4.3, **APPROX)
    for alias in ("P2", "P3", "P4"):
        assert got[alias] == pytest.approx(1.0 / 4.3, **APPROX)


def test_shares_equal_reputations_equal_shares() -> None:
    got = shares({"P1": 0.5, "P2": 0.5})
    assert got == pytest.approx({"P1": 0.5, "P2": 0.5}, **APPROX)


def test_shares_empty_mapping() -> None:
    assert shares({}) == {}


def test_shares_zero_reputation_still_gets_share() -> None:
    # Failure mode 2: share by raw reputation would give P1 exactly 0.
    got = shares({"P1": 0.0, "P2": 1.0})
    assert got["P1"] > 0.0
    assert got["P1"] == pytest.approx(0.5 / 2.0, **APPROX)
    assert got["P2"] == pytest.approx(1.5 / 2.0, **APPROX)
    # Everyone at zero still splits evenly (raw weights would divide by zero).
    assert shares({"P1": 0.0, "P2": 0.0}) == pytest.approx(
        {"P1": 0.5, "P2": 0.5}, **APPROX
    )


@given(alias_maps)
def test_shares_sum_to_one_and_keep_keys(reps: dict[str, float]) -> None:
    got = shares(reps)
    assert set(got) == set(reps)
    assert sum(got.values()) == pytest.approx(1.0, abs=1e-9)


@given(st.integers(min_value=1, max_value=12), reputation_st)
def test_shares_equal_reputation_gives_equal_shares(n: int, rep: float) -> None:
    got = shares({f"P{i}": rep for i in range(1, n + 1)})
    for value in got.values():
        assert value == pytest.approx(1.0 / n, abs=1e-9)


# --- draw_demands ----------------------------------------------------------


def test_draw_demands_matches_poisson_of_alias_stream() -> None:
    assert draw_demands(7, 5, {"P1": 30.2})["P1"] == poisson(
        stream(7, 5, "demand:P1"), 30.2
    )


def test_draw_demands_returns_ints_for_each_alias() -> None:
    got = draw_demands(1, 1, {"P1": 10.0, "P2": 40.0})
    assert set(got) == {"P1", "P2"}
    assert all(isinstance(v, int) and v >= 0 for v in got.values())


def test_draw_demands_independent_of_other_aliases() -> None:
    # Failure mode 1: with one shared stream, removing P1 shifts P2's draw.
    # Knuth (lam <= 30) consumes a variable number of uniforms, so a shared
    # stream would change P2 for at least some of these seeds/days.
    both = {"P1": 25.0, "P2": 20.0, "P3": 12.0}
    alone_checks = 0
    for seed, day in itertools.product(range(1, 9), range(1, 6)):
        full = draw_demands(seed, day, both)
        without_p1 = draw_demands(seed, day, {"P2": 20.0, "P3": 12.0})
        only_p2 = draw_demands(seed, day, {"P2": 20.0})
        assert full["P2"] == without_p1["P2"] == only_p2["P2"]
        assert full["P3"] == without_p1["P3"]
        assert full["P2"] == poisson(stream(seed, day, "demand:P2"), 20.0)
        assert full["P3"] == poisson(stream(seed, day, "demand:P3"), 12.0)
        alone_checks += 1
    assert alone_checks == 40


def test_draw_demands_independent_of_insertion_order() -> None:
    lam = {"P1": 30.2, "P2": 23.3, "P3": 8.0, "P4": 45.5}
    for perm in itertools.permutations(lam):
        reordered = {k: lam[k] for k in perm}
        assert draw_demands(11, 3, reordered) == draw_demands(11, 3, lam)


def test_draw_demands_depends_on_seed_and_day() -> None:
    lam = {"P1": 25.0}
    values = {draw_demands(s, d, lam)["P1"] for s in range(1, 6) for d in range(1, 6)}
    assert len(values) > 1


def test_draw_demands_mean_over_2000_draws() -> None:
    total = sum(draw_demands(42, day, {"P1": 23.3})["P1"] for day in range(1, 2001))
    assert total / 2000 == pytest.approx(23.3, rel=0.03)


# --- serve -----------------------------------------------------------------


def test_serve_worked_examples() -> None:
    assert serve(5, 45, 40) == ServeResult(owed=50, sold=40, backlog_end=10)
    assert serve(0, 0, 30) == ServeResult(owed=0, sold=0, backlog_end=0)


def test_serve_backlog_from_stock_counts_as_sold() -> None:
    # Failure mode 3 (spec 14 #5): late backlog served from stock is sold.
    res = serve(10, 0, 10)
    assert res.sold == 10
    assert res.owed == 10
    assert res.backlog_end == 0


def test_serve_stock_exceeds_owed() -> None:
    assert serve(2, 3, 100) == ServeResult(owed=5, sold=5, backlog_end=0)


@given(
    st.integers(0, 10_000),
    st.integers(0, 10_000),
    st.integers(0, 10_000),
)
def test_serve_invariants(backlog: int, demand: int, stock: int) -> None:
    res = serve(backlog, demand, stock)
    assert res.owed == backlog + demand
    assert res.owed == res.sold + res.backlog_end
    assert res.sold <= stock
    assert res.sold == min(res.owed, stock)
    assert res.backlog_end >= 0


@pytest.mark.parametrize("args", [(-1, 0, 0), (0, -1, 0), (0, 0, -1)])
def test_serve_rejects_negative_arguments(args: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        serve(*args)


# --- market_shares ---------------------------------------------------------


def test_market_shares_worked_examples() -> None:
    got = market_shares({"P1": 30, "P2": 10, "P3": 0})
    assert got == pytest.approx({"P1": 0.75, "P2": 0.25, "P3": 0.0}, **APPROX)
    assert market_shares({"P1": 0, "P2": 0}) == {"P1": 0.0, "P2": 0.0}


@given(
    st.dictionaries(
        st.sampled_from([f"P{i}" for i in range(1, 13)]),
        st.integers(0, 5_000),
        min_size=1,
        max_size=12,
    )
)
def test_market_shares_sum(sold: dict[str, int]) -> None:
    got = market_shares(sold)
    assert set(got) == set(sold)
    if sum(sold.values()) > 0:
        assert sum(got.values()) == pytest.approx(1.0, abs=1e-9)
    else:
        assert all(v == 0.0 for v in got.values())
