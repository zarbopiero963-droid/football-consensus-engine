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
from typing import Callable


class QuotaExceeded(RuntimeError):
    """Conservative request budget exhausted: do not send an HTTP request."""


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
        # SQL DDL and quota row locks must NEVER overlap. Running DDL inside
        # an active per-key advisory lock caused a real PostgreSQL deadlock in
        # multi-worker CI; separate the two lock domains transactionally.
        if self._schema_ready:
            return
        with self._schema_guard:
            if self._schema_ready:
                return
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_INIT_LOCK_ID,))
                    for statement in _SCHEMA:
                        cur.execute(statement)
                conn.commit()
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
                    raise QuotaExceeded("rolling provider request budget exhausted")

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
                        raise QuotaExceeded("shared provider budget exhausted")
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
        max_count = self.limits.per_minute if kind == "minute" else self.limits.per_day
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
            conn.commit()

    async def mark_exhausted(self, secret: str, kind: str) -> None:
        await asyncio.to_thread(self._mark_exhausted, secret, kind)
