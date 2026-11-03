"""Fill, waste and the reputation update (spec §7.6)."""


def fill_ratio(sold: int, owed: int) -> float:
    if owed == 0:
        return 1.0
    return sold / owed


def waste_ratio(spoiled: int, sold: int) -> float:
    available = sold + spoiled
    if available == 0:
        return 0.0
    return spoiled / available


def day_score(fill: float, waste: float, waste_weight: float) -> float:
    """One day's score: serving what is owed against spoiling (spec 7.6)."""
    return (1 - waste_weight) * fill + waste_weight * (1 - waste)


def update_reputation(
    reputation: float,
    fill: float,
    waste: float,
    smoothing: float,
    waste_weight: float,
) -> float:
    day = day_score(fill, waste, waste_weight)
    return (1 - smoothing) * reputation + smoothing * day
