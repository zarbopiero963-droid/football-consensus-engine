"""PR-07 offline adversarial certification; no provider calls."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bridge.api_football.config import Settings
from bridge.api_football.errors import ProviderHTTPError, ProviderUnavailable
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient
from bridge.api_football.transport import InjectedHTTPXTransport, ProviderReply, validated_request
from bridge.api_football.wiring import build_client


class _Quota:
    def __init__(self):
        self.calls = 0
        self.released = 0
    async def acquire(self, secret):
        self.calls += 1
        return QuotaLease("lease", "hash")
    async def release(self, lease):
        self.released += 1
    async def mark_exhausted(self, secret, kind):
        return None


class _Transport:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = 0
    async def get(self, path, params=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply or ProviderReply(
            200, {"response": [{"ok": True}], "paging": {"current": 1, "total": 1}},
            "2026-01-01T00:00:00+00:00", 40,
        )


def _settings():
    return Settings(enabled=True, api_key="test-only", live_authorized=True,
                    max_rpm=20, max_daily_requests=100, max_concurrency=2)


def _run(transport):
    q = _Quota()
    client = ResilientFootballClient(_settings(), transport, quota=q)
    return asyncio.run(client.get("/countries", {"search": "ita"})), q


def test_live_gate_is_off_by_default_and_key_cannot_bypass():
    s = Settings(enabled=True, api_key="test-only")
    assert s.live_authorized is False
    assert s.real_requests_authorized is False
    assert build_client(s) is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504])
def test_provider_http_failures_are_bounded_and_sanitized(status):
    transport = _Transport(error=ProviderHTTPError(status))
    with pytest.raises(ProviderHTTPError):
        _run(transport)
    assert transport.calls <= 3


def test_timeout_and_network_failure_are_bounded():
    for error in (ProviderUnavailable("PROVIDER_TIMEOUT"), ProviderUnavailable("PROVIDER_NETWORK_ERROR")):
        transport = _Transport(error=error)
        with pytest.raises(ProviderUnavailable):
            _run(transport)
        assert transport.calls <= 3


def test_deduplication_and_quota_reservation_are_shared_per_client():
    async def scenario():
        q = _Quota()
        transport = _Transport()
        client = ResilientFootballClient(_settings(), transport, quota=q)
        first, second = await asyncio.gather(
            client.get("/countries", {"search": "ita"}),
            client.get("/countries", {"search": "ita"}),
        )
        return first, second, q, transport
    first, second, q, transport = asyncio.run(scenario())
    assert first == second
    assert transport.calls == 1
    assert q.calls == q.released == 1


def test_transport_rejects_redirect_and_invalid_payload():
    def handler(request):
        if request.url.path == "/countries":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/"})
        return httpx.Response(200, content=b"{broken")
    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://v3.football.api-sports.io",
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as http:
            transport = InjectedHTTPXTransport(http, api_key="test-only")
            with pytest.raises(ProviderUnavailable, match="PROVIDER_REDIRECT_REJECTED"):
                await transport.get("/countries", {"search": "ita"})
    async def malformed():
        async with httpx.AsyncClient(
            base_url="https://v3.football.api-sports.io",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"{broken")),
            follow_redirects=False,
        ) as bad_http:
            bad = InjectedHTTPXTransport(bad_http, api_key="test-only")
            with pytest.raises(Exception, match="PROVIDER_INVALID_JSON"):
                await bad.get("/countries", {"search": "ita"})
    async def all_cases():
        await scenario()
        await malformed()
    asyncio.run(all_cases())


def test_transport_rejects_unallowlisted_path_and_origin():
    with pytest.raises(Exception):
        validated_request("/not-an-endpoint", {})
    with pytest.raises(ValueError):
        InjectedHTTPXTransport(
            httpx.AsyncClient(base_url="https://evil.example"),
            api_key="test-only",
        )


def test_oversized_payload_is_rejected_before_json_use():
    def handler(request):
        return httpx.Response(200, content=json.dumps({"response": "x" * 600000}).encode())
    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://v3.football.api-sports.io",
            transport=httpx.MockTransport(handler),
        ) as http:
            transport = InjectedHTTPXTransport(http, api_key="test-only")
            with pytest.raises(Exception, match="PROVIDER_RESPONSE_TOO_LARGE"):
                await transport.get("/countries", {"search": "ita"})
    asyncio.run(scenario())
