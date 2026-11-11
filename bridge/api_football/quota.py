"""Atomic, durable per-key API budget and concurrency leases.

Reserved calls are always counted (including failed or cancelled calls).
PostgreSQL serializes reservations across workers/instances using a
transaction-scoped advisory lock; a process-local counter is insufficient.
This module has no database or network side effects when imported.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


class QuotaExceeded(RuntimeError):
    """Conservative request budget exhausted: do not send an HTTP request.

    `window` names the binding local limit: "minute" (rolling 60 s window, provider-zero
    marker or UTC minute bucket) with `retry_after_seconds` computed from the coordinator's
    own attempt times, or "day" (UTC calendar-day bucket) with `reset_at_utc`, the next UTC
    midnight. None when a caller raised it without that knowledge (fake coordinators)."""

    def __init__(self, message: str = "", *, window: str | None = None,
                 retry_after_seconds: int | None = None, reset_at_utc: str | None = None):
        if window not in (None, "minute", "day"):
            raise ValueError("unsupported quota window")
        self.window = window
        self.retry_after_seconds = retry_after_seconds
        self.reset_at_utc = reset_at_utc
        super().__init__(message)


class QuotaConcurrencyBusy(RuntimeError):
    """Shared active-lease concurrency exhausted."""


@dataclass(frozen=True)
class QuotaLimits:
    per_minute: int
    per_day: int
    max_concurrency: int

    def __post_init__(self):
        if not (1 <= self.per_minute <= 10000 and
                1 <= self.per_day <= 1_000_000 and
                1 <= self.max_concurrency <= 32):
            raise ValueError("Quota limits out of bounds")


@dataclass(frozen=True)
class QuotaLease:
    lease_id: str
    key_hash: str


# Migration SQL mirror retained for schema-contract tests and restore-state
# fixtures only. Runtime must never execute these statements.
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS api_football_quota_counters (
         api_key_hash TEXT NOT NULL,
         window_kind TEXT NOT NULL,
         bucket BIGINT NOT NULL,
         used INTEGER NOT NULL,
         PRIMARY KEY (api_key_hash, window_kind, bucket)
       )""",
    """CREATE TABLE IF NOT EXISTS api_football_quota_leases (
         lease_id TEXT PRIMARY KEY,
         api_key_hash TEXT NOT NULL,
         expires_at TIMESTAMPTZ NOT NULL
       )""",
    """CREATE INDEX IF NOT EXISTS idx_api_football_lease_expires
       ON api_football_quota_leases(api_key_hash, expires_at)""",
    """CREATE TABLE IF NOT EXISTS api_football_quota_attempts (
         attempt_id TEXT PRIMARY KEY,
         api_key_hash TEXT NOT NULL,
         created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
       )""",
    """CREATE INDEX IF NOT EXISTS idx_api_football_attempt_recent
       ON api_football_quota_attempts(api_key_hash, created_at)""",
)

_REQUIRED_TABLES = (
    "api_football_quota_counters",
    "api_football_quota_leases",
    "api_football_quota_attempts",
)


def _default_connect():
    from app.db import connect
    return connect()


def _key_hash(secret: str) -> str:
    if not isinstance(secret, str) or not secret:
        raise ValueError("API key unavailable")
    # SHA-256 fingerprint, NEVER the plaintext provider key or an auth token.
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _advisory_lock_key(key_hash: str) -> int:
    # Only deliberate lock collisions are possible; they serialize rather
    # than permit unsafe extra requests.
    return int(key_hash[:16], 16) & ((1 << 63) - 1)


_SCHEMA_INIT_LOCK_ID = 0x4150464F4F544241

# A provider-reported zero must block EVERY consumer of the key, whatever its own
# configured limits (runtime 80/day, a 4-request smoke workflow, ...). The counters are
# therefore raised to the largest limit any configuration may hold (config.py bounds),
# not to the caller's own limit, and a rolling provider-zero marker blocks the next 60 s
# for every per-minute limit.
_EXHAUSTED_COUNT = {"minute": 10_000, "day": 1_000_000}
_PROVIDER_ZERO_PREFIX = "provider-zero-"
_PROVIDER_ZERO_RECENT = (
    "SELECT 1 FROM api_football_quota_attempts WHERE api_key_hash=%s "
    "AND attempt_id LIKE 'provider-zero-%%' "
    "AND created_at > clock_timestamp()-INTERVAL '60 seconds' LIMIT 1"
)


# Seconds until the rolling window frees one slot: the (n - limit + 1)-th oldest attempt in
# the window leaves it 60 s after its creation. Read from the same table that enforces it.
_ROLLING_RETRY_AFTER = (
    "SELECT GREATEST(1, CEIL(EXTRACT(EPOCH FROM (created_at + INTERVAL '60 seconds' - clock_timestamp()))))::INT AS s "
    "FROM api_football_quota_attempts WHERE api_key_hash=%s "
    "AND created_at > clock_timestamp()-INTERVAL '60 seconds' "
    "ORDER BY created_at ASC OFFSET %s LIMIT 1"
)
_PROVIDER_ZERO_RETRY_AFTER = (
    "SELECT GREATEST(1, CEIL(EXTRACT(EPOCH FROM (max(created_at) + INTERVAL '60 seconds' - clock_timestamp()))))::INT AS s "
    "FROM api_football_quota_attempts WHERE api_key_hash=%s "
    "AND attempt_id LIKE 'provider-zero-%%' "
    "AND created_at > clock_timestamp()-INTERVAL '60 seconds'"
)


def _value(row, key: str):
    return row[key] if isinstance(row, dict) else row[0]


def _day_exceeded(epoch: int) -> QuotaExceeded:
    """The day bucket is epoch // 86400 of the database clock: the UTC calendar day."""
    reset = (epoch // 86400 + 1) * 86400
    return QuotaExceeded("shared provider budget exhausted", window="day",
                         retry_after_seconds=max(1, reset - epoch),
                         reset_at_utc=datetime.fromtimestamp(reset, timezone.utc).isoformat())


def _minute_exceeded(message: str, seconds) -> QuotaExceeded:
    retry = int(seconds) if seconds is not None else 60
    return QuotaExceeded(message, window="minute", retry_after_seconds=min(60, max(1, retry)))


class PostgresQuota:
    """Multi-process per-key UTC rolling-bucket accounting on an existing DB.

    Per-minute quota enforces BOTH UTC bucket and rolling 60-second
    limits. Per-day quota is a UTC calendar bucket. Real-time provider
    limits must be verified before activating live traffic.
    """

    def __init__(self, limits: QuotaLimits, *, connect: Callable = _default_connect):
        self.limits = limits
        self._connect = connect
        self._schema_ready = False
        self._schema_guard = threading.Lock()

    def _ensure_schema(self) -> None:
        # Runtime never performs DDL. Bridge migrations provision these tables;
        # this check fails closed while preserving least privilege.
        if self._schema_ready:
            return
        with self._schema_guard:
            if self._schema_ready:
                return
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name FROM unnest(%s::text[]) AS name "
                        "WHERE to_regclass('public.' || name) IS NULL",
                        (list(_REQUIRED_TABLES),),
                    )
                    missing = [row["name"] if isinstance(row, dict) else row[0]
                               for row in cur.fetchall()]
                    if missing:
                        raise RuntimeError("APIFOOTBALL_QUOTA_SCHEMA_NOT_READY")
            self._schema_ready = True

    def _reserve(self, secret: str) -> QuotaLease:
        key = _key_hash(secret)
        self._ensure_schema()
        lease = secrets.token_urlsafe(24)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_advisory_lock_key(key),))
                cur.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::BIGINT AS epoch")
                row = cur.fetchone()
                epoch = int(row["epoch"] if isinstance(row, dict) else row[0])
                cur.execute(
                    "DELETE FROM api_football_quota_leases "
                    "WHERE api_key_hash=%s AND expires_at <= clock_timestamp()",
                    (key,),
                )
                cur.execute(
                    "SELECT count(*) AS active FROM api_football_quota_leases "
                    "WHERE api_key_hash=%s",
                    (key,),
                )
                count = cur.fetchone()
                active = int(count["active"] if isinstance(count, dict) else count[0])
                if active >= self.limits.max_concurrency:
                    raise QuotaConcurrencyBusy("shared concurrency budget exhausted")

                # The UTC day is the binding limit when it is spent: say so before the minute
                # checks, or a client would retry every minute until midnight. Read-only: the
                # atomic conditional increment below still enforces it.
                cur.execute(
                    "SELECT used FROM api_football_quota_counters "
                    "WHERE api_key_hash=%s AND window_kind='day' AND bucket=%s",
                    (key, epoch // 86400),
                )
                day_row = cur.fetchone()
                if day_row is not None and int(_value(day_row, "used")) >= self.limits.per_day:
                    raise _day_exceeded(epoch)

                # Protect the provider against double bursts at UTC minute
                # boundaries with an exact rolling 60-second history.
                # The same per-key lock serializes ALL worker reservations.
                cur.execute(
                    "DELETE FROM api_football_quota_attempts "
                    "WHERE api_key_hash=%s "
                    "AND created_at <= clock_timestamp()-INTERVAL '60 seconds'",
                    (key,),
                )
                cur.execute(
                    "SELECT count(*) AS n FROM api_football_quota_attempts "
                    "WHERE api_key_hash=%s "
                    "AND created_at > clock_timestamp()-INTERVAL '60 seconds'",
                    (key,),
                )
                rolling = cur.fetchone()
                recent_count = int(rolling["n"] if isinstance(rolling, dict) else rolling[0])
                if recent_count >= self.limits.per_minute:
                    cur.execute(_ROLLING_RETRY_AFTER, (key, recent_count - self.limits.per_minute))
                    row = cur.fetchone()
                    raise _minute_exceeded("rolling provider request budget exhausted",
                                           _value(row, "s") if row is not None else None)
                cur.execute(_PROVIDER_ZERO_RECENT, (key,))
                if cur.fetchone() is not None:
                    cur.execute(_PROVIDER_ZERO_RETRY_AFTER, (key,))
                    row = cur.fetchone()
                    raise _minute_exceeded("provider reported a zero per-minute budget",
                                           _value(row, "s") if row is not None else None)

                for kind, bucket, maximum in (
                    ("minute", epoch // 60, self.limits.per_minute),
                    ("day", epoch // 86400, self.limits.per_day),
                ):
                    cur.execute(
                        """INSERT INTO api_football_quota_counters
                           (api_key_hash, window_kind, bucket, used)
                           VALUES (%s, %s, %s, 1)
                           ON CONFLICT (api_key_hash, window_kind, bucket)
                           DO UPDATE SET used=api_football_quota_counters.used+1
                           WHERE api_football_quota_counters.used < %s
                           RETURNING used""",
                        (key, kind, bucket, maximum),
                    )
                    if cur.fetchone() is None:
                        # Context manager rolls back BOTH buckets and lease.
                        if kind == "day":
                            raise _day_exceeded(epoch)
                        # UTC minute bucket (e.g. raised by a provider-reported zero): it
                        # turns over at the next minute boundary.
                        raise _minute_exceeded("shared provider budget exhausted", 60 - epoch % 60)
                cur.execute(
                    """INSERT INTO api_football_quota_attempts (attempt_id, api_key_hash)
                       VALUES (%s, %s)""",
                    (lease, key),
                )
                cur.execute(
                    """INSERT INTO api_football_quota_leases
                       (lease_id, api_key_hash, expires_at)
                       VALUES (%s, %s, clock_timestamp() + INTERVAL '180 seconds')""",
                    (lease, key),
                )
            conn.commit()
        return QuotaLease(lease_id=lease, key_hash=key)

    async def prepare(self) -> None:
        """One-time schema readiness, run outside the per-request lease budget."""
        if self._schema_ready:
            return
        await asyncio.to_thread(self._ensure_schema)

    async def acquire(self, secret: str) -> QuotaLease:
        # Database operations never block the Starlette/MCP event loop.
        return await asyncio.to_thread(self._reserve, secret)

    def _release(self, lease: QuotaLease) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM api_football_quota_leases WHERE lease_id=%s AND api_key_hash=%s",
                    (lease.lease_id, lease.key_hash),
                )
            conn.commit()

    async def release(self, lease: QuotaLease) -> None:
        # Shield release on cancellation in the orchestrator; leaks are
        # bounded by the 180-second crash-recovery lease.
        await asyncio.to_thread(self._release, lease)

    def _snapshot(self, secret: str) -> dict[str, int]:
        key = _key_hash(secret)
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::BIGINT AS epoch")
                row = cur.fetchone()
                epoch = int(row["epoch"] if isinstance(row, dict) else row[0])
                used = {}
                for kind, bucket in (("minute", epoch // 60), ("day", epoch // 86400)):
                    cur.execute(
                        "SELECT used FROM api_football_quota_counters "
                        "WHERE api_key_hash=%s AND window_kind=%s AND bucket=%s",
                        (key, kind, bucket),
                    )
                    found = cur.fetchone()
                    used[kind] = int((found["used"] if isinstance(found, dict) else found[0]) if found else 0)
                cur.execute(
                    "SELECT count(*) AS n FROM api_football_quota_leases "
                    "WHERE api_key_hash=%s AND expires_at>clock_timestamp()",
                    (key,),
                )
                found = cur.fetchone()
                active = int(found["n"] if isinstance(found, dict) else found[0])
                cur.execute(
                    "SELECT count(*) AS n FROM api_football_quota_attempts "
                    "WHERE api_key_hash=%s "
                    "AND created_at > clock_timestamp()-INTERVAL '60 seconds'",
                    (key,),
                )
                row = cur.fetchone()
                rolling_count = int(row["n"] if isinstance(row, dict) else row[0])
                cur.execute(_PROVIDER_ZERO_RECENT, (key,))
                if cur.fetchone() is not None:
                    rolling_count = self.limits.per_minute
        return {
            # Report the effective budget, not just the UTC calendar bucket.
            # Otherwise the snapshot can claim capacity while acquire()
            # correctly rejects the same request in the rolling window.
            "remaining_minute": max(
                0, min(self.limits.per_minute - used["minute"],
                       self.limits.per_minute - rolling_count)
            ),
            "remaining_day": max(0, self.limits.per_day - used["day"]),
            "active_leases": active,
        }

    async def snapshot(self, secret: str) -> dict[str, int]:
        return await asyncio.to_thread(self._snapshot, secret)


    def _mark_exhausted(self, secret: str, kind: str) -> None:
        """Honor a provider-reported zero budget across all bridge workers."""
        if kind not in ("minute", "day"):
            raise ValueError("unsupported budget window")
        key = _key_hash(secret)
        self._ensure_schema()
        max_count = _EXHAUSTED_COUNT[kind]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_advisory_lock_key(key),))
                cur.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::BIGINT AS epoch")
                row = cur.fetchone()
                epoch = int(row["epoch"] if isinstance(row, dict) else row[0])
                bucket = epoch // (60 if kind == "minute" else 86400)
                cur.execute(
                    """INSERT INTO api_football_quota_counters
                       (api_key_hash, window_kind, bucket, used)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (api_key_hash, window_kind, bucket)
                       DO UPDATE SET used=GREATEST(api_football_quota_counters.used, EXCLUDED.used)""",
                    (key, kind, bucket, max_count),
                )
                if kind == "minute":
                    # Provider-reported zero applies across UTC minute boundaries.
                    # Consume rolling slots so independent workers remain blocked.
                    cur.execute(
                        """INSERT INTO api_football_quota_attempts (attempt_id, api_key_hash)
                           SELECT %s || '-' || generate_series(1, %s)::text, %s
                           ON CONFLICT (attempt_id) DO NOTHING""",
                        (_PROVIDER_ZERO_PREFIX + secrets.token_urlsafe(16), self.limits.per_minute, key),
                    )
            conn.commit()

    async def mark_exhausted(self, secret: str, kind: str) -> None:
        await asyncio.to_thread(self._mark_exhausted, secret, kind)
