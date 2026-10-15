"""Fixed-length FIFO pipeline modelling a delay of L days (spec 5.6)."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Pipeline(Generic[T]):
    """A FIFO of `length` slots; the daily cycle is push then pop_front."""

    def __init__(self, length: int, initial: Sequence[T]) -> None:
        if length < 0:
            raise ValueError("pipeline length must be >= 0")
        if len(initial) != length:
            raise ValueError("initial must hold exactly `length` items")
        self._length = length
        self._items: deque[T] = deque(initial)

    @property
    def length(self) -> int:
        return self._length

    def push(self, item: T) -> None:
        self._items.append(item)

    def pop_front(self) -> T:
        if not self._items:
            raise IndexError("pop from an empty pipeline")
        return self._items.popleft()

    def items(self) -> list[T]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def to_payload(self, encode: Callable[[T], Any]) -> dict:
        return {"length": self._length, "items": [encode(x) for x in self._items]}

    @classmethod
    def from_payload(cls, data: dict, decode: Callable[[Any], T]) -> Pipeline[T]:
        length = data["length"]
        raw = list(data["items"])
        if len(raw) not in (length, length + 1):
            raise ValueError("pipeline payload has an invalid item count")
        if length < 0:
            raise ValueError("pipeline length must be >= 0")
        pipe: Pipeline[T] = cls.__new__(cls)
        pipe._length = length
        pipe._items = deque(decode(x) for x in raw)
        return pipe
