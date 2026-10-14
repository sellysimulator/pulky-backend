"""Stock: lots, the cellar, nightly spoilage and quality (spec section 3)."""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.core.money import round_cents


def hazard(
    age: int, fresh_days: int, max_days: int, initial_quality: int = 100
) -> float:
    """Spoil risk at the check of a lot of this age (spec 3.3)."""
    ramp = max_days - fresh_days
    k = max(0, age - fresh_days)
    if 100 * k >= initial_quality * ramp:
        return 1.0
    if initial_quality == 100 and k == 0:
        return 0.0
    return min(1.0, (1 - initial_quality / 100) + k / ramp)


def quality(age: int, fresh_days: int, max_days: int, initial_quality: int) -> int:
    """Whole-percent quality at tonight's check (spec 3.5), in integers."""
    ramp = max_days - fresh_days
    k = max(0, age + 1 - fresh_days)
    return max(0, (2 * ramp * initial_quality - 200 * k + ramp) // (2 * ramp))


def days_left(age: int, fresh_days: int, max_days: int, initial_quality: int) -> int:
    """Days a lot can still be sold, counting today (spec 3.5)."""
    ramp = max_days - fresh_days
    a_star = max(age + 1, fresh_days + (initial_quality * ramp + 99) // 100)
    return a_star - age


def quality_guide(day: int, fresh_days: int, max_days: int) -> list[dict]:
    """Half-spoiled day and last day per quality, 100% down to 0% (spec 3.5)."""
    ramp = max_days - fresh_days
    rows: list[dict] = []
    for q in range(100, -1, -5):
        survival = 1.0
        j = 0
        while True:
            j += 1
            q_j = max(0.0, q - (j - 1) * 100 / ramp)
            survival *= q_j / 100
            if survival <= 0.5 + 1e-9:
                break
        k_sure = (q * ramp + 99) // 100 + 1
        rows.append(
            {
                "quality": q,
                "half_spoiled_day": day + j - 1,
                "last_day": day + k_sure - 1,
            }
        )
    return rows


@dataclass
class Lot:
    id: str
    qty: int
    age: int
    unit_cost: float
    initial_quality: int
    fresh_days: int
    max_days: int
    conserved: bool

    def risk_tonight(self) -> float:
        return hazard(
            self.age + 1, self.fresh_days, self.max_days, self.initial_quality
        )

    def quality(self) -> int:
        return quality(self.age, self.fresh_days, self.max_days, self.initial_quality)

    def days_left(self) -> int:
        return days_left(self.age, self.fresh_days, self.max_days, self.initial_quality)

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "qty": self.qty,
            "age": self.age,
            "unit_cost": self.unit_cost,
            "initial_quality": self.initial_quality,
            "fresh_days": self.fresh_days,
            "max_days": self.max_days,
            "conserved": self.conserved,
        }

    @classmethod
    def from_payload(cls, data: dict) -> Lot:
        return cls(
            id=str(data["id"]),
            qty=int(data["qty"]),
            age=int(data["age"]),
            unit_cost=float(data["unit_cost"]),
            initial_quality=int(data["initial_quality"]),
            fresh_days=int(data["fresh_days"]),
            max_days=int(data["max_days"]),
            conserved=bool(data["conserved"]),
        )


class Cellar:
    """A player's lots, oldest first."""

    def __init__(self, lots: Iterable[Lot] = ()) -> None:
        self._lots: list[Lot] = list(lots)

    @property
    def lots(self) -> tuple[Lot, ...]:
        return tuple(self._lots)

    def total(self) -> int:
        return sum(lot.qty for lot in self._lots)

    def get(self, lot_id: str) -> Lot | None:
        for lot in self._lots:
            if lot.id == lot_id:
                return lot
        return None

    def _check_new(self, lot: Lot) -> None:
        if lot.qty <= 0:
            raise ValueError("lot quantity must be positive")
        if self.get(lot.id) is not None:
            raise ValueError(f"lot {lot.id} is already in the cellar")

    def add(self, lot: Lot) -> None:
        self._check_new(lot)
        self._lots.append(lot)

    def insert(self, lot: Lot) -> None:
        self._check_new(lot)
        index = len(self._lots)
        for i, stored in enumerate(self._lots):
            if stored.age < lot.age:
                index = i
                break
        self._lots.insert(index, lot)

    def take(self, lot_id: str, qty: int) -> Lot:
        lot = self.get(lot_id)
        if lot is None:
            raise ValueError(f"unknown lot {lot_id}")
        if qty < 1 or qty > lot.qty:
            raise ValueError("invalid quantity to take")
        taken = replace(lot, qty=qty)
        lot.qty -= qty
        if lot.qty == 0:
            self._lots.remove(lot)
        return taken

    def consume(self, qty: int) -> None:
        if qty < 0 or qty > self.total():
            raise ValueError("invalid quantity to consume")
        remaining = qty
        for lot in self._lots:
            if remaining == 0:
                break
            used = min(lot.qty, remaining)
            lot.qty -= used
            remaining -= used
        self._lots = [lot for lot in self._lots if lot.qty > 0]

    def conserve(self, lot_ids: Sequence[str], extra_days: int) -> int:
        chosen: list[Lot] = []
        for lot_id in lot_ids:
            lot = self.get(lot_id)
            if lot is None:
                raise ValueError(f"unknown lot {lot_id}")
            if lot.conserved or any(lot is c for c in chosen):
                raise ValueError(f"lot {lot_id} cannot be conserved")
            chosen.append(lot)
        for lot in chosen:
            lot.fresh_days += extra_days
            lot.max_days += extra_days
            lot.conserved = True
        return sum(lot.qty for lot in chosen)

    def nightly_spoil(self, rng: random.Random) -> int:
        spoiled = 0
        for lot in self._lots:
            lot.age += 1
            p = hazard(lot.age, lot.fresh_days, lot.max_days, lot.initial_quality)
            if p >= 1:
                spoiled += lot.qty
                lot.qty = 0
            elif p > 0:
                lost = sum(1 for _ in range(lot.qty) if rng.random() < p)
                spoiled += lost
                lot.qty -= lost
        self._lots = [lot for lot in self._lots if lot.qty > 0]
        return spoiled

    def expected_spoil_tonight(self) -> float:
        return round(sum(lot.qty * lot.risk_tonight() for lot in self._lots), 2)

    def view(self, day: int) -> list[dict]:
        rows: list[dict] = []
        for lot in self._lots:
            risk = lot.risk_tonight()
            rows.append(
                {
                    "id": lot.id,
                    "qty": lot.qty,
                    "age": lot.age,
                    "unit_cost": round_cents(lot.unit_cost),
                    "initial_quality": lot.initial_quality,
                    "conserved": lot.conserved,
                    "quality": lot.quality(),
                    "last_day": day + lot.days_left() - 1,
                    "risk_tonight": round(risk, 4),
                    "expected_loss_tonight": round(lot.qty * risk, 2),
                }
            )
        return rows

    def stock_by_age(self) -> list[dict]:
        rows: list[dict] = []
        for age in sorted({lot.age for lot in self._lots}):
            entry: dict[str, Any] = {"age": age}
            for conserved, prefix in ((False, "plain"), (True, "conserved")):
                group = [
                    lot
                    for lot in self._lots
                    if lot.age == age and lot.conserved == conserved
                ]
                units = sum(lot.qty for lot in group)
                entry[f"{prefix}_qty"] = units
                entry[f"{prefix}_risk_tonight"] = (
                    round(sum(lot.qty * lot.risk_tonight() for lot in group) / units, 4)
                    if units
                    else None
                )
            rows.append(
                {
                    "age": age,
                    "plain_qty": entry["plain_qty"],
                    "conserved_qty": entry["conserved_qty"],
                    "plain_risk_tonight": entry["plain_risk_tonight"],
                    "conserved_risk_tonight": entry["conserved_risk_tonight"],
                }
            )
        return rows

    def to_payload(self) -> list[dict]:
        return [lot.to_payload() for lot in self._lots]

    @classmethod
    def from_payload(cls, data: list[dict]) -> Cellar:
        return cls(Lot.from_payload(item) for item in data)
