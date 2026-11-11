"""Bounded, TTL-aware in-process cache; never caches credentials or errors."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import json
import time
from typing import Any, Callable


def ttl_for_endpoint(path: str) -> float:
    if path.startswith("/odds/live") or path in ("/fixtures/events", "/fixtures/statistics"):
        return 5.0
    if path == "/fixtures":
        return 15.0
    if path == "/odds":
        return 180.0
    if path in ("/leagues", "/leagues/seasons", "/timezone", "/countries"):
        return 3600.0
    if path == "/status":
        return 0.0  # Account quota must never be trusted from an old cached response.
    return 45.0


@dataclass
class _Record:
    expires_at: float
    size: int
    value: Any
    stored_at: float = 0.0


class BoundedCache:
    def __init__(self, *, max_entries: int = 128, max_bytes: int = 2_097_152,
                 max_entry_bytes: int = 262_144, clock: Callable[[], float] = time.monotonic):
        if min(max_entries, max_bytes, max_entry_bytes) <= 0 or max_entry_bytes > max_bytes:
            raise ValueError("Invalid cache limits")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.clock = clock
        self._data: OrderedDict[tuple, _Record] = OrderedDict()
        self._size = 0

    def get(self, key: tuple):
        entry = self.get_with_age(key)
        return None if entry is None else entry[0]

    def get_with_age(self, key: tuple):
        """(value, seconds since it was stored) or None; the age is from the cache's own clock."""
        item = self._data.get(key)
        if item is None:
            return None
        now = self.clock()
        if item.expires_at <= now:
            self._evict(key)
            return None
        self._data.move_to_end(key)
        return deepcopy(item.value), max(0.0, now - item.stored_at)

    def put(self, key: tuple, value: Any, ttl: float) -> bool:
        if ttl <= 0:
            return False
        try:
            size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        except (ValueError, TypeError):
            return False
        if size > self.max_entry_bytes:
            return False
        self._evict(key)
        while self._data and (len(self._data) >= self.max_entries or self._size + size > self.max_bytes):
            self._evict(next(iter(self._data)))
        now = self.clock()
        self._data[key] = _Record(now + ttl, size, deepcopy(value), now)
        self._size += size
        return True

    def _evict(self, key: tuple) -> None:
        item = self._data.pop(key, None)
        if item is not None:
            self._size -= item.size

    def clear(self) -> None:
        self._data.clear()
        self._size = 0
