"""FIX-08: the shared PostgreSQL coordinator says WHICH local limit refused a reservation.

Rolling 60 s window -> window "minute" with retry_after_seconds from the real attempt times;
UTC calendar-day bucket -> window "day" with the reset at the next UTC midnight. Limits,
defaults, attempt counting and the provider-zero marker are unchanged. Dedicated
TEST_DATABASE_URL only; no provider traffic.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import os
import secrets

import pytest

from bridge.api_football.quota import PostgresQuota, QuotaExceeded, QuotaLimits

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


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def cleanup(key):
    with connect() as conn, conn.cursor() as cur:
        for table in ("api_football_quota_attempts", "api_football_quota_leases", "api_football_quota_counters"):
            cur.execute(f"DELETE FROM {table} WHERE api_key_hash=%s", (digest(key),))
        conn.commit()


def backdated_attempts(key, *seconds_ago):
    with connect() as conn, conn.cursor() as cur:
        for ago in seconds_ago:
            cur.execute(
                "INSERT INTO api_football_quota_attempts (attempt_id, api_key_hash, created_at) "
                "VALUES (%s, %s, clock_timestamp() - make_interval(secs => %s))",
                (secrets.token_urlsafe(20), digest(key), ago))
        conn.commit()


def run(coro_factory, key):
    async def go():
        try:
            return await coro_factory()
        finally:
            cleanup(key)
    return asyncio.run(go())


def test_rolling_window_refusal_reports_minute_and_retry_after_from_the_oldest_slot():
    key = identity()
    quota = PostgresQuota(QuotaLimits(2, 100, 2), connect=connect)

    async def go():
        await quota.snapshot(key)
        # 3 attempts in the window with a limit of 2: a slot frees when the 2nd oldest leaves.
        backdated_attempts(key, 55, 40, 10)
        with pytest.raises(QuotaExceeded) as info:
            await quota.acquire(key)
        exc = info.value
        assert exc.window == "minute"
        assert 18 <= exc.retry_after_seconds <= 21
        assert exc.reset_at_utc is None
        # Nothing was consumed by the refusal.
        assert (await quota.snapshot(key))["remaining_day"] == 100
    run(go, key)


def test_single_slot_limit_retry_after_tracks_the_only_attempt():
    key = identity()
    quota = PostgresQuota(QuotaLimits(1, 100, 2), connect=connect)

    async def go():
        await quota.snapshot(key)
        backdated_attempts(key, 50)
        with pytest.raises(QuotaExceeded, match="rolling") as info:
            await quota.acquire(key)
        assert info.value.window == "minute" and 9 <= info.value.retry_after_seconds <= 11
    run(go, key)


def test_daily_bucket_refusal_reports_day_and_next_utc_midnight():
    key = identity()
    quota = PostgresQuota(QuotaLimits(60, 1, 2), connect=connect)

    async def go():
        lease = await quota.acquire(key)
        await quota.release(lease)
        before = datetime.now(timezone.utc)
        with pytest.raises(QuotaExceeded) as info:
            await quota.acquire(key)
        exc = info.value
        assert exc.window == "day"
        reset = datetime.fromisoformat(exc.reset_at_utc)
        assert reset.tzinfo is not None and reset.utcoffset() == timedelta(0)
        assert (reset.hour, reset.minute, reset.second, reset.microsecond) == (0, 0, 0, 0)
        midnight = datetime(before.year, before.month, before.day, tzinfo=timezone.utc) + timedelta(days=1)
        assert reset in (midnight, midnight + timedelta(days=1))  # a run across UTC midnight
        assert 1 <= exc.retry_after_seconds <= 86_400
        # The daily refusal is reported as daily even though the minute window has room.
        assert (await quota.snapshot(key))["remaining_minute"] == 59
    run(go, key)


def test_daily_exhaustion_wins_over_a_full_minute_window():
    key = identity()
    quota = PostgresQuota(QuotaLimits(1, 1, 2), connect=connect)

    async def go():
        lease = await quota.acquire(key)
        await quota.release(lease)
        with pytest.raises(QuotaExceeded) as info:
            await quota.acquire(key)
        # Waiting a minute would not help: the day is the binding limit.
        assert info.value.window == "day"
    run(go, key)


def test_provider_zero_marker_reports_minute_with_a_full_window_wait():
    key = identity()
    quota = PostgresQuota(QuotaLimits(60, 7000, 1), connect=connect)

    async def go():
        await quota.mark_exhausted(key, "minute")
        with pytest.raises(QuotaExceeded) as info:
            await quota.acquire(key)
        assert info.value.window == "minute" and 58 <= info.value.retry_after_seconds <= 60
    run(go, key)


def test_provider_zero_day_marker_reports_day():
    key = identity()
    quota = PostgresQuota(QuotaLimits(60, 7000, 1), connect=connect)

    async def go():
        await quota.mark_exhausted(key, "day")
        with pytest.raises(QuotaExceeded) as info:
            await quota.acquire(key)
        assert info.value.window == "day" and info.value.reset_at_utc.endswith("T00:00:00+00:00")
    run(go, key)


def test_concurrent_reservations_never_over_admit_and_all_refusals_are_typed():
    key = identity()
    coordinators = [PostgresQuota(QuotaLimits(3, 100, 32), connect=connect) for _ in range(4)]

    async def go():
        await coordinators[0].snapshot(key)
        results = await asyncio.gather(*(coordinators[i % 4].acquire(key) for i in range(12)),
                                       return_exceptions=True)
        leases = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, BaseException)]
        assert len(leases) == 3 and len(refused) == 9
        assert all(isinstance(r, QuotaExceeded) and r.window == "minute"
                   and 1 <= r.retry_after_seconds <= 60 for r in refused)
        for lease in leases:
            await coordinators[0].release(lease)
        # Real attempt counting: exactly the admitted reservations were charged.
        assert (await coordinators[0].snapshot(key))["remaining_day"] == 97
    run(go, key)


def test_limit_overflow_by_one_is_refused_and_the_limit_itself_is_admitted():
    key = identity()
    quota = PostgresQuota(QuotaLimits(60, 7000, 1), connect=connect)

    async def go():
        await quota.snapshot(key)
        backdated_attempts(key, *([30] * 59))
        lease = await quota.acquire(key)  # the 60th in the window: admitted
        await quota.release(lease)
        with pytest.raises(QuotaExceeded) as info:  # the 61st: refused
            await quota.acquire(key)
        assert info.value.window == "minute" and 28 <= info.value.retry_after_seconds <= 31
    run(go, key)
