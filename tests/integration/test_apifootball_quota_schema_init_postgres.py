"""Real PostgreSQL: first call in a fresh process must fit the lease budget."""
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


class Transport:
    async def get(self, path, params=None):
        return ProviderReply(200, {"response": [], "errors": []}, "2026-10-31T00:00:00+00:00", 2)


def test_fresh_process_first_call_with_slow_connections():
    import psycopg
    from psycopg.rows import dict_row

    def connect():
        time.sleep(0.6)  # per-connection latency, as observed on Render (~0.95 s incl. statements)
        return psycopg.connect(os.environ["TEST_DATABASE_URL"], row_factory=dict_row)

    key = "FAKE_TEST_KEY_" + secrets.token_hex(18)
    digest = hashlib.sha256(key.encode()).hexdigest()
    # Fresh PostgresQuota == fresh process: _schema_ready is False although tables may exist.
    quota = PostgresQuota(QuotaLimits(8, 80, 1), connect=connect)
    settings = Settings(enabled=True, api_key=key, live_authorized=True, max_concurrency=1,
                        cache_enabled=False, circuit_breaker_enabled=False)
    client = ResilientFootballClient(settings, Transport(), quota=quota, retry=RetryPolicy(max_attempts=1))
    client._LEASE_TIMEOUT_SECONDS = 1.0  # init (>=0.6) + reserve (>=0.6) > 1.0; each alone fits

    try:
        out = asyncio.run(client.get("/status", {"n": 1}))
        assert out["http_status"] == 200
    finally:
        with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn, conn.cursor() as cur:
            for table in ("api_football_quota_attempts", "api_football_quota_leases", "api_football_quota_counters"):
                cur.execute(f"DELETE FROM {table} WHERE api_key_hash=%s", (digest,))
            conn.commit()
