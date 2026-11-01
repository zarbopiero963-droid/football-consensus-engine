"""SX Bet request admission: documented rate buckets, local and shared coordinators.

Every SX Bet request is admitted against a *plan* of buckets before any network
call (governance §2.4):

* ``global``: the per-key budget ``SXBET_MAX_RPM`` (1..600 per 60 s) applied to
  every SX Bet request of this deployment;
* the documented per-route budget, when https://docs.sx.bet/developers/rate-limits
  publishes one (per API key for authenticated routes, per client IP for public
  routes, including the shared "markets & sports data" 500/60 s family);
* the documented edge bucket for unauthenticated traffic (35,000 per 10 min).

A request is admitted only if *every* bucket of its plan has capacity, so the
effective rate is never higher than min(local configured, documented). Routes
the documentation does not list use the global budget only. Admitted slots are
always consumed, also when the call later fails (conservative).

A provider 429 stores a cooldown (``Retry-After``, default 60 s, capped at
3600 s) on the most specific bucket of the plan; while it is active every
process refuses that bucket without calling the provider.

``PostgresRateCoordinator`` is the production coordinator (multi-process /
multi-instance): one transaction-scoped advisory lock serializes reservations,
the database clock is the only clock, and windows are exact sliding windows.
``LocalRateLimiter`` keeps the same semantics in-process for tests/local runs
without ``DATABASE_URL``. Bucket keys carry only a SHA-256 of the API key.

The schema source of truth is ``sql/migrations/0007_sxbet_rate_limits.sql``;
``_SCHEMA`` repeats it statement by statement (pinned by tests) so a process
can ensure it idempotently on a database the migration runner has not reached.
This module has no database or network side effects when imported.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

DOCS = "https://docs.sx.bet/developers/rate-limits"
DEFAULT_COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 3600
# Longest documented window (edge bucket: 10 min) plus margin; older rows are pruned.
_RETENTION_SECONDS = 900


class RateLimited(Exception):
    """A bucket of the plan has no capacity or is cooling down: do not call the provider."""

    def __init__(self, retry_after: int, *, reason: str):
        super().__init__(reason)
        self.retry_after = max(1, int(retry_after))
        self.reason = reason


@dataclass(frozen=True)
class Bucket:
    key: str
    limit: int
    window_seconds: int
    kind: str  # "global" | "route" | "edge"

    def __post_init__(self):
        if not (1 <= self.limit <= 1_000_000 and 1 <= self.window_seconds <= 3600):
            raise ValueError("bucket out of bounds")


@dataclass(frozen=True)
class RatePlan:
    buckets: tuple[Bucket, ...]
    cooldown_key: str

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(b.key for b in self.buckets)


# ---- documented limits (https://docs.sx.bet/developers/rate-limits, GET routes) ----
# (name, matcher, limit, window_seconds, scope); scope "key" = per API key, "ip" = per client IP.
# Order matters only for readability: matchers are mutually exclusive.
_ID = r"[^/]+"
_DOCUMENTED: tuple[tuple[str, re.Pattern[str], int, int, str], ...] = tuple(
    (name, re.compile(pattern), limit, window, scope)
    for name, pattern, limit, window, scope in (
        # Authenticated limits: "Each row is the per-API-key budget for that route."
        ("GET /orders-v3", r"^/orders-v3$", 1200, 60, "key"),
        ("GET /orders-v3/{orderId}", rf"^/orders-v3/(?!public$|odds$|client$){_ID}$", 1200, 60, "key"),
        ("GET /orders-v3/client/{clientOrderId}", rf"^/orders-v3/client/{_ID}$", 1200, 60, "key"),
        ("GET /orderbook-v3/snapshot/market", r"^/orderbook-v3/snapshot/market$", 27500, 300, "key"),
        ("GET /fills-v3", r"^/fills-v3$", 300, 60, "key"),
        ("GET /positions-v3", r"^/positions-v3$", 300, 60, "key"),
        ("GET /trades-v3", r"^/trades-v3$", 300, 60, "key"),
        ("GET /trades-v3/{tradeId}", rf"^/trades-v3/(?!public$){_ID}$", 300, 60, "key"),
        ("GET /positions-v3/settled/pnl", r"^/positions-v3/settled/pnl$", 120, 60, "key"),
        ("GET /user/balance-v3", r"^/user/balance-v3$", 300, 60, "key"),
        ("GET /user/fees-v3", r"^/user/fees-v3$", 120, 60, "key"),
        ("GET /user/realtime-token-v3/api-key", r"^/user/realtime-token-v3/api-key$", 30, 60, "key"),
        ("GET /user/proxy", r"^/user/proxy$", 120, 60, "key"),
        ("GET /user/pending-deploy-proxy", r"^/user/pending-deploy-proxy$", 120, 60, "key"),
        ("GET /user/transfer-to-proxy/pending", r"^/user/transfer-to-proxy/pending$", 120, 60, "key"),
        ("GET /user/transfer-to-proxy/status", r"^/user/transfer-to-proxy/status$", 120, 60, "key"),
        ("/user/multisig/*", r"^/user/multisig/.+$", 60, 60, "key"),
        # Public limits: "limited per client IP at the edge".
        ("GET /orders-v3/odds/best", r"^/orders-v3/odds/best$", 5500, 60, "ip"),
        ("GET /metadata/obv3", r"^/metadata/obv3$", 500, 60, "ip"),
        ("GET /summary-v3/*", r"^/summary-v3/.+$", 500, 60, "ip"),
        ("GET /trades-v3/public", r"^/trades-v3/public$", 200, 60, "ip"),
        ("GET /trades-v3/public/count", r"^/trades-v3/public/count$", 200, 60, "ip"),
        # "These endpoint families share a single combined budget of 500 requests per 60s per client IP."
        ("markets_and_sports_data", r"^(/markets/.+|/fixture/.+|/leagues|/leagues/.+|/sports|/teams|/live-scores|/search)$",
         500, 60, "ip"),
    )
)
# "all unauthenticated traffic shares a single global bucket at the edge of 35,000 requests per 10 min window."
_EDGE = ("unauthenticated_edge", 35000, 600)


def documented_route(path: str) -> tuple[str, int, int, str] | None:
    """(name, limit, window_seconds, scope) for a documented GET route, else None."""
    for name, pattern, limit, window, scope in _DOCUMENTED:
        if pattern.fullmatch(path):
            return name, limit, window, scope
    return None


def key_hash(api_key: str) -> str:
    # SHA-256 fingerprint only: the plaintext key never reaches a bucket key or the database.
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest() if api_key else "none"


def build_plan(*, network: str, api_key: str, max_rpm: int, path: str, auth: bool) -> RatePlan:
    if not 1 <= max_rpm <= 600:
        raise ValueError("SXBET_MAX_RPM out of bounds 1..600")
    prefix = f"sxbet:v1:{network}"
    owner = f"{prefix}:key:{key_hash(api_key)}"
    glob = Bucket(f"{owner}:global", max_rpm, 60, "global")
    buckets = [glob]
    cooldown_key = glob.key
    route = documented_route(path)
    if route is not None:
        name, limit, window, scope = route
        scope_prefix = owner if scope == "key" else f"{prefix}:ip"
        specific = Bucket(f"{scope_prefix}:route:{name}", limit, window, "route")
        buckets.append(specific)
        cooldown_key = specific.key
    if not auth:
        name, limit, window = _EDGE
        buckets.append(Bucket(f"{prefix}:ip:{name}", limit, window, "edge"))
    return RatePlan(tuple(buckets), cooldown_key)


def cooldown_seconds(retry_after: int | None) -> int:
    if retry_after is None or retry_after < 1:
        return DEFAULT_COOLDOWN_SECONDS
    return min(int(retry_after), MAX_COOLDOWN_SECONDS)


class LocalRateLimiter:
    """In-process sliding windows with the same plan semantics (no DATABASE_URL only)."""

    kind = "LOCAL_PROCESS"

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self.clock = clock
        self._calls: dict[str, deque[float]] = {}
        self._cooldowns: dict[str, float] = {}
        self._lock = threading.Lock()

    def _default_plan(self) -> RatePlan:
        b = Bucket("local:global", self.per_minute, 60, "global")
        return RatePlan((b,), b.key)

    async def acquire(self, plan: RatePlan | None = None) -> None:
        plan = plan or self._default_plan()
        with self._lock:
            now = self.clock()
            waits = [until - now for key, until in self._cooldowns.items()
                     if key in plan.keys and until > now]
            if waits:
                raise RateLimited(math.ceil(max(waits)), reason="COOLDOWN")
            for b in plan.buckets:
                limit = min(b.limit, self.per_minute) if b.kind == "global" else b.limit
                calls = self._calls.setdefault(b.key, deque())
                while calls and now - calls[0] >= b.window_seconds:
                    calls.popleft()
                if len(calls) >= limit:
                    raise RateLimited(int(b.window_seconds - (now - calls[0])) + 1, reason="WINDOW_FULL")
            for b in plan.buckets:
                self._calls[b.key].append(now)

    async def record_rate_limited(self, plan: RatePlan, retry_after: int | None) -> None:
        with self._lock:
            until = self.clock() + cooldown_seconds(retry_after)
            self._cooldowns[plan.cooldown_key] = max(until, self._cooldowns.get(plan.cooldown_key, until))


# Runtime copy of sql/migrations/0007_sxbet_rate_limits.sql (the source of truth).
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sxbet_rate_attempts (
    attempt_id TEXT PRIMARY KEY,
    bucket_key TEXT NOT NULL CHECK (bucket_key LIKE 'sxbet:v1:%' AND length(bucket_key) <= 256),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
)""",
    """CREATE INDEX IF NOT EXISTS idx_sxbet_rate_attempts_bucket
ON sxbet_rate_attempts(bucket_key, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_sxbet_rate_attempts_created
ON sxbet_rate_attempts(created_at)""",
    """CREATE TABLE IF NOT EXISTS sxbet_rate_cooldowns (
    bucket_key TEXT PRIMARY KEY CHECK (bucket_key LIKE 'sxbet:v1:%' AND length(bucket_key) <= 256),
    cooldown_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
)""",
)

_SCHEMA_LOCK_ID = 0x5358425343484D41  # "SXBSCHMA": schema DDL only, never nested in the rate lock
_RATE_LOCK_ID = 0x5358425241544531  # "SXBRATE1": serializes every SX Bet reservation


def _default_connect():
    from app.db import connect
    return connect()


def _value(row, name: str):
    return row[name] if isinstance(row, dict) else row[0]


class PostgresRateCoordinator:
    """Shared, fail-closed admission for all workers/instances on one PostgreSQL."""

    kind = "POSTGRES_SHARED"

    def __init__(self, *, connect: Callable = _default_connect):
        self._connect = connect
        self._schema_ready = False
        self._schema_guard = threading.Lock()

    def _ensure_schema(self) -> None:
        # DDL never overlaps the reservation lock (deadlock lesson from api_football quota).
        if self._schema_ready:
            return
        with self._schema_guard:
            if self._schema_ready:
                return
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_ID,))
                    for statement in _SCHEMA:
                        cur.execute(statement)
                conn.commit()
            self._schema_ready = True

    async def prepare(self) -> None:
        if self._schema_ready:
            return
        await asyncio.to_thread(self._ensure_schema)

    def _reserve(self, plan: RatePlan) -> None:
        self._ensure_schema()
        keys = list(plan.keys)
        token = secrets.token_urlsafe(18)
        with self._connect() as conn:
            with conn.cursor() as cur:
                # Bound server-side waits too; the caller has its own wall-clock budget.
                cur.execute("SET LOCAL lock_timeout = '2500ms'")
                cur.execute("SET LOCAL statement_timeout = '2500ms'")
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_RATE_LOCK_ID,))
                cur.execute(
                    "DELETE FROM sxbet_rate_attempts "
                    "WHERE created_at <= clock_timestamp() - make_interval(secs => %s)",
                    (_RETENTION_SECONDS,),
                )
                cur.execute("DELETE FROM sxbet_rate_cooldowns WHERE cooldown_until <= clock_timestamp()")
                cur.execute(
                    "SELECT CEIL(MAX(EXTRACT(EPOCH FROM cooldown_until - clock_timestamp())))::INTEGER AS wait "
                    "FROM sxbet_rate_cooldowns WHERE bucket_key = ANY(%s) AND cooldown_until > clock_timestamp()",
                    (keys,),
                )
                wait = _value(cur.fetchone(), "wait")
                if wait is not None:
                    raise RateLimited(int(wait), reason="COOLDOWN")
                for b in plan.buckets:
                    cur.execute(
                        "SELECT count(*) AS n, "
                        "CEIL(EXTRACT(EPOCH FROM min(created_at) + make_interval(secs => %s) - clock_timestamp()))"
                        "::INTEGER AS wait "
                        "FROM sxbet_rate_attempts "
                        "WHERE bucket_key=%s AND created_at > clock_timestamp() - make_interval(secs => %s)",
                        (b.window_seconds, b.key, b.window_seconds),
                    )
                    row = cur.fetchone()
                    used = int(row["n"] if isinstance(row, dict) else row[0])
                    if used >= b.limit:
                        later = row["wait"] if isinstance(row, dict) else row[1]
                        raise RateLimited(int(later or 1), reason="WINDOW_FULL")
                for i, b in enumerate(plan.buckets):
                    cur.execute(
                        "INSERT INTO sxbet_rate_attempts (attempt_id, bucket_key) VALUES (%s, %s)",
                        (f"{token}-{i}", b.key),
                    )
            conn.commit()

    async def acquire(self, plan: RatePlan) -> None:
        # Never blocks the MCP event loop.
        await asyncio.to_thread(self._reserve, plan)

    def _record(self, plan: RatePlan, retry_after: int | None) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '2500ms'")
                cur.execute(
                    """INSERT INTO sxbet_rate_cooldowns (bucket_key, cooldown_until)
                       VALUES (%s, clock_timestamp() + make_interval(secs => %s))
                       ON CONFLICT (bucket_key) DO UPDATE
                       SET cooldown_until = GREATEST(sxbet_rate_cooldowns.cooldown_until, EXCLUDED.cooldown_until),
                           updated_at = clock_timestamp()""",
                    (plan.cooldown_key, cooldown_seconds(retry_after)),
                )
            conn.commit()

    async def record_rate_limited(self, plan: RatePlan, retry_after: int | None) -> None:
        await asyncio.to_thread(self._record, plan, retry_after)
