"""PR-02 shared quota integration: dedicated CI PostgreSQL only, no API traffic."""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets

import pytest

from bridge.api_football.quota import (
    PostgresQuota, QuotaLimits, QuotaExceeded, QuotaConcurrencyBusy,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires dedicated TEST_DATABASE_URL; no production credentials",
)


def connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(os.environ["TEST_DATABASE_URL"], row_factory=dict_row)


def identity():
    return "FAKE_TEST_KEY_" + secrets.token_hex(18)


def cleanup(key):
    digest = hashlib.sha256(key.encode()).hexdigest()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM api_football_quota_attempts WHERE api_key_hash=%s", (digest,))
        cur.execute("DELETE FROM api_football_quota_leases WHERE api_key_hash=%s", (digest,))
        cur.execute("DELETE FROM api_football_quota_counters WHERE api_key_hash=%s", (digest,))
        conn.commit()


def test_atomic_daily_and_minute_budgets_roll_back_on_failure():
    key = identity()
    quota = PostgresQuota(QuotaLimits(per_minute=3, per_day=1, max_concurrency=3), connect=connect)

    async def go():
        try:
            lease = await quota.acquire(key)
            await quota.release(lease)
            before = await quota.snapshot(key)
            assert before["remaining_day"] == 0
            assert before["remaining_minute"] == 2
            with pytest.raises(QuotaExceeded):
                await quota.acquire(key)
            after = await quota.snapshot(key)
            assert after["remaining_day"] == 0
            # Rejected day quota cannot consume a phantom minute request.
            assert after["remaining_minute"] == before["remaining_minute"]
            assert after["active_leases"] == 0
        finally:
            cleanup(key)
    asyncio.run(go())


def test_separate_process_equivalent_coordinators_do_not_over_admit():
    key = identity()
    q1 = PostgresQuota(QuotaLimits(1, 1, 2), connect=connect)
    q2 = PostgresQuota(QuotaLimits(1, 1, 2), connect=connect)

    async def go():
        try:
            results = await asyncio.gather(
                q1.acquire(key), q2.acquire(key), return_exceptions=True,
            )
            accepted = [r for r in results if not isinstance(r, BaseException)]
            rejected = [r for r in results if isinstance(r, BaseException)]
            assert len(accepted) == 1
            assert len(rejected) == 1
            assert isinstance(rejected[0], QuotaExceeded)
            assert (await q2.snapshot(key))["remaining_day"] == 0
            await q1.release(accepted[0])
        finally:
            cleanup(key)
    asyncio.run(go())


def test_concurrency_shared_across_independent_coordinator_instances():
    key = identity()
    q1 = PostgresQuota(QuotaLimits(4, 4, 1), connect=connect)
    q2 = PostgresQuota(QuotaLimits(4, 4, 1), connect=connect)

    async def go():
        try:
            first = await q1.acquire(key)
            with pytest.raises(QuotaConcurrencyBusy):
                await q2.acquire(key)
            snap = await q2.snapshot(key)
            assert snap["active_leases"] == 1
            assert snap["remaining_day"] == 3
            await q1.release(first)
            second = await q2.acquire(key)
            assert (await q1.snapshot(key))["active_leases"] == 1
            await q2.release(second)
            assert (await q1.snapshot(key))["active_leases"] == 0
        finally:
            cleanup(key)
    asyncio.run(go())


def test_provider_zero_header_marked_shared_and_persistent():
    key = identity()
    q1 = PostgresQuota(QuotaLimits(8, 80, 2), connect=connect)
    q2 = PostgresQuota(QuotaLimits(8, 80, 2), connect=connect)

    async def go():
        try:
            lease = await q1.acquire(key)
            await q1.release(lease)
            await q1.mark_exhausted(key, "minute")
            assert (await q2.snapshot(key))["remaining_minute"] == 0
            with pytest.raises(QuotaExceeded):
                await q2.acquire(key)
            await q2.mark_exhausted(key, "day")
            assert (await q1.snapshot(key))["remaining_day"] == 0
            with pytest.raises(ValueError):
                await q1.mark_exhausted(key, "year")
        finally:
            cleanup(key)
    asyncio.run(go())


def test_expired_crash_lease_does_not_permanently_block_requests():
    key = identity()
    quota = PostgresQuota(QuotaLimits(6, 6, 1), connect=connect)

    async def go():
        try:
            lease = await quota.acquire(key)
            digest = hashlib.sha256(key.encode()).hexdigest()
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE api_football_quota_leases "
                    "SET expires_at=clock_timestamp()-INTERVAL '2 seconds' "
                    "WHERE api_key_hash=%s AND lease_id=%s",
                    (digest, lease.lease_id),
                )
                conn.commit()
            next_lease = await quota.acquire(key)
            assert next_lease.lease_id != lease.lease_id
            assert (await quota.snapshot(key))["active_leases"] == 1
            await quota.release(next_lease)
        finally:
            cleanup(key)
    asyncio.run(go())


def test_plaintext_key_is_never_written_to_budget_tables():
    key = identity()
    quota = PostgresQuota(QuotaLimits(8, 80, 2), connect=connect)

    async def go():
        try:
            lease = await quota.acquire(key)
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT api_key_hash FROM api_football_quota_counters "
                    "WHERE api_key_hash=%s",
                    (hashlib.sha256(key.encode()).hexdigest(),),
                )
                rows = cur.fetchall()
                assert len(rows) == 1
                assert rows[0]["api_key_hash"] != key
            await quota.release(lease)
        finally:
            cleanup(key)
    asyncio.run(go())


def test_rolling_minute_blocks_recent_attempt_even_without_fixed_bucket_debits():
    """A request made just before UTC minute turnover still consumes the
    provider's rolling 60-second budget in the new minute."""
    key = identity()
    quota = PostgresQuota(QuotaLimits(1, 4, 2), connect=connect)

    async def go():
        try:
            # Bootstrap schema without consuming an API budget unit.
            assert (await quota.snapshot(key))["remaining_minute"] == 1
            digest = hashlib.sha256(key.encode()).hexdigest()
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_football_quota_attempts "
                    "(attempt_id, api_key_hash, created_at) "
                    "VALUES (%s, %s, clock_timestamp()-INTERVAL '5 seconds')",
                    (secrets.token_urlsafe(20), digest),
                )
                conn.commit()
            # The fixed UTC bucket is empty but the rolling window isn't.
            # The quota report must reflect the same effective capacity
            # that the reservation check will enforce.
            assert (await quota.snapshot(key))["remaining_minute"] == 0
            with pytest.raises(QuotaExceeded, match="rolling"):
                await quota.acquire(key)
            assert (await quota.snapshot(key))["active_leases"] == 0
            assert (await quota.snapshot(key))["remaining_day"] == 4
        finally:
            cleanup(key)
    asyncio.run(go())


def test_stale_rolling_attempt_expires_and_budget_recovers():
    key = identity()
    quota = PostgresQuota(QuotaLimits(1, 4, 2), connect=connect)

    async def go():
        try:
            await quota.snapshot(key)
            digest = hashlib.sha256(key.encode()).hexdigest()
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_football_quota_attempts "
                    "(attempt_id, api_key_hash, created_at) "
                    "VALUES (%s, %s, clock_timestamp()-INTERVAL '70 seconds')",
                    (secrets.token_urlsafe(20), digest),
                )
                conn.commit()
            lease = await quota.acquire(key)
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM api_football_quota_attempts "
                    "WHERE api_key_hash=%s", (digest,),
                )
                assert cur.fetchone()["n"] == 1
            await quota.release(lease)
            with pytest.raises(QuotaExceeded):
                await quota.acquire(key)
            assert (await quota.snapshot(key))["remaining_day"] == 3
        finally:
            cleanup(key)
    asyncio.run(go())


def test_effective_minute_snapshot_matches_shared_rolling_budget():
    key = identity()
    quota = PostgresQuota(QuotaLimits(2, 5, 2), connect=connect)

    async def go():
        try:
            before = await quota.snapshot(key)
            assert before["remaining_minute"] == 2
            lease = await quota.acquire(key)
            await quota.release(lease)
            after = await quota.snapshot(key)
            assert after["remaining_minute"] == 1
            assert after["remaining_day"] == 4
            assert after["active_leases"] == 0
        finally:
            cleanup(key)
    asyncio.run(go())
