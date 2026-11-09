"""In-process sliding-window limits for the unauthenticated OAuth endpoints (WP-022a, G-03/G-12).

Two independent windows are checked for every guarded event:
- per client address: stops a single source from guessing or flooding;
- global: the backstop, also when the address is spoofed or rotated (IPv6, proxies).

The production bridge runs as ONE process (Render, numInstances=1), so in-process state is
authoritative there; it is reset by a restart. A PostgreSQL-backed limiter (survives restarts,
works across workers) is planned with the next owner-gated migration batch (0009), together
with refresh-token families; until then a restart grants at most one extra window.

Values are never logged; keys are client addresses only (no password, token or client id).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

GLOBAL = "*"


class WindowLimiter:
    """At most `limit` recorded events per key within `window` seconds."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 10_000):
        if limit < 1 or window <= 0:
            raise ValueError("limit and window must be positive")
        self.limit, self.window, self._clock, self._max_keys = limit, window, clock, max_keys
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        q = self._events.get(key)
        if q is None:
            return deque()
        while q and now - q[0] >= self.window:
            q.popleft()
        if not q:
            self._events.pop(key, None)
        return q

    def retry_after(self, key: str) -> int | None:
        """Seconds until `key` may act again, or None when it is not limited."""
        with self._lock:
            now = self._clock()
            q = self._trim(key, now)
            if len(q) < self.limit:
                return None
            return max(1, int(self.window - (now - q[0])) + 1)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def record(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            q = self._trim(key, now)
            if key not in self._events:
                if len(self._events) >= self._max_keys:
                    # Bounded memory: drop the stalest key (never the global one).
                    stalest = min((k for k in self._events if k != GLOBAL),
                                  key=lambda k: self._events[k][-1], default=None)
                    if stalest is not None:
                        self._events.pop(stalest, None)
                self._events[key] = q
            q.append(now)


class DualLimiter:
    """A per-address window plus a global window; blocked when either is exhausted."""

    def __init__(self, per_key: WindowLimiter, global_: WindowLimiter):
        self.per_key, self.global_ = per_key, global_

    def retry_after(self, key: str) -> int | None:
        waits = [w for w in (self.per_key.retry_after(key), self.global_.retry_after(GLOBAL)) if w]
        return max(waits) if waits else None

    def record(self, key: str) -> None:
        self.per_key.record(key)
        self.global_.record(GLOBAL)

    def reset(self) -> None:
        self.per_key.reset()
        self.global_.reset()


def reset_all() -> None:
    """Test isolation only; the process has no runtime caller."""
    CONSENT_FAILURES.reset()
    REGISTRATIONS.reset()


# Consent password: failures only. 5 per address per 15 min; 50 in total per hour.
CONSENT_FAILURES = DualLimiter(WindowLimiter(5, 900), WindowLimiter(50, 3600))
# Dynamic client registration: every attempt. 10 per address per hour; 100 in total per hour.
REGISTRATIONS = DualLimiter(WindowLimiter(10, 3600), WindowLimiter(100, 3600))


def client_address(request) -> str:
    """Best-effort client address for the per-address windows.

    On Render the path is client -> Cloudflare -> Render proxy -> app, and X-Forwarded-For
    reads "<client>, <Cloudflare edge>": its LAST hop is a shared Cloudflare edge address, not
    the client (review of #177, P1). Cloudflare sets CF-Connecting-IP itself, overwriting any
    client-sent value, so it is used first. Without it, the FIRST X-Forwarded-For hop is used:
    it is client-controlled, which can only split an attacker across keys (the global window
    still bounds them) and never merges unrelated users into one shared bucket.
    """
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf[:64]
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    if hops:
        return hops[0][:64]
    client = getattr(request, "client", None)
    return (getattr(client, "host", None) or "unknown")[:64]
