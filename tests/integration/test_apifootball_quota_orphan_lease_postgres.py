"""Real PostgreSQL regression: coordinator timeout must not leak a concurrency lease."""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time

import pytest

from bridge.api_football.config import Settings
from bridge.api_football.quota import PostgresQuota, QuotaLimits
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.transport import ProviderReply

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires dedicated TEST_DATABASE_URL; no production credentials",
)


def _connect_factory(state):
    import psycopg
    from psycopg.rows import dict_row

    def connect():
        if state["slow"]:
            time.sleep(state["slow"])  # emulates serverless cold start / slow TLS connect
        return psycopg.connect(os.environ["TEST_DATABASE_URL"], row_factory=dict_row)
    return connect


class Transport:
    async def get(self, path, params=None):
        return ProviderReply(200, {"response": [], "errors": []}, "2026-10-31T00:00:00+00:00", 2)


def test_coordinator_timeout_does_not_block_following_calls():
    key = "FAKE_TEST_KEY_" + secrets.token_hex(18)
    digest = hashlib.sha256(key.encode()).hexdigest()
    state = {"slow": 0}
    connect = _connect_factory(state)
    quota = PostgresQuota(QuotaLimits(8, 80, 1), connect=connect)
    settings = Settings(enabled=True, api_key=key, live_authorized=True, max_concurrency=1,
                        cache_enabled=False, circuit_breaker_enabled=False)
    client = ResilientFootballClient(settings, Transport(), quota=quota, retry=RetryPolicy(max_attempts=1))
    client._LEASE_TIMEOUT_SECONDS = 0.5

    def active():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM api_football_quota_leases "
                        "WHERE api_key_hash=%s AND expires_at>clock_timestamp()", (digest,))
            return cur.fetchone()["n"]

    async def go():
        quota._ensure_schema()
        state["slow"] = 1.0
        with pytest.raises(Exception) as first:
            await client.get("/fixtures", {"id": 1})
        assert getattr(first.value, "code", None) == "QUOTA_COORDINATOR_UNAVAILABLE"
        state["slow"] = 0
        deadline = time.monotonic() + 15
        while active() != 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        assert active() == 0
        out = await client.get("/fixtures", {"id": 2})
        assert out["http_status"] == 200
        assert active() == 0

    try:
        asyncio.run(go())
    finally:
        with connect() as conn, conn.cursor() as cur:
            for table in ("api_football_quota_attempts", "api_football_quota_leases", "api_football_quota_counters"):
                cur.execute(f"DELETE FROM {table} WHERE api_key_hash=%s", (digest,))
            conn.commit()
