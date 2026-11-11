"""PR #185 review P3s: reserved declared-error keys survive the bound; a concurrent success()
never erases an active Retry-After hold; a shared single-flight request is counted once."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from bridge.api_football.circuit import CircuitBreaker
from bridge.api_football.config import Settings
from bridge.api_football.errors import ProviderHTTPError, declared_error_keys
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import register_tools
from bridge.api_football.transport import ProviderReply

KEY = "LOCAL_FAKE_KEY_P3_ONLY"
_IS_KEY = lambda k: k.isidentifier()


# ---- errors.py: reserved keys before the 16-key truncation ----------------------------------

@pytest.mark.parametrize("reserved", ["requests", "rateLimit", "plan", "token"])
def test_reserved_key_survives_many_other_keys(reserved):
    # 20 keys that all sort before the reserved one ("a..." < "plan" < "rateLimit" < "requests" < "token").
    errors = {f"a{i:02d}": "x" for i in range(20)} | {reserved: "x"}
    keys = declared_error_keys(errors, _IS_KEY)
    assert reserved in keys and len(keys) == 16 and list(keys) == sorted(keys)


def test_all_reserved_keys_survive_together():
    errors = {f"a{i:02d}": "x" for i in range(40)} | {"requests": 1, "rateLimit": 1, "plan": 1, "token": 1}
    keys = declared_error_keys(errors, _IS_KEY)
    assert {"requests", "rateLimit", "plan", "token"} <= set(keys) and len(keys) == 16


def test_small_error_sets_are_unchanged():
    assert declared_error_keys({"requests": "x", "date": "y"}, _IS_KEY) == ("date", "requests")
    assert declared_error_keys([{"token": "x"}, {"1bad": "y"}], _IS_KEY) == ("OTHER", "token")


# ---- circuit.py: success() vs an active hold ------------------------------------------------

def test_concurrent_success_does_not_erase_retry_after_hold():
    now = [100.0]
    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: now[0])

    async def go():
        await breaker.hold(120)       # a 429 from one request
        await breaker.success()       # another request, sent before the hold, succeeds
        assert breaker.open
        now[0] += 119
        assert breaker.open
        now[0] += 2
        assert not breaker.open       # the hold ends on time
        await breaker.before_request()
    asyncio.run(go())


def test_failure_opening_does_not_shorten_a_longer_hold():
    now = [0.0]
    breaker = CircuitBreaker(threshold=1, cooldown=30, clock=lambda: now[0])

    async def go():
        await breaker.hold(300)
        await breaker.fail()
        now[0] = 100
        assert breaker.open
    asyncio.run(go())


def test_success_without_hold_still_closes():
    now = [0.0]
    breaker = CircuitBreaker(threshold=1, cooldown=30, clock=lambda: now[0])

    async def go():
        await breaker.fail()
        assert breaker.open
        now[0] = 31
        await breaker.before_request()   # half-open probe
        await breaker.success()
        assert not breaker.open
        await breaker.hold(10)
        now[0] = 42
        await breaker.success()          # the hold has expired: success closes
        assert not breaker.open
    asyncio.run(go())


# ---- resilience/tools: one shared flight is counted once -------------------------------------

class _Quota:
    def __init__(self):
        self.calls = 0

    async def acquire(self, secret):
        self.calls += 1
        return QuotaLease(f"l{self.calls}", "h")

    async def release(self, lease):
        return None

    async def mark_exhausted(self, secret, kind):
        return None


class _SlowTransport:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def get(self, path, params=None):
        self.calls += 1
        await asyncio.sleep(0.05)
        if self.fail:
            raise ProviderHTTPError(503)
        body = {"get": "countries", "parameters": {}, "errors": [], "results": 0,
                "paging": {"current": 1, "total": 1}, "response": []}
        return ProviderReply(200, body, datetime.now(timezone.utc).isoformat(), 10, 100, 10)


class _Server:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


def _tools(transport):
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True, cache_enabled=False)
    client = ResilientFootballClient(settings, transport, quota=_Quota(),
                                     retry=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0))
    server = _Server()
    register_tools(server, settings, client=client)
    return server.tools


def test_shared_flight_success_is_counted_once():
    transport = _SlowTransport()
    tools = _tools(transport)

    async def go():
        return await asyncio.gather(*(tools["apifootball_countries"]() for _ in range(3)))
    results = asyncio.run(go())
    assert transport.calls == 1
    assert sum(r["network_calls_made"] for r in results) == 1
    assert all(r["status"] == "REAL_API_RESPONSE" for r in results)


def test_shared_flight_failure_is_counted_once_and_keeps_its_code():
    transport = _SlowTransport(fail=True)
    tools = _tools(transport)

    async def go():
        return await asyncio.gather(*(tools["apifootball_countries"]() for _ in range(3)))
    results = asyncio.run(go())
    assert transport.calls == 1
    assert sum(r["network_calls_made"] for r in results) == 1
    assert all(r["error"] == {"code": "PROVIDER_SERVER_ERROR", "http_status": 503} for r in results)
