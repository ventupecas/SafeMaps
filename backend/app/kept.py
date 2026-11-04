"""The latest few results of something costly, by key, for trips planned at once."""

from __future__ import annotations

from collections.abc import Hashable
from threading import Lock
from typing import Generic, TypeVar

T = TypeVar("T")


class Kept(Generic[T]):
    """Reading takes no lock, as a dict read is safe across threads. Dropping the oldest has to be done by one
    thread at a time: looking for the oldest fails if another thread adds something at that moment."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._items: dict[Hashable, T] = {}
        self._lock = Lock()

    def get(self, key: Hashable) -> T | None:
        return self._items.get(key)

    def put(self, key: Hashable, value: T) -> None:
        with self._lock:
            if key not in self._items and len(self._items) >= self.limit:
                del self._items[next(iter(self._items))]
            self._items[key] = value

    def __len__(self) -> int:
        return len(self._items)
