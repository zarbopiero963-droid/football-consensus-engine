"""A timed-out quota reservation must not leave an orphan concurrency lease.

Reproduced on real PostgreSQL (see tests/integration/test_apifootball_quota_orphan_lease_postgres.py):
with max_concurrency=1, one coordinator call slower than the lease timeout
returned QUOTA_COORDINATOR_UNAVAILABLE while the worker thread still inserted
the lease, so every later call failed with LOCAL_CONCURRENCY_BUSY for 180 s.
"""
import asyncio
import threading
import time

import pytest

from bridge.api_football.config import Settings
from bridge.api_football.errors import ProviderUnavailable
from bridge.api_football.quota import QuotaConcurrencyBusy, QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.transport import ProviderReply

KEY = "FAKE_TEST_ONLY_KEY"


class SlowThreadQuota:
    """Mimics PostgresQuota: work runs in a thread and cannot be cancelled."""

    def __init__(self, max_concurrency=1):
        self.max = max_concurrency
        self.active = {}
        self.lock = threading.Lock()
        self.delay = 0.0
        self.fail_release = False
        self.n = 0

    def _reserve(self):
        time.sleep(self.delay)
        with self.lock:
            if len(self.active) >= self.max:
                raise QuotaConcurrencyBusy("busy")
            self.n += 1
            lease = QuotaLease("lease-%d" % self.n, "hash")
            self.active[lease.lease_id] = lease
            return lease

    async def acquire(self, secret):
        return await asyncio.to_thread(self._reserve)

    async def release(self, lease):
        if self.fail_release:
            raise RuntimeError("SECRET_DSN db down")
        with self.lock:
            self.active.pop(lease.lease_id, None)

    async def mark_exhausted(self, secret, kind):
        return None


class Transport:
    def __init__(self):
        self.calls = 0

    async def get(self, path, params=None):
        self.calls += 1
        return ProviderReply(200, {"response": [], "errors": []}, "2026-10-31T00:00:00+00:00", 2)


async def _until(predicate, deadline=5.0):
    end = time.monotonic() + deadline
    while not predicate():
        if time.monotonic() > end:
            return False
        await asyncio.sleep(0.02)
    return True


def _client(quota):
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True, max_concurrency=1,
                        cache_enabled=False, circuit_breaker_enabled=False)
    client = ResilientFootballClient(settings, Transport(), quota=quota,
                                     retry=RetryPolicy(max_attempts=1))
    client._LEASE_TIMEOUT_SECONDS = 0.05
    return client


def test_timed_out_reservation_releases_its_orphan_lease():
    async def go():
        quota = SlowThreadQuota()
        client = _client(quota)
        quota.delay = 0.2
        try:
            await client.get("/fixtures", {"id": 1})
            raise AssertionError("expected coordinator timeout")
        except ProviderUnavailable as exc:
            assert exc.code == "QUOTA_COORDINATOR_UNAVAILABLE"
        assert client.transport.calls == 0  # no HTTP request without a lease
        assert await _until(lambda: quota.n == 1 and quota.active == {})  # compensating release ran
        quota.delay = 0.0
        out = await client.get("/fixtures", {"id": 2})  # not LOCAL_CONCURRENCY_BUSY
        assert out["http_status"] == 200
        assert quota.active == {}
    asyncio.run(go())


def test_timeout_where_reservation_itself_fails_leaves_nothing_to_release():
    async def go():
        quota = SlowThreadQuota(max_concurrency=0)
        client = _client(quota)
        quota.delay = 0.2
        with pytest.raises(ProviderUnavailable) as exc:
            await client.get("/fixtures", {"id": 1})
        assert exc.value.code == "QUOTA_COORDINATOR_UNAVAILABLE"
        await asyncio.sleep(0.3)
        assert quota.active == {}
        assert not client._orphan_releases
    asyncio.run(go())


def test_failed_compensating_release_is_contained_and_secret_free(caplog):
    async def go():
        quota = SlowThreadQuota()
        quota.fail_release = True
        client = _client(quota)
        quota.delay = 0.2
        with pytest.raises(ProviderUnavailable):
            await client.get("/fixtures", {"id": 1})
        assert await _until(lambda: quota.n == 1 and not client._orphan_releases)
        # Release failed: the lease is bounded by the 180 s DB expiry, never raised to callers.
        assert len(quota.active) == 1
        assert not client._orphan_releases
    asyncio.run(go())
    assert "QUOTA_ORPHAN_RELEASE_FAILED" in caplog.text
    assert "SECRET_DSN" not in caplog.text and KEY not in caplog.text


def test_fast_path_unchanged_single_claim_single_release():
    async def go():
        quota = SlowThreadQuota()
        client = _client(quota)
        client._LEASE_TIMEOUT_SECONDS = 3.0
        out = await client.get("/fixtures", {"id": 3})
        assert out["http_status"] == 200
        assert quota.n == 1 and quota.active == {}
    asyncio.run(go())


def test_cancelled_caller_releases_lease_committed_after_cancellation():
    async def go():
        quota = SlowThreadQuota()
        client = _client(quota)
        client._LEASE_TIMEOUT_SECONDS = 5.0
        quota.delay = 0.2
        task = asyncio.ensure_future(client._lease())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _until(lambda: quota.n == 1 and quota.active == {})
    asyncio.run(go())
