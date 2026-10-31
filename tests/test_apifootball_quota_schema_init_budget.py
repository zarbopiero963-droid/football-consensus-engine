"""One-time coordinator schema init must not consume the per-request lease budget.

Render evidence (2026-10-31, process [59], instance rjhpd): the first
API-FOOTBALL call after process start returned QUOTA_COORDINATOR_UNAVAILABLE
at ~3 s; the next call succeeded. The first PostgresQuota operation per
process runs _ensure_schema (connect + DDL) and then _reserve (connect +
statements) inside the same 3 s wait_for.
"""
import asyncio
import logging
import threading
import time

import pytest

from bridge.api_football.config import Settings
from bridge.api_football.errors import ProviderUnavailable
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.transport import ProviderReply

KEY = "FAKE_TEST_ONLY_KEY"


class SchemaQuota:
    """Mimics PostgresQuota: lazy schema init then reservation, all in one thread."""

    def __init__(self, init_delay, reserve_delay, init_error=None):
        self.init_delay, self.reserve_delay, self.init_error = init_delay, reserve_delay, init_error
        self.schema_ready = False
        self.lock = threading.Lock()
        self.inits = 0
        self.active = {}
        self.n = 0

    def _ensure_schema(self):
        with self.lock:
            if self.schema_ready:
                return
            self.inits += 1
            time.sleep(self.init_delay)
            if self.init_error:
                raise self.init_error
            self.schema_ready = True

    async def prepare(self):
        if not self.schema_ready:
            await asyncio.to_thread(self._ensure_schema)

    def _reserve(self):
        self._ensure_schema()
        time.sleep(self.reserve_delay)
        with self.lock:
            self.n += 1
            lease = QuotaLease("lease-%d" % self.n, "hash")
            self.active[lease.lease_id] = lease
            return lease

    async def acquire(self, secret):
        return await asyncio.to_thread(self._reserve)

    async def release(self, lease):
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


def _client(quota, lease_timeout=0.3, prepare_timeout=2.0):
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True, max_concurrency=1,
                        cache_enabled=False, circuit_breaker_enabled=False)
    client = ResilientFootballClient(settings, Transport(), quota=quota, retry=RetryPolicy(max_attempts=1))
    client._LEASE_TIMEOUT_SECONDS = lease_timeout
    client._PREPARE_TIMEOUT_SECONDS = prepare_timeout
    return client


def test_first_call_after_process_start_succeeds_when_schema_init_is_slow():
    async def go():
        # init 0.25 s + reserve 0.15 s = 0.40 s > 0.30 s lease budget; each alone fits.
        quota = SchemaQuota(init_delay=0.25, reserve_delay=0.15)
        client = _client(quota)
        out = await client.get("/status", {"n": 1})
        assert out["http_status"] == 200
        assert quota.inits == 1 and quota.active == {}
        out = await client.get("/status", {"n": 2})
        assert out["http_status"] == 200
        assert quota.inits == 1  # init runs once per process
    asyncio.run(go())


def test_slow_reservation_still_bounded_by_lease_timeout():
    async def go():
        quota = SchemaQuota(init_delay=0.0, reserve_delay=0.6)
        client = _client(quota)
        with pytest.raises(ProviderUnavailable) as exc:
            await client.get("/status", {"n": 1})
        assert exc.value.code == "QUOTA_COORDINATOR_UNAVAILABLE"
        assert client.transport.calls == 0
    asyncio.run(go())


def test_schema_init_beyond_its_own_bound_fails_closed_without_provider_call(caplog):
    async def go():
        quota = SchemaQuota(init_delay=0.5, reserve_delay=0.0)
        client = _client(quota, prepare_timeout=0.2)
        with pytest.raises(ProviderUnavailable) as exc:
            await client.get("/status", {"n": 1})
        assert exc.value.code == "QUOTA_COORDINATOR_UNAVAILABLE"
        assert client.transport.calls == 0
        await asyncio.sleep(0.5)          # shielded init keeps running and completes
        assert quota.schema_ready
        out = await client.get("/status", {"n": 2})
        assert out["http_status"] == 200
    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert "stage=prepare reason=TIMEOUT" in caplog.text


def test_schema_init_error_logs_class_name_only(caplog):
    async def go():
        quota = SchemaQuota(init_delay=0.0, reserve_delay=0.0,
                            init_error=RuntimeError("SECRET_DSN postgresql://u:p@h/db"))
        client = _client(quota)
        with pytest.raises(ProviderUnavailable) as exc:
            await client.get("/status", {"n": 1})
        assert exc.value.code == "QUOTA_COORDINATOR_UNAVAILABLE"
        assert "SECRET_DSN" not in str(exc.value)
    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert "stage=prepare reason=ERROR error_type=RuntimeError" in caplog.text
    assert "SECRET_DSN" not in caplog.text and "u:p@" not in caplog.text


def test_reserve_timeout_is_logged_with_stage_and_elapsed(caplog):
    async def go():
        quota = SchemaQuota(init_delay=0.0, reserve_delay=0.6)
        client = _client(quota)
        with pytest.raises(ProviderUnavailable):
            await client.get("/status", {"n": 1})
        await asyncio.sleep(0.6)
    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert "stage=reserve reason=TIMEOUT" in caplog.text and "elapsed_ms=" in caplog.text


def test_quota_without_prepare_keeps_previous_behaviour():
    class NoPrepare(SchemaQuota):
        prepare = None
    async def go():
        quota = NoPrepare(init_delay=0.0, reserve_delay=0.0)
        out = await _client(quota).get("/status", {"n": 1})
        assert out["http_status"] == 200
    asyncio.run(go())
