"""Black-box tests for app.core.calendar (weekday_of, order windows)."""

import pytest

from app.core.calendar import OrderWindows, order_windows, weekday_index, weekday_of
from app.core.enums import WEEKDAYS, Weekday

W = Weekday


def test_weekday_index() -> None:
    assert [weekday_index(d) for d in WEEKDAYS] == [1, 2, 3, 4, 5, 6, 7]


def test_weekday_of_examples() -> None:
    assert weekday_of(1) == W.MON
    assert weekday_of(5) == W.FRI
    assert weekday_of(8) == W.MON
    assert weekday_of(14) == W.SUN
    assert weekday_of(60) == WEEKDAYS[(60 - 1) % 7]


@pytest.mark.parametrize("day", [0, -1, -7])
def test_weekday_of_below_one_raises(day: int) -> None:
    with pytest.raises(ValueError):
        weekday_of(day)


def test_mid_game_windows() -> None:
    w = order_windows(5, 14, 1, 7, 12.0)
    assert isinstance(w, OrderWindows)
    assert w.spot_open is True
    assert w.spot_arrives_day == 6
    assert w.futures_open is True
    assert w.futures_delivery_day == 12
    assert w.last_day_to_buy == 7
    assert w.days_left_to_buy == 3
    assert (
        w.futures_message
        == "Futures: 3 days left to buy (delivery in 7 days, at $12.00)"
    )
    assert w.spot_message is None


def test_last_day_to_buy_boundary() -> None:
    last = order_windows(7, 14, 1, 7, 12.0)
    assert last.days_left_to_buy == 1
    assert last.futures_open is True
    assert last.futures_message == "Last day to buy futures"
    after = order_windows(8, 14, 1, 7, 12.0)
    assert after.days_left_to_buy == 0
    assert after.futures_open is False
    assert (
        after.futures_message
        == "Futures closed: deliveries would arrive after the game ends"
    )
    assert after.futures_delivery_day == 15
    assert order_windows(6, 14, 1, 7, 12.0).days_left_to_buy == 2


def test_days_left_floor_is_zero_late() -> None:
    assert order_windows(14, 14, 1, 7, 12.0).days_left_to_buy == 0


def test_futures_message_uses_formatted_price() -> None:
    w = order_windows(1, 14, 1, 7, 1234.5)
    assert w.futures_message == (
        "Futures: 7 days left to buy (delivery in 7 days, at $1,234.50)"
    )


def test_spot_window() -> None:
    assert order_windows(13, 14, 1, 7, 12.0).spot_open is True
    assert order_windows(13, 14, 1, 7, 12.0).spot_message is None
    last = order_windows(14, 14, 1, 7, 12.0)
    assert last.spot_open is False
    assert last.spot_arrives_day == 15
    assert (
        last.spot_message == "Spot orders closed: they would arrive after the game ends"
    )
    assert order_windows(14, 14, 0, 7, 12.0).spot_open is True
    assert order_windows(14, 14, 0, 7, 12.0).spot_arrives_day == 14


def test_windows_are_frozen() -> None:
    w = order_windows(5, 14, 1, 7, 12.0)
    with pytest.raises(AttributeError):
        w.spot_open = False  # type: ignore[misc]


def _all_params() -> list[tuple[int, int, int, int]]:
    """Exhaustive sweep (hypothesis is not a dev dependency of this repo)."""
    out = []
    for duration in range(8, 61):
        for horizon in range(1, min(14, duration - 1) + 1):
            for lead in range(0, horizon):
                for day in range(1, duration + 1):
                    out.append((day, duration, lead, horizon))
    return out


def test_window_invariants() -> None:
    for day, duration, lead, horizon in _all_params():
        _check(day, duration, lead, horizon)


def _check(day: int, duration: int, lead: int, horizon: int) -> None:
    w = order_windows(day, duration, lead, horizon, 12.0)
    assert w.futures_open == (w.days_left_to_buy > 0)
    assert w.futures_open == (day + horizon <= duration)
    assert w.spot_open == (day + lead <= duration)
    assert w.spot_arrives_day == day + lead
    assert w.futures_delivery_day == day + horizon
    assert w.last_day_to_buy == duration - horizon
    assert w.days_left_to_buy == max(0, duration - horizon - day + 1)
    assert (w.spot_message is None) == w.spot_open
    if w.days_left_to_buy > 1:
        assert w.futures_message.startswith("Futures: ")
    elif w.days_left_to_buy == 1:
        assert w.futures_message == "Last day to buy futures"
    else:
        assert w.futures_message.startswith("Futures closed")
