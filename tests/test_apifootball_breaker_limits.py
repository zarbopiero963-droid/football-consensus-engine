"""Circuit breaker vs request/plan limitations and 429 Retry-After (owner decision). No network:
a fake transport behind the REAL resilience client, with a controllable breaker clock."""
from __future__ import annotations

import asyncio

import pytest

from bridge.api_football.circuit import CircuitBreaker
from bridge.api_football.config import Settings
from bridge.api_football.errors import (
    CircuitOpen, ProviderDataError, ProviderDeclaredErrors, ProviderHTTPError, ProviderUnavailable,
)
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy

KEY = "LOCAL_FAKE_KEY_BREAKER_ONLY"


class Quota:
    def __init__(self):
        self.marked = []

    async def acquire(self, secret):
        return QuotaLease("lease", "hash")

    async def release(self, lease):
        return None

    async def mark_exhausted(self, secret, kind):
        self.marked.append(kind)


class Transport:
    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    async def get(self, path, params=None):
        self.calls += 1
        raise self.errors.pop(0)


def _client(*errors, attempts=1):
    now = [0.0]
    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: now[0])
    transport = Transport(*errors)
    quota = Quota()
    client = ResilientFootballClient(Settings(enabled=True, api_key=KEY, live_authorized=True), transport,
                                     quota=quota, breaker=breaker,
                                     retry=RetryPolicy(max_attempts=attempts, base_delay=0.0, max_delay=0.0))
    return client, transport, breaker, now, quota


def _two_calls(client):
    async def go():
        for i in range(2):
            with pytest.raises(Exception) as exc:
                await client.get("/fixtures", {"last": i + 1})
            assert not isinstance(exc.value, CircuitOpen)
    asyncio.run(go())


# ---- request / plan limitations are valid answers, not faults ---------------------------------

@pytest.mark.parametrize("make", [
    lambda: ProviderDeclaredErrors(("plan",)),
    lambda: ProviderHTTPError(400),
    lambda: ProviderHTTPError(404),
    lambda: ProviderHTTPError(422),
])
def test_two_consecutive_limitations_leave_the_circuit_closed(make):
    client, transport, breaker, _, _ = _client(make(), make())
    _two_calls(client)
    assert not breaker.open and breaker._failures == 0 and transport.calls == 2


def test_a_limitation_does_not_reset_the_count_of_real_faults():
    client, transport, breaker, _, _ = _client(
        ProviderHTTPError(503), ProviderDeclaredErrors(("plan",)), ProviderHTTPError(503))
    async def go():
        for i in range(3):
            with pytest.raises((ProviderHTTPError, ProviderDataError)):
                await client.get("/fixtures", {"last": i + 1})
        with pytest.raises(CircuitOpen):
            await client.get("/fixtures", {"last": 9})
    asyncio.run(go())
    assert transport.calls == 3


def test_a_half_open_probe_answered_by_a_limitation_does_not_close_the_circuit():
    client, transport, breaker, now, _ = _client(
        ProviderHTTPError(503), ProviderHTTPError(503), ProviderDeclaredErrors(("plan",)))
    _two_calls(client)
    assert breaker.open
    now[0] = 31.0  # cooldown over: the next call is the single half-open probe

    async def go():
        with pytest.raises(ProviderDeclaredErrors):
            await client.get("/fixtures", {"last": 5})
    asyncio.run(go())
    assert breaker.open  # not proof of recovery: re-opened, as for an aborted probe


# ---- real faults still open it ------------------------------------------------------------------

@pytest.mark.parametrize("make", [
    lambda: ProviderHTTPError(500), lambda: ProviderHTTPError(503),
    lambda: ProviderUnavailable("PROVIDER_TIMEOUT"), lambda: ProviderUnavailable("PROVIDER_NETWORK_ERROR"),
    lambda: ProviderDataError("PROVIDER_INVALID_JSON"),
    lambda: ProviderDataError("PROVIDER_REPLY_CONTRACT_INVALID"),
    lambda: ProviderHTTPError(408),  # request timeout: transient, not a limitation
    lambda: ProviderHTTPError(401), lambda: ProviderHTTPError(403),  # AUTH: unchanged, still counts
    lambda: ProviderDeclaredErrors(("token",)), lambda: ProviderDeclaredErrors(("OTHER",)),
    lambda: ProviderDeclaredErrors(("plan", "token")),
])
def test_two_consecutive_faults_still_open_the_circuit(make):
    client, transport, breaker, _, _ = _client(make(), make())
    _two_calls(client)
    assert breaker.open
    with pytest.raises(CircuitOpen):
        asyncio.run(client.get("/fixtures", {"last": 9}))
    assert transport.calls == 2


# ---- 429 honours Retry-After ------------------------------------------------------------------

def test_429_with_retry_after_holds_the_circuit_for_that_long():
    client, transport, breaker, now, quota = _client(ProviderHTTPError(429, retry_after_seconds=120))

    async def go():
        with pytest.raises(ProviderHTTPError):
            await client.get("/fixtures", {"last": 1})
        now[0] = 119.0
        with pytest.raises(CircuitOpen):  # one 429 is enough: the provider said "not before 120 s"
            await client.get("/fixtures", {"last": 2})
    asyncio.run(go())
    assert transport.calls == 1 and quota.marked == ["minute"]
    now[0] = 120.0
    assert not breaker.open


def test_429_retry_after_is_bounded_and_never_shortens_an_open_circuit():
    client, _, breaker, now, _ = _client(ProviderHTTPError(429, retry_after_seconds=86_400))

    async def go():
        with pytest.raises(ProviderHTTPError):
            await client.get("/fixtures", {"last": 1})
    asyncio.run(go())
    assert breaker._open_until == CircuitBreaker.MAX_HOLD_SECONDS == 3600

    async def shorter():
        b = CircuitBreaker(threshold=1, cooldown=30, clock=lambda: 0.0)
        await b.before_request()
        await b.fail()
        await b.hold(5)
        return b._open_until
    assert asyncio.run(shorter()) == 30


@pytest.mark.parametrize("retry_after", [None, 0])
def test_429_without_retry_after_counts_as_today(retry_after):
    client, _, breaker, _, quota = _client(ProviderHTTPError(429, retry_after_seconds=retry_after),
                                           ProviderHTTPError(429, retry_after_seconds=retry_after))

    async def go():
        with pytest.raises(ProviderHTTPError):
            await client.get("/fixtures", {"last": 1})
        assert not breaker.open  # one failure, below the threshold
        with pytest.raises(ProviderHTTPError):
            await client.get("/fixtures", {"last": 2})
        assert breaker.open  # the second opens it, as before
    asyncio.run(go())
    assert quota.marked == ["minute", "minute"]


def test_breaker_disabled_ignores_retry_after():
    client, transport, breaker, _, _ = _client(ProviderHTTPError(429, retry_after_seconds=60), ProviderHTTPError(429))
    client.settings = Settings(enabled=True, api_key=KEY, live_authorized=True, circuit_breaker_enabled=False)

    async def go():
        for i in range(2):
            with pytest.raises(ProviderHTTPError):
                await client.get("/fixtures", {"last": i + 1})
    asyncio.run(go())
    assert not breaker.open and transport.calls == 2
