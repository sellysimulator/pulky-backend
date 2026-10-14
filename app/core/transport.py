"""Refrigeration failure and who loses what (spec section 6.2)."""

import math
import random
from collections.abc import Mapping


def draw_failure(rng: random.Random, failure_probability: float) -> bool:
    return rng.random() < failure_probability


def transport_losses(
    shipped: Mapping[str, int],
    failure: bool,
    loss_fraction: float,
    shared: bool,
) -> dict[str, int]:
    if not failure:
        return {alias: 0 for alias in shipped}
    shippers = sum(1 for qty in shipped.values() if qty > 0)
    n = max(shippers, 1) if shared else 1
    return {
        alias: (math.floor(qty * loss_fraction / n + 1e-9) if qty > 0 else 0)
        for alias, qty in shipped.items()
    }
