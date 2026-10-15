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


def update_reputation(
    reputation: float,
    fill: float,
    waste: float,
    smoothing: float,
    waste_weight: float,
) -> float:
    day = (1 - waste_weight) * fill + waste_weight * (1 - waste)
    return (1 - smoothing) * reputation + smoothing * day
