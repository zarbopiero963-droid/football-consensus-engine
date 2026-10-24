"""PR-02 API-FOOTBALL resilience certification: ALL provider calls are mocks."""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
import json

import httpx
import pytest

from bridge.api_football.cache import BoundedCache, ttl_for_endpoint
from bridge.api_football.circuit import CircuitBreaker
from bridge.api_football.client import UnsupportedEndpoint
from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.errors import (
    CircuitOpen, ProviderUnavailable, ProviderHTTPError, ProviderDataError, RequestQueueFull,
)
from bridge.api_football.quota import QuotaExceeded, QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.transport import (
    InjectedHTTPXTransport, ProviderReply, validated_request,
)


KEY = "LOCAL_FAKE_APIFOOTBALL_SECRET_NEVER_SEND"


class FakeQuota:
    def __init__(self):
        self.claims = []
        self.releases = []
        self.marked = []
        self.error = None

    async def acquire(self, secret: str):
        if self.error:
            raise self.error
        assert secret == KEY
        lease = QuotaLease(f"lease-{len(self.claims) + 1}", "unit-test-key-hash")
        self.claims.append(lease)
        return lease

    async def release(self, lease):
        self.releases.append(lease.lease_id)

    async def mark_exhausted(self, secret, kind):
        assert secret == KEY
        self.marked.append(kind)


class FakeTransport:
    def __init__(self, items=()):
        self.items = list(items)
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if not self.items:
            raise AssertionError("No mock response configured")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item


def _reply(*, data=None, minute=None, day=None):
    return ProviderReply(
        status_code=200,
        data={"response": [] if data is None else data, "results": 0, "paging": {"current": 1, "total": 1}},
        received_at_utc="2026-10-24T00:00:00+00:00",
        bytes_received=64,
        quota_remaining_day=day,
        quota_remaining_minute=minute,
    )


def _settings(**changes):
    base = Settings(enabled=True, api_key=KEY, max_concurrency=2,
                    max_rpm=8, max_daily_requests=80, timeout_seconds=2.0)
    return replace(base, **changes)


def _make_client(items, *, quota=None, retry=None, settings=None, breaker=None):
    transport = items if isinstance(items, FakeTransport) else FakeTransport(items)
    budget = quota or FakeQuota()
    client = ResilientFootballClient(
        settings or _settings(), transport,
        quota=budget,
        retry=retry or RetryPolicy(max_attempts=1),
        breaker=breaker,
    )
    return client, transport, budget


def test_strict_allowlist_and_param_types_reject_url_injection():
    assert validated_request("/odds/live", {"fixture": 10, "bet": "4"}) == (
        "/odds/live", {"fixture": 10, "bet": "4"},
    )
    for path in ("/fixtures/../odds", "http://localhost/mcp",
                 "https://v3.football.api-sports.io/fixtures", "/fixtures?league=1"):
        with pytest.raises(UnsupportedEndpoint):
            validated_request(path, {})
    for params in (
        {"path": object()}, {"x": True}, {"x": 2**50}, {"x": "\x00"},
        {"../host": "localhost"}, {"x": "a" * 129},
        {"x": "\nX-Api-Sports-Key: stolen"}, {str(i): i for i in range(17)},
    ):
        with pytest.raises(ValueError):
            validated_request("/fixtures", params)


def test_http_transport_only_allows_provider_origin():
    fake = httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ValueError):
        InjectedHTTPXTransport(fake, api_key=KEY)
    asyncio.run(fake.aclose())


def test_http_transport_uses_header_never_query_and_preserves_null_zero_missing():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["x-apisports-key"] == KEY
        assert "x-apisports-key" not in request.url.query.decode().lower()
        return httpx.Response(200, json={
            "results": 1, "response": [{"corner": 0, "xg": None, "blank": ""}],
            "paging": {"current": 1, "total": 1},
        }, headers={"x-ratelimit-requests-remaining": "79", "x-ratelimit-remaining": "7"})

    async def go():
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler)) as http:
            transport = InjectedHTTPXTransport(http, api_key=KEY)
            result = await transport.get("/fixtures/statistics", {"fixture": 10})
            assert result.data["response"][0] == {"corner": 0, "xg": None, "blank": ""}
            assert "shot_x" not in result.data["response"][0]
            assert result.quota_remaining_day == 79
            assert result.quota_remaining_minute == 7
            assert result.status_code == 200
            assert result.bytes_received > 0
            assert KEY not in repr(result)
    asyncio.run(go())
    assert len(requests) == 1
    assert str(requests[0].url).startswith(OFFICIAL_BASE_URL + "/fixtures/statistics")


@pytest.mark.parametrize("code,expect", [
    (301, "PROVIDER_REDIRECT_REJECTED"),
    (401, "PROVIDER_HTTP_401"),
    (403, "PROVIDER_HTTP_403"),
    (429, "PROVIDER_HTTP_429"),
    (500, "PROVIDER_HTTP_500"),
    (502, "PROVIDER_HTTP_502"),
])
def test_transport_status_handling_never_follows_redirect(code, expect):
    visits = []

    def handler(request):
        visits.append(str(request.url))
        return httpx.Response(code, headers={"location": "http://127.0.0.1/steal"})

    async def go():
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler)) as http:
            with pytest.raises((ProviderHTTPError, ProviderUnavailable), match=expect):
                await InjectedHTTPXTransport(http, api_key=KEY).get("/odds")
    asyncio.run(go())
    assert len(visits) == 1
    assert all("127.0.0.1" not in s for s in visits)


@pytest.mark.parametrize("payload,code", [
    (b"NOT-JSON", "PROVIDER_INVALID_JSON"),
    (b"[]", "PROVIDER_JSON_NOT_OBJECT"),
    (b'{"results": 0}', "PROVIDER_RESPONSE_FIELD_MISSING"),
    (b'{"response":[],"errors":{"token":"not valid"}}', "PROVIDER_DECLARED_ERRORS"),
    (b'{"response":[],"paging":{"current":4,"total":2}}', "PROVIDER_INVALID_PAGINATION"),
    (b'{"response":[],"paging":{"current":0,"total":0}}', "PROVIDER_INVALID_PAGINATION"),
])
def test_transport_rejects_bad_provider_payloads_without_leaking_body(payload, code):
    async def go():
        async with httpx.AsyncClient(
            base_url=OFFICIAL_BASE_URL,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload)),
        ) as http:
            with pytest.raises(ProviderDataError, match=code) as exc:
                await InjectedHTTPXTransport(http, api_key=KEY).get("/fixtures")
            assert KEY not in str(exc.value)
            assert "not valid" not in str(exc.value)
    asyncio.run(go())


def test_transport_aborts_oversized_response_before_accepting_data():
    async def go():
        async with httpx.AsyncClient(
            base_url=OFFICIAL_BASE_URL,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 524289)),
        ) as http:
            with pytest.raises(ProviderDataError, match="PROVIDER_RESPONSE_TOO_LARGE"):
                await InjectedHTTPXTransport(http, api_key=KEY).get("/fixtures")
    asyncio.run(go())


def test_cache_expiry_lru_and_oversized_value():
    clock = [5.0]
    c = BoundedCache(max_entries=2, max_bytes=300, max_entry_bytes=150, clock=lambda: clock[0])
    assert c.put(("a",), {"v": 0, "null": None}, 2)
    x = c.get(("a",))
    assert x == {"v": 0, "null": None}
    x["v"] = 99
    assert c.get(("a",))["v"] == 0
    assert c.put(("b",), {"v": 1}, 30)
    assert c.put(("c",), {"v": 2}, 30)
    assert c.get(("a",)) is None
    assert c.get(("b",)) is not None
    assert not c.put(("too_big",), {"data": "x" * 300}, 30)
    clock[0] += 40
    assert c.get(("b",)) is None
    assert ttl_for_endpoint("/odds/live") <= ttl_for_endpoint("/odds")
    assert ttl_for_endpoint("/status") == 0


def test_singleflight_deduplicates_and_cache_reuses_without_budget():
    async def go():
        wait = asyncio.Event()
        async def release_response():
            await wait.wait()
            return _reply(data=[{"t": 0, "n": None}])
        client, transport, quota = _make_client([release_response], settings=_settings())
        tasks = [asyncio.create_task(client.get("/fixtures", {"league": 20})) for _ in range(8)]
        await asyncio.sleep(0)
        wait.set()
        results = await asyncio.gather(*tasks)
        assert len(transport.calls) == 1
        assert len(quota.claims) == len(quota.releases) == 1
        assert all(r["data"]["response"][0]["n"] is None for r in results)
        results[0]["data"]["response"][0]["t"] = 999
        result = await client.get("/fixtures", {"league": 20})
        assert result["cache_status"] == "HIT"
        assert result["data"]["response"][0]["t"] == 0
        assert len(transport.calls) == 1
    asyncio.run(go())


def test_retry_503_twice_then_success_has_three_distinct_quota_debits():
    async def go():
        client, transport, quota = _make_client([
            ProviderHTTPError(503), ProviderHTTPError(502), _reply()],
            retry=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
        )
        result = await client.get("/fixtures", {"league": 20})
        assert result["cache_status"] == "MISS"
        assert len(transport.calls) == 3
        assert len(quota.claims) == len(quota.releases) == 3
    asyncio.run(go())


@pytest.mark.parametrize("code", [401, 403, 429])
def test_non_retryable_status_never_storms(code):
    async def go():
        client, transport, quota = _make_client(
            [ProviderHTTPError(code), _reply()],
            retry=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
        )
        with pytest.raises(ProviderHTTPError) as e:
            await client.get("/odds/live", {"fixture": 55})
        assert e.value.status == code
        assert len(transport.calls) == len(quota.claims) == len(quota.releases) == 1
        assert quota.marked == (["minute"] if code == 429 else [])
    asyncio.run(go())


def test_shared_quota_backend_unavailable_fails_closed_without_exposing_exception():
    class BrokenQuota(FakeQuota):
        async def acquire(self, secret):
            raise RuntimeError(f"SECRET_DSN {secret}")

    async def go():
        transport = FakeTransport([_reply()])
        client, _, _ = _make_client(transport, quota=BrokenQuota())
        with pytest.raises(ProviderUnavailable, match="QUOTA_COORDINATOR_UNAVAILABLE") as e:
            await client.get("/fixtures")
        assert KEY not in str(e.value)
        assert not transport.calls
    asyncio.run(go())


def test_reported_zero_remaining_halts_shared_window_via_coordinator():
    async def go():
        client, transport, quota = _make_client([_reply(day=0, minute=0)])
        r = await client.get("/fixtures")
        assert r["quota_remaining_provider_day"] == 0
        assert quota.marked == ["day", "minute"]
    asyncio.run(go())


def test_circuit_opens_recovers_with_exactly_one_half_open_probe():
    async def go():
        now = [0.]
        b = CircuitBreaker(threshold=2, cooldown=5, clock=lambda: now[0])
        await b.before_request()
        await b.fail()
        await b.before_request()
        await b.fail()
        assert b.open
        with pytest.raises(CircuitOpen):
            await b.before_request()
        now[0] = 6.
        await b.before_request()
        with pytest.raises(CircuitOpen):
            await b.before_request()
        await b.success()
        assert not b.open
        await b.before_request()
        await b.abort()
    asyncio.run(go())


def test_client_circuit_blocks_after_repeated_provider_failures():
    async def go():
        client, transport, quota = _make_client([
            ProviderHTTPError(503), ProviderHTTPError(503), _reply()
        ])
        for i in range(2):
            with pytest.raises(ProviderHTTPError):
                await client.get("/fixtures", {"last": i + 1})
        with pytest.raises(CircuitOpen):
            await client.get("/fixtures", {"last": 3})
        assert len(transport.calls) == 2
        assert len(quota.claims) == 2
    asyncio.run(go())


def test_cancellation_of_one_waiter_does_not_abort_shared_request_or_leak_lease():
    async def go():
        event = asyncio.Event()
        async def waiting():
            await event.wait()
            return _reply()
        client, transport, quota = _make_client([waiting])
        a = asyncio.create_task(client.get("/fixtures", {"id": 1}))
        b = asyncio.create_task(client.get("/fixtures", {"id": 1}))
        await asyncio.sleep(0.05)
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        event.set()
        result = await b
        assert result["status"] == "MOCK_TRANSPORT_ONLY"
        assert len(transport.calls) == 1
        assert len(quota.claims) == len(quota.releases) == 1
    asyncio.run(go())


def test_disabled_adapter_cannot_construct_resilience_client():
    transport = FakeTransport([])
    with pytest.raises(ProviderUnavailable, match="PROVIDER_NOT_CONFIGURED"):
        ResilientFootballClient(Settings.from_env({"APIFOOTBALL_ENABLED": "false"}), transport)


def test_quota_headers_and_mock_data_never_become_synthetic_pit():
    async def go():
        client, _, _ = _make_client([_reply(data=[{"goals": 0, "xg": None}])])
        result = await client.get("/fixtures", {"id": 3})
        assert result["provider_timestamp_utc"] is None
        assert result["retrieved_at_utc"] == "2026-10-24T00:00:00+00:00"
        assert result["data"]["response"][0]["xg"] is None
        assert result["status"] == "MOCK_TRANSPORT_ONLY"
        assert "PIT" not in result
    asyncio.run(go())


def test_unique_inflight_task_count_is_bounded_before_allocation():
    """Do not create unbounded tasks for unique requests under overload."""
    async def go():
        gate = asyncio.Event()
        async def delayed():
            await gate.wait()
            return _reply()

        transport = FakeTransport([delayed] * 4)
        client, _, quota = _make_client(transport, settings=_settings(max_concurrency=1))
        tasks = [asyncio.create_task(client.get("/fixtures", {"id": n})) for n in range(4)]
        try:
            await asyncio.sleep(0.05)
            assert len(client._flights) == 4
            with pytest.raises(RequestQueueFull):
                await client.get("/fixtures", {"id": 99})
            assert len(client._flights) == 4
            # An identical caller may reuse a slot despite a full queue.
            duplicate = asyncio.create_task(client.get("/fixtures", {"id": 0}))
            gate.set()
            results = await asyncio.gather(*tasks, duplicate, return_exceptions=True)
            assert all(isinstance(item, dict) or isinstance(item, RequestQueueFull)
                       for item in results)
            assert not any(isinstance(item, AssertionError) for item in results)
        finally:
            gate.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(go())
