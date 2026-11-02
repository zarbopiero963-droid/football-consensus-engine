"""FCB-WP-003 on real PostgreSQL: the live scripts consume the SHARED durable budget.

The day budget is enforced by the coordinator tables (not by the script), every attempt
is counted, and no lease is left behind.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets

import httpx
import pytest

from bridge.api_football.quota import PostgresQuota, QuotaLimits
from scripts.api_football_live_smoke import SAMPLE, run_live

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires dedicated TEST_DATABASE_URL; no production credentials",
)


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(os.environ["TEST_DATABASE_URL"], row_factory=dict_row)


def test_live_smoke_is_bounded_by_the_shared_postgres_budget():
    key = "FAKE_TEST_KEY_" + secrets.token_hex(18)
    digest = hashlib.sha256(key.encode()).hexdigest()
    env = {"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "true",
           "APIFOOTBALL_API_KEY": key, "APIFOOTBALL_MAX_DAILY_REQUESTS": "4",
           "APIFOOTBALL_MAX_RPM": "8", "APIFOOTBALL_MAX_CONCURRENCY": "1"}
    sent = []

    def handler(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"response": [], "results": 0, "errors": {}})

    # Another worker already spent 2 of the 4 daily units for this key.
    first = PostgresQuota(QuotaLimits(8, 4, 1), connect=_connect)

    async def spend_two():
        for _ in range(2):
            await first.release(await first.acquire(key))
    asyncio.run(spend_two())

    coordinator = PostgresQuota(QuotaLimits(8, 4, 1), connect=_connect)
    result = asyncio.run(run_live(env, transport=httpx.MockTransport(handler), coordinator=coordinator))

    assert sent == list(SAMPLE[:2])  # only the remaining shared budget was used
    assert result["results"][-1] == {"path": SAMPLE[2], "outcome": "BLOCKED_COORDINATOR",
                                     "code": "QUOTA_EXCEEDED"}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT used FROM api_football_quota_counters "
                    "WHERE api_key_hash=%s AND window_kind='day'", (digest,))
        assert [r["used"] for r in cur.fetchall()] == [4]
        cur.execute("SELECT count(*) AS n FROM api_football_quota_leases WHERE api_key_hash=%s", (digest,))
        assert cur.fetchone()["n"] == 0


def test_provider_zero_is_durable_for_other_workers():
    key = "FAKE_TEST_KEY_" + secrets.token_hex(18)
    env = {"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "true",
           "APIFOOTBALL_API_KEY": key, "APIFOOTBALL_MAX_DAILY_REQUESTS": "50",
           "APIFOOTBALL_MAX_RPM": "8", "APIFOOTBALL_MAX_CONCURRENCY": "1"}

    def handler(request):
        return httpx.Response(200, json={"response": [], "results": 0, "errors": {}},
                              headers={"x-ratelimit-requests-remaining": "0"})

    asyncio.run(run_live(env, transport=httpx.MockTransport(handler),
                         coordinator=PostgresQuota(QuotaLimits(8, 50, 1), connect=_connect)))
    sent = []

    def second(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"response": [], "results": 0, "errors": {}})

    # A different process (fresh coordinator object) must now be refused before sending.
    result = asyncio.run(run_live(env, transport=httpx.MockTransport(second),
                                  coordinator=PostgresQuota(QuotaLimits(8, 50, 1), connect=_connect)))
    assert sent == []
    assert result["results"][0]["code"] == "QUOTA_EXCEEDED"
