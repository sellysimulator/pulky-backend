"""Quantity rounding and the Poisson sampler (spec §5.1, §7.3)."""

import math
import random


def halfup(x: float) -> int:
    return math.floor(x + 0.5)


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        # Python round(): the one exception to halfup (spec §7.3).
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    threshold = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1
