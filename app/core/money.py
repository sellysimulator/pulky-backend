"""Money rounding and formatting (spec §8.1, §5.7)."""

import math


def round_cents(value: float) -> float:
    cents = float(f"{value * 100:.12g}")
    return math.copysign(math.floor(abs(cents) + 0.5), cents) / 100 + 0.0


def format_money(value: float) -> str:
    rounded = round_cents(value)
    text = f"{abs(rounded):,.2f}"
    return f"-${text}" if rounded < 0 else f"${text}"


def format_number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    whole, _, frac = repr(float(value)).partition(".")
    if "e" in repr(float(value)):
        return f"{value:,.12f}".rstrip("0").rstrip(".")
    sign = "-" if whole.startswith("-") else ""
    return f"{sign}{int(whole.lstrip('-')):,}.{frac}"


def to_cents(value: float) -> int:
    return math.floor(round_cents(value) * 100 + 0.5)
