"""A player's futures book and the first-claim fill (spec 5.3)."""

import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.core.supply import hamilton


@dataclass(frozen=True)
class Contract:
    contract_day: int
    ships_day: int
    delivery_day: int
    qty: int

    def to_payload(self) -> dict:
        return {
            "contract_day": self.contract_day,
            "ships_day": self.ships_day,
            "delivery_day": self.delivery_day,
            "qty": self.qty,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "Contract":
        return cls(
            contract_day=int(data["contract_day"]),
            ships_day=int(data["ships_day"]),
            delivery_day=int(data["delivery_day"]),
            qty=int(data["qty"]),
        )


def make_contract(
    day: int, qty: int, horizon_days: int, lead_time_days: int
) -> Contract:
    if qty <= 0:
        raise ValueError("a futures contract needs a positive quantity")
    if lead_time_days >= horizon_days:
        raise ValueError("lead time must be shorter than the futures horizon")
    return Contract(
        contract_day=day,
        ships_day=day + horizon_days - lead_time_days,
        delivery_day=day + horizon_days,
        qty=qty,
    )


class FuturesBook:
    def __init__(self, contracts: Iterable[Contract] = ()) -> None:
        self._contracts: list[Contract] = []
        for contract in contracts:
            self.add(contract)

    @property
    def contracts(self) -> tuple[Contract, ...]:
        return tuple(self._contracts)

    def add(self, contract: Contract) -> None:
        if any(c.contract_day == contract.contract_day for c in self._contracts):
            raise ValueError("a contract already exists for that contract day")
        self._contracts.append(contract)
        self._contracts.sort(key=lambda c: c.contract_day)

    def due(self, day: int) -> int:
        return sum(c.qty for c in self._contracts if c.ships_day == day)

    def take_due(self, day: int) -> int:
        total = self.due(day)
        self._contracts = [c for c in self._contracts if c.ships_day != day]
        return total

    def open_units(self) -> int:
        return sum(c.qty for c in self._contracts)

    def cancel_all(self) -> int:
        total = self.open_units()
        self._contracts = []
        return total

    def view(self) -> list[dict]:
        return self.to_payload()

    def to_payload(self) -> list[dict]:
        return [c.to_payload() for c in self._contracts]

    @classmethod
    def from_payload(cls, data: list[dict[str, Any]]) -> "FuturesBook":
        return cls(Contract.from_payload(d) for d in data)


def fill_futures(
    due: Mapping[str, int], capacity: int, rng: random.Random
) -> dict[str, int]:
    return hamilton(due, capacity, rng)
