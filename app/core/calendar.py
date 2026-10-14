"""Calendar and ordering windows (spec §4.2)."""

from dataclasses import dataclass

from app.core.enums import WEEKDAYS, Weekday
from app.core.money import format_money


def weekday_index(weekday: Weekday) -> int:
    return WEEKDAYS.index(weekday) + 1


def weekday_of(day: int) -> Weekday:
    if day < 1:
        raise ValueError("day must be at least 1")
    return WEEKDAYS[(day - 1) % 7]


@dataclass(frozen=True)
class OrderWindows:
    spot_open: bool
    spot_arrives_day: int
    futures_open: bool
    futures_delivery_day: int
    last_day_to_buy: int
    days_left_to_buy: int
    futures_message: str
    spot_message: str | None


def order_windows(
    day: int,
    duration_days: int,
    lead_time_days: int,
    horizon_days: int,
    base_price: float,
) -> OrderWindows:
    spot_arrives = day + lead_time_days
    delivery = day + horizon_days
    last_day = duration_days - horizon_days
    days_left = max(0, last_day - day + 1)
    if days_left > 1:
        futures_message = (
            f"Futures: {days_left} days left to buy "
            f"(delivery in {horizon_days} days, at {format_money(base_price)})"
        )
    elif days_left == 1:
        futures_message = "Last day to buy futures"
    else:
        futures_message = "Futures closed: deliveries would arrive after the game ends"
    spot_open = spot_arrives <= duration_days
    return OrderWindows(
        spot_open=spot_open,
        spot_arrives_day=spot_arrives,
        futures_open=delivery <= duration_days,
        futures_delivery_day=delivery,
        last_day_to_buy=last_day,
        days_left_to_buy=days_left,
        futures_message=futures_message,
        spot_message=(
            None
            if spot_open
            else "Spot orders closed: they would arrive after the game ends"
        ),
    )
