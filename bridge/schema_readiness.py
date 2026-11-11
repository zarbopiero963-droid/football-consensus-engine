"""FCB-WP-006 — schema readiness, reported by /healthz and logged at startup.

The runtime never applies migrations (see app/migrations.py); it only REPORTS whether the
database it talks to carries the bridge migration series it was built for, so a deploy on an
empty or drifted database is visible immediately instead of surfacing as 503s and
UndefinedTable errors.

States (fixed codes, never exception text, DSN or SQL):
  NOT_CONFIGURED   no DATABASE_URL
  UNKNOWN          not checked yet (first check runs in the background)
  DB_UNAVAILABLE   connection or query failed / timed out
  EMPTY            no schema_migrations table
  OUTDATED         some bridge versions are missing (database behind this checkout)
  AHEAD            versions unknown to this checkout are recorded
  DRIFT            a recorded name or checksum differs from the repository file
  MISSING_TABLES   tracking matches but a table the runtime needs is absent
  READY            every bridge version recorded with matching name and checksum,
                   and every runtime-required table present

/healthz never blocks on the database: it returns the cached result and, when the cache is
stale, schedules one background refresh bounded by CHECK_TIMEOUT_SECONDS.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable

from app.migrations import BRIDGE, load_migrations

CHECK_TIMEOUT_SECONDS = 3.0
CACHE_TTL_SECONDS = 60.0

# Tables the running code reads or writes on its hot paths (OAuth, browser leases, the two
# shared rate coordinators, Claude DCR). Names only; never data.
REQUIRED_TABLES = (
    "oauth_authorization_codes", "oauth_access_tokens", "oauth_refresh_tokens", "oauth_claude_dcr_clients",
    "browser_session_leases", "sxbet_rate_attempts", "sxbet_rate_cooldowns",
    "api_football_quota_counters", "api_football_quota_leases", "api_football_quota_attempts",
)

# Versions (and their tables) that a database may still lack while the feature that needs them
# is OFF: the persistent OAuth throttle (0009) ships disabled and its migration is a separate
# owner action. While MCP_OAUTH_THROTTLE_MODE is off they are reported in `deferred_versions`
# instead of turning READY into OUTDATED; in shadow, enforce or an invalid mode they are required.
DEFERRED_WHILE_THROTTLE_OFF = {9: ("oauth_throttle_buckets", "oauth_throttle_unlock_audit")}


def _deferrable() -> dict[int, tuple[str, ...]]:
    from bridge.oauth_persistent_throttle import mode
    return DEFERRED_WHILE_THROTTLE_OFF if mode() == "off" else {}


def _default_connect():
    import psycopg
    from psycopg.rows import dict_row
    from app.db import database_url
    # Bounded connect: a timed-out check must not leave a thread stuck for libpq's default.
    return psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=3)


def evaluate(connect: Callable = _default_connect) -> dict[str, Any]:
    """Synchronous, read-only check. Returns {"state", "missing_tables", "bridge_versions"}."""
    expected = {m.version: (m.name, m.checksum) for m in load_migrations(BRIDGE.directory)}
    deferrable = _deferrable()
    checked = tuple(REQUIRED_TABLES) + tuple(t for _v, ts in sorted(DEFERRED_WHILE_THROTTLE_OFF.items())
                                             for t in ts)
    result: dict[str, Any] = {"state": "DB_UNAVAILABLE", "missing_tables": None,
                              "bridge_versions": len(expected), "deferred_versions": []}
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '2500ms'")
                cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS ok")
                row = cur.fetchone()
                tracked = bool(row["ok"] if isinstance(row, dict) else row[0])
                if not tracked:
                    result["state"] = "EMPTY"
                    return result
                cur.execute("SELECT version, name, sha256 FROM schema_migrations")
                rows = cur.fetchall()
                recorded = {}
                for r in rows:
                    v, n, s = (r["version"], r["name"], r["sha256"]) if isinstance(r, dict) else r
                    recorded[int(v)] = (n, s)
                # A view or sequence with a required name does not count as the table.
                cur.execute("SELECT t FROM unnest(%s::text[]) AS t WHERE NOT EXISTS ("
                            "SELECT 1 FROM pg_class c WHERE c.oid = to_regclass(t) AND c.relkind IN ('r', 'p'))",
                            (list(checked),))
                missing = sorted((r["t"] if isinstance(r, dict) else r[0]) for r in cur.fetchall())
    except Exception:
        return result
    # A deferred version's tables are required as soon as the version is recorded.
    excused = {t for v, ts in deferrable.items() if v not in recorded for t in ts}
    missing = [t for t in missing if t not in excused]
    if any(v in expected and recorded[v] != expected[v] for v in recorded):
        state = "DRIFT"  # most serious first: a recorded migration differs from the repository
    elif set(recorded) - set(expected):
        state = "AHEAD"
    elif set(expected) - set(recorded) - set(deferrable):
        state = "OUTDATED"
    elif missing:
        state = "MISSING_TABLES"
    else:
        state = "READY"
    result.update(state=state, missing_tables=missing,
                  deferred_versions=sorted(set(deferrable) - set(recorded)))
    return result


class SchemaReadiness:
    """Cached, non-blocking readiness for /healthz."""

    def __init__(self, connect: Callable = _default_connect, *, clock: Callable[[], float] = time.monotonic):
        self._connect = connect
        self._clock = clock
        self._result: dict[str, Any] = {"state": "UNKNOWN", "missing_tables": None, "bridge_versions": None}
        self._checked_at: float | None = None
        self._task: asyncio.Task | None = None

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("DATABASE_URL", "").strip())

    async def refresh(self) -> dict[str, Any]:
        if not self.configured():
            self._result = {"state": "NOT_CONFIGURED", "missing_tables": None, "bridge_versions": None}
            self._checked_at = self._clock()
            return self._result
        try:
            self._result = await asyncio.wait_for(asyncio.to_thread(evaluate, self._connect),
                                                  timeout=CHECK_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # includes TimeoutError: fixed code only
            self._result = {"state": "DB_UNAVAILABLE", "missing_tables": None, "bridge_versions": None}
        self._checked_at = self._clock()
        return self._result

    def snapshot(self) -> dict[str, Any]:
        """Never awaits the database. Schedules a refresh when the cache is stale."""
        if not self.configured():
            return {"state": "NOT_CONFIGURED", "missing_tables": None, "bridge_versions": None}
        stale = self._checked_at is None or self._clock() - self._checked_at > CACHE_TTL_SECONDS
        if stale and (self._task is None or self._task.done()):
            try:
                self._task = asyncio.get_running_loop().create_task(self.refresh())
            except RuntimeError:
                pass
        return dict(self._result)
