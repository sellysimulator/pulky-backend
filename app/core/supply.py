"""Abstract supplier draw and scarce-capacity allocation (spec 5.1, 5.4)."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import floor
from typing import Protocol, runtime_checkable

from app.core.money import round_cents
from app.core.rng import stream
from app.core.sampling import halfup


@dataclass(frozen=True)
class SupplierDay:
    capacity: int
    disrupted: bool
    spot_price: float


@runtime_checkable
class SupplierModel(Protocol):
    def draw_day(
        self, day: int, aggregate_orders_history: Sequence[int]
    ) -> SupplierDay: ...


class AbstractSupplier:
    def __init__(
        self,
        *,
        seed: int,
        n_players: int,
        capacity_mean_per_player: float,
        capacity_sd_per_player: float,
        disruption_probability: float,
        disruption_factor: float,
        base_price: float,
        spot_spread: float,
    ) -> None:
        self._seed = seed
        self._n = n_players
        self._mean = capacity_mean_per_player
        self._sd = capacity_sd_per_player
        self._dis_p = disruption_probability
        self._dis_f = disruption_factor
        self._base = base_price
        self._spread = spot_spread

    def draw_day(
        self, day: int, aggregate_orders_history: Sequence[int] = ()
    ) -> SupplierDay:
        rng = stream(self._seed, day, "supply")
        mu = self._mean * self._n
        sd = self._sd * self._n
        cap = max(0, halfup(rng.gauss(mu, sd)))
        disrupted = rng.random() < self._dis_p
        if disrupted:
            cap = halfup(cap * self._dis_f)
        spot = round_cents(self._base * (1 + rng.uniform(-self._spread, self._spread)))
        return SupplierDay(capacity=cap, disrupted=disrupted, spot_price=spot)


def _check(requests: Mapping[str, int], capacity: int) -> None:
    if capacity < 0:
        raise ValueError("capacity must be >= 0")
    if any(r < 0 for r in requests.values()):
        raise ValueError("requests must be >= 0")


def _largest_remainder(
    requests: Mapping[str, int], capacity: int, tiebreak: Mapping[str, float]
) -> dict[str, int]:
    total = sum(requests.values())
    f = capacity / total
    base = {a: floor(r * f) for a, r in requests.items()}
    left = capacity - sum(base.values())
    order = sorted(requests, key=lambda a: (-(requests[a] * f - base[a]), tiebreak[a]))
    for a in order[:left]:
        base[a] += 1
    return base


def hamilton(
    requests: Mapping[str, int], capacity: int, rng: random.Random
) -> dict[str, int]:
    _check(requests, capacity)
    if sum(requests.values()) <= capacity:
        return dict(requests)
    tiebreak = {a: rng.random() for a in sorted(requests)}
    return _largest_remainder(requests, capacity, tiebreak)


def allocate(
    requests: Mapping[str, int],
    capacity: int,
    cooperative: bool,
    rng: random.Random,
) -> dict[str, int]:
    _check(requests, capacity)
    if sum(requests.values()) <= capacity:
        return dict(requests)
    tiebreak = {a: rng.random() for a in sorted(requests)}
    if cooperative:
        return _largest_remainder(requests, capacity, tiebreak)
    out: dict[str, int] = {}
    left = capacity
    for a in sorted(requests, key=lambda a: (-requests[a], tiebreak[a])):
        out[a] = min(requests[a], left)
        left -= out[a]
    return out
