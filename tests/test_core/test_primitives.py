"""Black-box tests for section 02 primitives (enums, limits, rng, sampling, money,
kernels) plus the whole-of-app/core import and global-RNG guards."""

import json
import random
import re
import statistics
from pathlib import Path

import pytest

from app.core import limits
from app.core.enums import (
    WEEKDAYS,
    Incoterm,
    Phase,
    RoomState,
    TradeSide,
    Weekday,
)
from app.core.kernels import (
    triangle_kernel,
    weekday_multiplier,
    weekday_multipliers,
)
from app.core.money import format_money, format_number, round_cents, to_cents
from app.core.rng import stream
from app.core.sampling import halfup, poisson

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "tests" / "fixtures" / "rounding_cases.json").read_text())[
    "round_cents"
]
W = Weekday


# ---- AC1 enums -------------------------------------------------------------
def test_enum_values_are_wire_strings() -> None:
    assert [m.value for m in Weekday] == [
        "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    ]  # fmt: skip
    assert {m.name: m.value for m in Incoterm} == {
        "EXW": "exw", "CIP": "cip", "DAP": "dap", "DDP": "ddp",
    }  # fmt: skip
    assert {m.name: m.value for m in RoomState} == {
        "LOBBY": "lobby", "CONFIGURING": "configuring",
        "RUNNING": "running", "FINISHED": "finished",
    }  # fmt: skip
    assert {m.name: m.value for m in Phase} == {
        "DECIDING": "deciding", "FINISHED": "finished",
    }  # fmt: skip
    assert {m.name: m.value for m in TradeSide} == {"BUY": "buy", "SELL": "sell"}
    assert Weekday.MON == "mon"


def test_weekdays_tuple_in_order() -> None:
    assert isinstance(WEEKDAYS, tuple)
    assert WEEKDAYS == (W.MON, W.TUE, W.WED, W.THU, W.FRI, W.SAT, W.SUN)


# ---- AC2 limits ------------------------------------------------------------
SPEC_LIMITS = {
    "MIN_PLAYERS_TO_START": 2,
    "MAX_PLAYERS": 12,
    "MIN_DAYS": 7,
    "MAX_DAYS": 60,
    "MAX_ORDER": 9999,
    "MAX_LAMBDA": 500,
    "MAX_LEAD_TIME": 7,
    "MAX_SHELF_DAYS": 14,
    "MAX_HORIZON": 14,
    "MAX_CONSERVE_LOTS": 30,
    "MAX_PEER_PRICE": 10000,
    "MAX_MONEY": 1e6,
    "MAX_DISPLAY_NAME": 24,
    "ROOM_TTL_SECONDS": 86400,
    "LOCK_TIMEOUT_SECONDS": 10,
}


@pytest.mark.parametrize(("name", "value"), SPEC_LIMITS.items())
def test_limit_constant_and_map(name: str, value: float) -> None:
    assert getattr(limits, name) == value
    assert limits.LIMITS[name] == value


def test_limits_map_has_exactly_the_constants() -> None:
    assert set(limits.LIMITS) == set(SPEC_LIMITS)


# ---- AC3 / FM7 rng ---------------------------------------------------------
def test_stream_equals_seeded_random() -> None:
    a = stream(42, 3, "supply")
    b = random.Random("42:3:supply")
    assert isinstance(a, random.Random)
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]
    assert stream(42, 3, "supply").random() == random.Random("42:3:supply").random()


def test_stream_fresh_and_sensitive_to_each_argument() -> None:
    assert stream(1, 1, "x") is not stream(1, 1, "x")
    base = [stream(1, 1, "x").random() for _ in range(1)]
    assert base == [stream(1, 1, "x").random()]
    assert stream(2, 1, "x").random() != base[0]
    assert stream(1, 2, "x").random() != base[0]
    assert stream(1, 1, "y").random() != base[0]


def test_stream_and_poisson_leave_global_random_untouched() -> None:
    random.seed(12345)
    expected = [random.random() for _ in range(5)]
    random.seed(12345)
    stream(7, 2, "demand:P1").random()
    poisson(stream(7, 2, "demand:P1"), 5)
    poisson(stream(7, 2, "demand:P1"), 100)
    assert [random.random() for _ in range(5)] == expected


# ---- AC9 / FM1 halfup ------------------------------------------------------
@pytest.mark.parametrize(
    ("x", "out"),
    [(2.5, 3), (3.5, 4), (0.5, 1), (2.49, 2), (-0.5, 0), (-1.5, -1), (0.0, 0)],
)
def test_halfup(x: float, out: int) -> None:
    result = halfup(x)
    assert result == out
    assert type(result) is int


# ---- AC8 poisson -----------------------------------------------------------
@pytest.mark.parametrize("lam", [7.5, 100.0])
def test_poisson_mean_and_variance(lam: float) -> None:
    rng = random.Random(3)
    samples = [poisson(rng, lam) for _ in range(4000)]
    assert all(type(s) is int and s >= 0 for s in samples)
    assert statistics.fmean(samples) == pytest.approx(lam, rel=0.05)
    assert statistics.pvariance(samples) == pytest.approx(lam, rel=0.10)


@pytest.mark.parametrize("lam", [0, -1, -0.5, 0.0])
def test_poisson_non_positive_is_zero(lam: float) -> None:
    rng = random.Random(1)
    state = rng.getstate()
    assert poisson(rng, lam) == 0
    assert rng.getstate() == state


def test_poisson_exact_reference_sequence() -> None:
    """Replay: the draw procedure is frozen (spec §7.3)."""
    import math

    def reference(rng: random.Random, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam > 30:
            return max(0, round(rng.gauss(lam, math.sqrt(lam))))
        threshold = math.exp(-lam)
        k, p = 0, 1.0
        while True:
            p *= rng.random()
            if p <= threshold:
                return k
            k += 1

    for lam in (0.3, 7.5, 30.0, 30.5, 100.0):
        a, b = random.Random(9), random.Random(9)
        assert [poisson(a, lam) for _ in range(50)] == [
            reference(b, lam) for _ in range(50)
        ]


# ---- AC4 / FM6 money -------------------------------------------------------
@pytest.mark.parametrize("case", CASES)
def test_round_cents_fixture(case: dict[str, float]) -> None:
    assert round_cents(case["value"]) == case["expected"]


@pytest.mark.parametrize("case", CASES)
def test_to_cents_matches_fixture(case: dict[str, float]) -> None:
    got = to_cents(case["value"])
    assert type(got) is int
    assert got == int(case["expected"] * 100 + (0.5 if case["expected"] >= 0 else -0.5))


def test_round_cents_named_cases() -> None:
    assert round_cents(0.125) == 0.13
    assert round_cents(1.005) == 1.01
    assert round_cents(-0.125) == -0.13
    assert str(round_cents(-0.0001)) == "0.0"  # no negative zero


def test_to_cents_named_cases() -> None:
    assert to_cents(0.125) == 13
    assert to_cents(1.005) == 101
    assert to_cents(-0.125) == -13
    assert to_cents(12.0) == 1200


def test_format_money() -> None:
    assert format_money(904) == "$904.00"
    assert format_money(6120.5) == "$6,120.50"
    assert format_money(-12) == "-$12.00"
    assert format_money(1234567.891) == "$1,234,567.89"
    assert format_money(1.005) == "$1.01"
    assert format_money(0) == "$0.00"
    assert format_money(-0.001) == "$0.00"


def test_format_number() -> None:
    assert format_number(9999) == "9,999"
    assert format_number(0.9) == "0.9"
    assert format_number(1e4) == "10,000"
    assert format_number(10000.0) == "10,000"
    assert format_number(0.25) == "0.25"
    assert format_number(1234.5) == "1,234.5"
    assert format_number(1e6) == "1,000,000"
    assert format_number(0) == "0"


# ---- AC5/AC6 / FM2-4 kernels ----------------------------------------------
def test_triangle_kernel() -> None:
    assert triangle_kernel(0, 0, 4) == 1.0
    assert triangle_kernel(1, 0, 4) == pytest.approx(0.75)
    assert triangle_kernel(3, 4, 4) == pytest.approx(0.75)
    assert triangle_kernel(0, 4, 4) == 0.0
    assert triangle_kernel(8, 4, 4) == 0.0
    assert triangle_kernel(100, 0, 4) == 0.0  # clamped
    assert triangle_kernel(3, 3, 0) == 1.0
    assert triangle_kernel(3, 4, 0) == 0.0
    assert triangle_kernel(3, 3, -2) == 1.0
    assert triangle_kernel(3, 4, -2) == 0.0


def _approx_map(got: dict[Weekday, float], want: dict[Weekday, float]) -> None:
    assert list(got) == list(WEEKDAYS)
    for day in WEEKDAYS:
        assert got[day] == pytest.approx(want[day], abs=1e-12)


def test_default_window_thu_sat() -> None:
    _approx_map(
        weekday_multipliers(W.THU, W.SAT),
        {W.MON: 0.25, W.TUE: 0.5, W.WED: 0.75, W.THU: 1.0, W.FRI: 1.0,
         W.SAT: 1.0, W.SUN: 0.75},
    )  # fmt: skip


def test_full_week_window_is_all_ones() -> None:
    assert all(v == 1.0 for v in weekday_multipliers(W.MON, W.SUN).values())


def test_single_day_window_wed() -> None:
    _approx_map(
        weekday_multipliers(W.WED, W.WED),
        {W.MON: 0.6, W.TUE: 0.8, W.WED: 1.0, W.THU: 0.8, W.FRI: 0.6,
         W.SAT: 0.4, W.SUN: 0.2},
    )  # fmt: skip


def test_sat_sun_window_has_no_wraparound() -> None:
    m = weekday_multipliers(W.SAT, W.SUN)
    _approx_map(
        m,
        {W.MON: 1 / 6, W.TUE: 2 / 6, W.WED: 3 / 6, W.THU: 4 / 6, W.FRI: 5 / 6,
         W.SAT: 1.0, W.SUN: 1.0},
    )  # fmt: skip
    assert m[W.MON] == pytest.approx(1 / 6) and m[W.MON] != pytest.approx(5 / 6)


def test_asymmetric_window_same_step_both_sides() -> None:
    m = weekday_multipliers(W.THU, W.SAT)
    assert m[W.WED] == pytest.approx(0.75)
    assert m[W.SUN] == pytest.approx(0.75)
    assert m[W.WED] == pytest.approx(m[W.SUN], abs=1e-12)


def test_farthest_day_is_never_zero() -> None:
    assert weekday_multiplier(W.MON, W.THU, W.SAT) == pytest.approx(0.25)
    for a in range(7):
        for b in range(a, 7):
            vals = weekday_multipliers(WEEKDAYS[a], WEEKDAYS[b])
            assert min(vals.values()) > 0


def test_multiplier_one_inside_and_step_outside() -> None:
    for a in range(7):
        for b in range(a, 7):
            pf, pt = WEEKDAYS[a], WEEKDAYS[b]
            reach = max(a, 6 - b) + 1  # max(from-1, 7-to)+1, 0-based indices here
            for i, d in enumerate(WEEKDAYS):
                got = weekday_multiplier(d, pf, pt)
                assert got == weekday_multipliers(pf, pt)[d]
                if a <= i <= b:
                    assert got == 1.0
                else:
                    dist = a - i if i < a else i - b
                    assert got == pytest.approx(1 - dist / reach, abs=1e-12)


def test_inverted_window_raises() -> None:
    with pytest.raises(ValueError):
        weekday_multiplier(W.MON, W.SAT, W.THU)
    with pytest.raises(ValueError):
        weekday_multipliers(W.SAT, W.THU)


# ---- AC10 / AC11 purity guards --------------------------------------------
CORE = ROOT / "app" / "core"
FORBIDDEN_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+app\.(config|services|sockets|db|api)\b", re.M
)
GLOBAL_RANDOM = re.compile(
    r"(?<![\w.])random\.(random|randint|randrange|choice|choices|shuffle|sample"
    r"|uniform|gauss|seed|getrandbits)\("
)
FROM_RANDOM = re.compile(r"^\s*from\s+random\s+import", re.M)
SECTION_MODULES = [
    "enums", "limits", "rng", "sampling", "money", "kernels", "calendar",
]  # fmt: skip


@pytest.mark.parametrize("name", SECTION_MODULES)
def test_section_modules_exist_and_do_not_import_outer_layers(name: str) -> None:
    path = CORE / f"{name}.py"
    assert path.is_file()
    assert not FORBIDDEN_IMPORT.search(path.read_text())


def test_no_global_random_use_in_app_core() -> None:
    offenders = []
    for path in CORE.rglob("*.py"):
        if path.name == "rng.py":
            continue
        text = path.read_text()
        if GLOBAL_RANDOM.search(text) or FROM_RANDOM.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_global_random_scanner_detects_violations() -> None:
    assert GLOBAL_RANDOM.search("x = random.random()")
    assert GLOBAL_RANDOM.search("random.seed(1)")
    assert not GLOBAL_RANDOM.search("rng: random.Random")
    assert not GLOBAL_RANDOM.search("self.rng.random()")
    assert FROM_RANDOM.search("from random import choice")
