"""Triangular kernel and the weekday multiplier (spec §7.1)."""

from app.core.enums import WEEKDAYS, Weekday


def triangle_kernel(current: float, preferred: float, denominator: float) -> float:
    if denominator <= 0:
        return 1.0 if current == preferred else 0.0
    return min(1.0, max(0.0, 1 - abs(current - preferred) / denominator))


def weekday_multiplier(weekday: Weekday, peak_from: Weekday, peak_to: Weekday) -> float:
    w = WEEKDAYS.index(weekday) + 1
    start = WEEKDAYS.index(peak_from) + 1
    end = WEEKDAYS.index(peak_to) + 1
    if start > end:
        raise ValueError("peak window cannot end before it starts")
    if start <= w <= end:
        dist = 0
    elif w < start:
        dist = start - w
    else:
        dist = w - end
    reach = max(start - 1, 7 - end) + 1
    return triangle_kernel(dist, 0, reach)


def weekday_multipliers(peak_from: Weekday, peak_to: Weekday) -> dict[Weekday, float]:
    return {d: weekday_multiplier(d, peak_from, peak_to) for d in WEEKDAYS}
