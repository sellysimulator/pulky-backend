"""Black-box tests for section 06 supply: AbstractSupplier, hamilton, allocate."""

import random
from math import floor

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core import supply
from app.core.limits import MAX_ORDER
from app.core.money import round_cents
from app.core.rng import stream
from app.core.sampling import halfup
from app.core.supply import (
    AbstractSupplier,
    SupplierDay,
    SupplierModel,
    allocate,
    hamilton,
)

# ---------------------------------------------------------------- supplier


def _make(**over: float) -> AbstractSupplier:
    args: dict = {
        "seed": 7,
        "n_players": 4,
        "capacity_mean_per_player": 22,
        "capacity_sd_per_player": 5,
        "disruption_probability": 0.05,
        "disruption_factor": 0.4,
        "base_price": 12,
        "spot_spread": 0.25,
    }
    args.update(over)
    return AbstractSupplier(**args)


def _expected(
    seed: int,
    day: int,
    n: int,
    mean: float,
    sd: float,
    p: float,
    factor: float,
    base: float,
    spread: float,
) -> SupplierDay:
    """Spec 5.1 recomputed with the stream."""
    r = stream(seed, day, "supply")
    cap = max(0, halfup(r.gauss(mean * n, sd * n)))
    dis = r.random() < p
    if dis:
        cap = halfup(cap * factor)
    spot = round_cents(base * (1 + r.uniform(-spread, spread)))
    return SupplierDay(cap, dis, spot)


def test_spec_worked_example() -> None:
    r = random.Random("7:3:supply")
    cap = max(0, halfup(r.gauss(88, 20)))
    dis = r.random() < 0.05
    cap = halfup(cap * 0.4) if dis else cap
    spot = round_cents(12 * (1 + r.uniform(-0.25, 0.25)))
    assert _make().draw_day(3) == SupplierDay(cap, dis, spot)


def test_draw_day_matches_spec_recomputation_many_seeds_and_days() -> None:
    disrupted_seen = 0
    for seed in range(60):
        for day in range(1, 11):
            for p in (0.05, 0.4):
                s = _make(seed=seed, disruption_probability=p)
                got = s.draw_day(day)
                want = _expected(seed, day, 4, 22, 5, p, 0.4, 12, 0.25)
                assert got == want
                disrupted_seen += got.disrupted
    assert disrupted_seen > 0


def test_draw_day_is_pure_in_day_and_ignores_history() -> None:
    s = _make(seed=11)
    first = s.draw_day(5)
    assert s.draw_day(5) == first
    assert s.draw_day(5, [10, 20, 30]) == first
    assert s.draw_day(5, aggregate_orders_history=[999]) == first
    assert s.draw_day(6) != first or s.draw_day(7) != first


@pytest.mark.parametrize(("mean_pp", "want"), [(0.625, 3), (0.125, 1), (1.125, 5)])
def test_capacity_rounds_halves_up_not_to_even(mean_pp: float, want: int) -> None:
    """Failure mode 5: sd 0 makes the gauss exactly x.5 (2.5, 0.5, 4.5), where
    Python round() would give 2, 0, 4."""
    s = _make(
        capacity_mean_per_player=mean_pp,
        capacity_sd_per_player=0,
        disruption_probability=0,
    )
    assert s.draw_day(1).capacity == want


def test_disruption_capacity_rounds_halves_up() -> None:
    s = _make(
        capacity_mean_per_player=1.25,
        capacity_sd_per_player=0,
        disruption_probability=1,
        disruption_factor=0.5,
    )
    day = s.draw_day(2)
    assert day.disrupted is True
    assert day.capacity == 3  # 5 * 0.5 = 2.5 -> 3 (round() would give 2)


def test_disruption_probability_one_and_zero() -> None:
    for seed in range(40):
        for day in (1, 2, 3, 9):
            on = _make(seed=seed, disruption_probability=1)
            off = _make(seed=seed, disruption_probability=0)
            d_on = on.draw_day(day)
            d_off = off.draw_day(day)
            base = _expected(seed, day, 4, 22, 5, 0.0, 0.4, 12, 0.25)
            assert d_off.disrupted is False
            assert d_off.capacity == base.capacity
            assert d_on.disrupted is True
            assert d_on.capacity == halfup(base.capacity * 0.4)
            # the draw count is fixed: spot price is the same either way
            assert d_on.spot_price == d_off.spot_price == base.spot_price


def test_disruption_draw_taken_even_when_capacity_is_zero() -> None:
    """Failure mode 4: with capacity 0 the spot price still needs the draws of
    gauss and random() before the uniform."""
    for seed in range(40):
        for day in (1, 4):
            s = _make(
                seed=seed,
                capacity_mean_per_player=0,
                capacity_sd_per_player=0,
                disruption_probability=0.5,
            )
            got = s.draw_day(day)
            want = _expected(seed, day, 4, 0, 0, 0.5, 0.4, 12, 0.25)
            assert got.capacity == 0
            assert got == want


def test_capacity_never_negative() -> None:
    for seed in range(200):
        s = _make(
            seed=seed,
            capacity_mean_per_player=1,
            capacity_sd_per_player=30,
            disruption_probability=0.5,
        )
        for day in (1, 2, 3):
            assert s.draw_day(day).capacity >= 0


def test_spot_price_in_range_and_in_cents() -> None:
    lo, hi = 12 * 0.75, 12 * 1.25
    prices = []
    for seed in range(100):
        s = _make(seed=seed)
        for day in range(1, 6):
            price = s.draw_day(day).spot_price
            prices.append(price)
            assert lo - 0.005 <= price <= hi + 0.005
            assert abs(price * 100 - round(price * 100)) < 1e-6
    assert len(set(prices)) > 50
    assert min(prices) < 11 and max(prices) > 13


def test_zero_spread_gives_base_price() -> None:
    s = _make(spot_spread=0)
    for day in range(1, 6):
        assert s.draw_day(day).spot_price == 12.0


def test_supplier_satisfies_supplier_model() -> None:
    s = _make()
    try:
        assert isinstance(s, SupplierModel)
    except TypeError:
        assert callable(s.draw_day)
    model: SupplierModel = s
    assert isinstance(model.draw_day(1, []), SupplierDay)


def test_supplier_day_is_frozen() -> None:
    d = SupplierDay(1, False, 2.0)
    with pytest.raises(AttributeError):
        d.capacity = 5  # type: ignore[misc]


def test_supply_module_exports_public_names() -> None:
    for name in ("SupplierDay", "SupplierModel", "AbstractSupplier"):
        assert hasattr(supply, name)


# ---------------------------------------------------------------- allocation


def _ref_allocate(
    requests: dict[str, int], capacity: int, cooperative: bool, rng: random.Random
) -> dict[str, int]:
    """Spec 5.4's code, verbatim."""
    total = sum(requests.values())
    if total <= capacity:
        return dict(requests)
    tiebreak = {a: rng.random() for a in sorted(requests)}
    if cooperative:
        f = capacity / total
        base = {a: floor(r * f) for a, r in requests.items()}
        left = capacity - sum(base.values())
        order = sorted(
            requests, key=lambda a: (-(requests[a] * f - base[a]), tiebreak[a])
        )
        for a in order[:left]:
            base[a] += 1
        return base
    out, left = {}, capacity
    for a in sorted(requests, key=lambda a: (-requests[a], tiebreak[a])):
        out[a] = min(requests[a], left)
        left -= out[a]
    return out


REQS = {"P1": 60, "P2": 50, "P3": 30}


def test_spec_example_allocate_and_hamilton() -> None:
    assert allocate(REQS, 100, False, random.Random(0)) == {
        "P1": 60,
        "P2": 40,
        "P3": 0,
    }
    assert allocate(REQS, 100, True, random.Random(0)) == {
        "P1": 43,
        "P2": 36,
        "P3": 21,
    }
    assert hamilton(REQS, 100, random.Random(0)) == {"P1": 43, "P2": 36, "P3": 21}


def test_cooperative_hands_out_the_whole_capacity() -> None:
    """Failure mode 2: bases 42/35/21 sum to 98; the two leftover units must be
    handed out."""
    for fn in (
        lambda r: allocate(REQS, 100, True, r),
        lambda r: hamilton(REQS, 100, r),
    ):
        out = fn(random.Random(0))
        assert sum(out.values()) == 100
        assert sum(out.values()) != 98


def test_total_within_capacity_returns_copy_and_draws_nothing() -> None:
    reqs = {"P1": 10, "P2": 5}
    for coop in (False, True):
        r = random.Random(5)
        out = allocate(reqs, 20, coop, r)
        assert out == reqs
        assert out is not reqs
        assert r.random() == random.Random(5).random()
    r = random.Random(5)
    out = hamilton(reqs, 15, r)
    assert out == reqs and out is not reqs
    assert r.random() == random.Random(5).random()


def test_scarce_draws_one_tiebreak_per_alias() -> None:
    reqs = {"P2": 30, "P1": 40, "P3": 0}
    for coop in (False, True):
        r = random.Random(9)
        allocate(reqs, 10, coop, r)
        ref = random.Random(9)
        for _ in reqs:
            ref.random()
        assert r.random() == ref.random()
    r = random.Random(9)
    hamilton(reqs, 10, r)
    ref = random.Random(9)
    for _ in reqs:
        ref.random()
    assert r.random() == ref.random()


def test_negative_capacity_raises() -> None:
    with pytest.raises(ValueError):
        allocate({"P1": 1}, -1, False, random.Random(0))
    with pytest.raises(ValueError):
        allocate({"P1": 1}, -1, True, random.Random(0))


def test_zero_capacity_serves_nobody() -> None:
    for coop in (False, True):
        assert allocate({"P1": 3, "P2": 4}, 0, coop, random.Random(1)) == {
            "P1": 0,
            "P2": 0,
        }


def _seed_with_first_draw_smaller(first_smaller: bool) -> int:
    for seed in range(1000):
        r = random.Random(seed)
        a, b = r.random(), r.random()
        if (a < b) == first_smaller:
            return seed
    raise AssertionError("no seed")


@pytest.mark.parametrize("p1_first", [True, False])
@pytest.mark.parametrize("order", [("P1", "P2"), ("P2", "P1")])
def test_equal_largest_orders_tie_broken_by_seeded_draw(
    p1_first: bool, order: tuple[str, str]
) -> None:
    """Failure mode 3: the winner follows the draws (P1 drawn first, then P2),
    never the insertion order and never the alias."""
    seed = _seed_with_first_draw_smaller(p1_first)
    reqs = {order[0]: 40, order[1]: 40}
    out = allocate(reqs, 50, False, random.Random(seed))
    winner, loser = ("P1", "P2") if p1_first else ("P2", "P1")
    assert out[winner] == 40
    assert out[loser] == 10


def test_tie_winner_varies_with_seed() -> None:
    winners = set()
    for seed in range(40):
        out = allocate({"P1": 40, "P2": 40}, 50, False, random.Random(seed))
        winners.add("P1" if out["P1"] == 40 else "P2")
    assert winners == {"P1", "P2"}


@pytest.mark.parametrize("coop", [False, True])
def test_matches_spec_reference_on_many_seeds(coop: bool) -> None:
    rnd = random.Random(1234)
    for seed in range(300):
        n = rnd.randint(1, 8)
        reqs = {f"P{i}": rnd.choice([0, 1, 5, 10, 40, 40, 77]) for i in range(1, n + 1)}
        cap = rnd.randint(0, max(1, sum(reqs.values())))
        got = allocate(reqs, cap, coop, random.Random(seed))
        want = _ref_allocate(reqs, cap, coop, random.Random(seed))
        assert got == want
        if coop:
            assert hamilton(reqs, cap, random.Random(seed)) == want


def test_request_never_exceeded_whatever_the_capacity() -> None:
    """Failure mode 1: a request of 5 never receives 6."""
    for coop in (False, True):
        for cap in (1, 5, 6, 100, 1005, 5000):
            out = allocate({"A": 5, "B": 1000}, cap, coop, random.Random(0))
            assert out["A"] <= 5 and out["B"] <= 1000
            assert sum(out.values()) == min(cap, 1005)
    out = hamilton({"A": 5, "B": 1000}, 600, random.Random(0))
    assert out["A"] <= 5
    assert sum(out.values()) == 600


def test_cooperative_never_gives_more_than_ordered_on_small_orders() -> None:
    # many tiny orders and a large one: remainder units must not overflow a request
    reqs = {f"P{i}": 1 for i in range(1, 8)}
    reqs["P8"] = 100
    for seed in range(50):
        out = allocate(reqs, 50, True, random.Random(seed))
        assert all(out[a] <= reqs[a] for a in reqs)
        assert sum(out.values()) == 50


# --- property tests

requests_st = st.integers(min_value=1, max_value=12).flatmap(
    lambda n: st.lists(
        st.integers(min_value=0, max_value=MAX_ORDER), min_size=n, max_size=n
    ).map(lambda xs: {f"P{i + 1}": v for i, v in enumerate(xs)})
)
capacity_st = st.integers(min_value=0, max_value=12 * MAX_ORDER + 100)
seed_st = st.integers(min_value=0, max_value=10**6)


@settings(max_examples=200, deadline=None)
@given(requests_st, capacity_st, st.booleans(), seed_st)
def test_property_conservation_bounds_and_keys(
    reqs: dict[str, int], cap: int, coop: bool, seed: int
) -> None:
    out = allocate(reqs, cap, coop, random.Random(seed))
    assert set(out) == set(reqs)
    assert sum(out.values()) == min(cap, sum(reqs.values()))
    assert all(0 <= out[a] <= reqs[a] for a in reqs)
    h = hamilton(reqs, cap, random.Random(seed))
    assert set(h) == set(reqs)
    assert sum(h.values()) == min(cap, sum(reqs.values()))
    assert all(0 <= h[a] <= reqs[a] for a in reqs)


@settings(max_examples=200, deadline=None)
@given(requests_st, capacity_st, st.booleans(), seed_st, st.randoms())
def test_property_insertion_order_does_not_matter(
    reqs: dict[str, int], cap: int, coop: bool, seed: int, shuffler: random.Random
) -> None:
    keys = list(reqs)
    shuffler.shuffle(keys)
    shuffled = {k: reqs[k] for k in keys}
    assert allocate(shuffled, cap, coop, random.Random(seed)) == allocate(
        reqs, cap, coop, random.Random(seed)
    )
    assert hamilton(shuffled, cap, random.Random(seed)) == hamilton(
        reqs, cap, random.Random(seed)
    )


@settings(max_examples=200, deadline=None)
@given(requests_st, capacity_st, seed_st)
def test_property_cooperative_equal_fill_up_to_one_unit(
    reqs: dict[str, int], cap: int, seed: int
) -> None:
    for out in (
        allocate(reqs, cap, True, random.Random(seed)),
        hamilton(reqs, cap, random.Random(seed)),
    ):
        live = [a for a in reqs if reqs[a] > 0]
        for i, a in enumerate(live):
            for b in live[i + 1 :]:
                gap = abs(out[a] / reqs[a] - out[b] / reqs[b])
                assert gap <= 1 / min(reqs[a], reqs[b]) + 1e-12


@settings(max_examples=200, deadline=None)
@given(requests_st, st.booleans(), seed_st, st.integers(0, 1000))
def test_property_no_draw_when_everything_fits(
    reqs: dict[str, int], coop: bool, seed: int, slack: int
) -> None:
    cap = sum(reqs.values()) + slack
    r = random.Random(seed)
    assert allocate(reqs, cap, coop, r) == reqs
    assert hamilton(reqs, cap, r) == reqs
    assert r.random() == random.Random(seed).random()
