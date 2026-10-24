"""Process-local resilience primitives. All state is bounded and in memory.

These limits are per process. The provider's own quota headers are the global
backstop across workers/instances (see QuotaState); a shared coordinator is a
separate, owner-approved change if Render runs more than one process.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any, Callable


class SlidingWindowLimiter:
    """At most `limit` acquisitions per rolling 60 seconds; waits are bounded."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._events and self._events[0] <= now - 60.0:
            self._events.popleft()

    def used(self) -> int:
        self._prune(self._clock())
        return len(self._events)

    async def acquire(self, limit: int, max_wait: float) -> bool:
        deadline = self._clock() + max_wait
        while True:
            async with self._lock:
                now = self._clock()
                self._prune(now)
                if len(self._events) < limit:
                    self._events.append(now)
                    return True
                wait = self._events[0] + 60.0 - now
            if now + wait > deadline:
                return False
            await asyncio.sleep(min(wait, 1.0))


class DailyBudget:
    """Local count of provider HTTP attempts per UTC day."""

    def __init__(self, today: Callable[[], str] | None = None) -> None:
        self._today = today or (lambda: datetime.now(timezone.utc).date().isoformat())
        self.day = self._today()
        self.used = 0

    def _roll(self) -> None:
        day = self._today()
        if day != self.day:
            self.day, self.used = day, 0

    def available(self, limit: int) -> bool:
        self._roll()
        return self.used < limit

    def consume(self) -> None:
        self._roll()
        self.used += 1


class CircuitBreaker:
    """CLOSED -> OPEN after consecutive failures; one HALF_OPEN probe after cooldown."""

    def __init__(self, threshold: int = 5, cooldown: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.open_for = cooldown
        self.probe_in_flight = False
        self.last_reason: str | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        if self._clock() - self.opened_at >= self.open_for:
            return "HALF_OPEN"
        return "OPEN"

    def retry_after(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.open_for - (self._clock() - self.opened_at))

    def allow(self) -> bool:
        state = self.state
        if state == "CLOSED":
            return True
        if state == "HALF_OPEN" and not self.probe_in_flight:
            self.probe_in_flight = True
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.probe_in_flight = False
        self.last_reason = None

    def failure(self, reason: str) -> None:
        self.failures += 1
        self.last_reason = reason
        if self.probe_in_flight or self.failures >= self.threshold:
            self.trip(reason, self.cooldown)
        self.probe_in_flight = False

    def trip(self, reason: str, seconds: float) -> None:
        self.opened_at = self._clock()
        self.open_for = seconds
        self.last_reason = reason
        self.probe_in_flight = False

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "consecutive_failures": self.failures,
                "retry_after_seconds": round(self.retry_after(), 1), "last_reason": self.last_reason}


class TTLCache:
    """Bounded LRU cache of successful results only."""

    def __init__(self, max_entries: int = 256, max_total_bytes: int = 64 * 1024 * 1024,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_entries = max_entries
        self.max_total_bytes = max_total_bytes
        self._clock = clock
        self._items: OrderedDict[Any, tuple[float, float, int, Any]] = OrderedDict()
        self.total_bytes = 0

    def get(self, key) -> tuple[Any, float] | None:
        item = self._items.get(key)
        if item is None:
            return None
        stored, expires, size, value = item
        now = self._clock()
        if now >= expires:
            self._drop(key)
            return None
        self._items.move_to_end(key)
        return value, now - stored

    def put(self, key, value, ttl: float, size: int) -> None:
        if ttl <= 0 or size > self.max_total_bytes // 4:
            return
        if key in self._items:
            self._drop(key)
        now = self._clock()
        self._items[key] = (now, now + ttl, size, value)
        self.total_bytes += size
        while len(self._items) > self.max_entries or self.total_bytes > self.max_total_bytes:
            self._drop(next(iter(self._items)))

    def _drop(self, key) -> None:
        _stored, _expires, size, _value = self._items.pop(key)
        self.total_bytes -= size

    def __len__(self) -> int:
        return len(self._items)


class QuotaState:
    """Last provider-reported counters. Provider headers are global for the key."""

    DAILY_LIMIT = "x-ratelimit-requests-limit"
    DAILY_REMAINING = "x-ratelimit-requests-remaining"
    MINUTE_LIMIT = "x-ratelimit-limit"
    MINUTE_REMAINING = "x-ratelimit-remaining"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.daily_limit: int | None = None
        self.daily_remaining: int | None = None
        self.minute_limit: int | None = None
        self.minute_remaining: int | None = None
        self.observed_at_utc: str | None = None
        self._observed_monotonic: float | None = None
        self.daily_exhausted_day: str | None = None

    @staticmethod
    def _int(headers, name) -> int | None:
        raw = headers.get(name)
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    def update(self, headers) -> None:
        values = {name: self._int(headers, name) for name in
                  (self.DAILY_LIMIT, self.DAILY_REMAINING, self.MINUTE_LIMIT, self.MINUTE_REMAINING)}
        if all(value is None for value in values.values()):
            return
        # Absent headers stay unknown; they never become zero.
        self.daily_limit = values[self.DAILY_LIMIT]
        self.daily_remaining = values[self.DAILY_REMAINING]
        self.minute_limit = values[self.MINUTE_LIMIT]
        self.minute_remaining = values[self.MINUTE_REMAINING]
        self.observed_at_utc = datetime.now(timezone.utc).isoformat()
        self._observed_monotonic = self._clock()

    def reserve_reached(self, reserve_percent: int) -> bool:
        if self.daily_exhausted_day == datetime.now(timezone.utc).date().isoformat():
            return True
        if self.daily_remaining is None or self.daily_limit is None:
            return False
        reserve = -(-self.daily_limit * reserve_percent // 100)
        return self.daily_remaining <= reserve

    def minute_blocked(self) -> bool:
        return (self.minute_remaining == 0 and self._observed_monotonic is not None
                and self._clock() - self._observed_monotonic < 60.0)

    def snapshot(self) -> dict[str, Any]:
        return {"daily_limit": self.daily_limit, "daily_remaining": self.daily_remaining,
                "minute_limit": self.minute_limit, "minute_remaining": self.minute_remaining,
                "observed_at_utc": self.observed_at_utc, "source": "provider_response_headers",
                "daily_exhausted_day_utc": self.daily_exhausted_day}
