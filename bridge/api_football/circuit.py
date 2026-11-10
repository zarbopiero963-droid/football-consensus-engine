"""Per-worker bounded circuit breaker; global per-key quota is separate."""
from __future__ import annotations

import asyncio
import time
from typing import Callable
from .errors import CircuitOpen


class CircuitBreaker:
    def __init__(self, *, threshold: int = 2, cooldown: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        if not (1 <= threshold <= 20 and 0.1 <= cooldown <= 300):
            raise ValueError("Invalid breaker policy")
        self.threshold = threshold
        self.cooldown = cooldown
        self.clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._half_open = False
        self._probe_inflight = False
        self._lock = asyncio.Lock()

    async def before_request(self):
        async with self._lock:
            now = self.clock()
            if self._open_until > now:
                raise CircuitOpen()
            if self._open_until > 0 and now >= self._open_until:
                self._half_open = True
            if self._half_open:
                if self._probe_inflight:
                    raise CircuitOpen()
                self._probe_inflight = True

    async def success(self):
        async with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._half_open = False
            self._probe_inflight = False

    async def fail(self):
        async with self._lock:
            self._failures += 1
            if self._half_open or self._failures >= self.threshold:
                self._open_until = self.clock() + self.cooldown
                self._half_open = False
            self._probe_inflight = False

    # Upper bound on a provider-requested pause (Retry-After), as for SX Bet's 429 cooldown.
    MAX_HOLD_SECONDS = 3600

    async def hold(self, seconds: int):
        """Keep the circuit open for at least `seconds` (the provider's Retry-After), bounded.
        Never shortens an already longer opening."""
        if type(seconds) is not int or seconds <= 0:
            return
        async with self._lock:
            self._open_until = max(self._open_until, self.clock() + min(seconds, self.MAX_HOLD_SECONDS))
            self._half_open = False
            self._probe_inflight = False

    async def abort(self):
        async with self._lock:
            self._probe_inflight = False
            if self._half_open:
                # Never grant an unrestricted reset from a cancelled probe.
                self._open_until = self.clock() + self.cooldown
                self._half_open = False

    @property
    def open(self) -> bool:
        return self._open_until > self.clock()
