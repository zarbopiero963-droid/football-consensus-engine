"""FIX-07 (G10): provider timestamp, acquisition time, response time, cache HIT/MISS, age and
the request subject. A server time is never labelled as a provider timestamp, and a cache
HIT keeps the original acquisition time. No network: httpx.MockTransport / fake transport
behind the REAL transport, resilience client and MCP tools."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from bridge.api_football.cache import BoundedCache
from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import register_tools
from bridge.api_football.transport import InjectedHTTPXTransport, ProviderReply, provider_http_date

KEY = "LOCAL_FAKE_KEY_TIMESTAMPS_ONLY"
BODY = {"get": "fixtures", "parameters": {"id": "7"}, "errors": [], "results": 1,
        "paging": {"current": 1, "total": 1}, "response": [{"fixture": {"id": 7, "timestamp": 1700000000}}]}


class Quota:
    async def acquire(self, secret):
        return QuotaLease("l", "h")

    async def release(self, lease):
        return None

    async def mark_exhausted(self, secret, kind):
        return None


class Server:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools(headers=None, *, clock=None, transport_calls=None):
    def handler(request):
        if transport_calls is not None:
            transport_calls.append(str(request.url))
        return httpx.Response(200, json=BODY, headers=headers or {})

    settings = Settings(enabled=True, api_key=KEY, live_authorized=True)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler))
    cache = BoundedCache(clock=clock) if clock else None
    client = ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=KEY), quota=Quota(),
                                     cache=cache, retry=RetryPolicy(base_delay=0.0, max_delay=0.0))
    server = Server()
    register_tools(server, settings, client=client)
    return server.tools


@pytest.mark.parametrize("raw,expected", [
    ("Tue, 10 Nov 2026 21:04:05 GMT", "2026-11-10T21:04:05+00:00"),
    ("Tue, 10 Nov 2026 21:04:05 +0100", None),     # not the IMF-fixdate form: not guessed
    ("Tuesday, 10-Nov-26 21:04:05 GMT", None),     # obsolete RFC 850 form
    ("Tue, 31 Nov 2026 21:04:05 GMT", None),       # impossible date
    ("", None), ("garbage", None),
])
def test_provider_http_date_is_strict(raw, expected):
    assert provider_http_date({"date": raw} if raw else {}) == expected


def test_provider_timestamp_comes_from_the_provider_date_header_only():
    tools = _tools({"date": "Tue, 10 Nov 2026 21:04:05 GMT"})
    r = asyncio.run(tools["apifootball_fixtures"](id=7))
    assert r["status"] == "REAL_API_RESPONSE"
    assert r["provider_timestamp_utc"] == "2026-11-10T21:04:05+00:00"
    assert r["provider_timestamp_source"] == "PROVIDER_HTTP_DATE_HEADER"
    assert r["time_sources"]["provider_timestamp_utc"] == "PROVIDER_HTTP_DATE_HEADER"
    assert r["cache_status"] == "MISS" and r["cache_age_seconds"] is None
    assert r["request_subject"] == {"id": 7}
    # The provider's data is untouched (its own UTC anchor included).
    assert r["data"]["response"][0]["fixture"]["timestamp"] == 1700000000


def test_without_date_header_the_provider_timestamp_stays_null_never_a_server_time():
    tools = _tools({})
    r = asyncio.run(tools["apifootball_fixtures"](id=7))
    assert r["provider_timestamp_utc"] is None and r["provider_timestamp_source"] is None
    assert r["time_sources"]["provider_timestamp_utc"] is None
    assert r["retrieved_at_utc"] is not None
    assert r["time_sources"]["retrieved_at_utc"] == "BRIDGE_SERVER_CLOCK_AT_ACQUISITION"
    assert r["time_sources"]["responded_at_utc"] == "BRIDGE_SERVER_CLOCK"


def test_cache_hit_keeps_the_original_acquisition_time_and_reports_its_age():
    now = [1000.0]
    calls = []
    tools = _tools({"date": "Tue, 10 Nov 2026 21:04:05 GMT"}, clock=lambda: now[0], transport_calls=calls)
    first = asyncio.run(tools["apifootball_fixtures"](id=7))
    now[0] += 9.5
    second = asyncio.run(tools["apifootball_fixtures"](id=7))
    assert len(calls) == 1
    assert second["cache_status"] == "HIT" and second["network_calls_made"] == 0
    assert second["retrieved_at_utc"] == first["retrieved_at_utc"]       # not overwritten (G10)
    assert second["provider_timestamp_utc"] == first["provider_timestamp_utc"]
    assert second["cache_age_seconds"] == 9.5
    assert second["time_sources"]["cache_age_seconds"] == "BRIDGE_CACHE_CLOCK"
    assert second["responded_at_utc"] >= first["responded_at_utc"]
    assert second["age_seconds"] >= 0


def test_age_is_response_minus_acquisition():
    tools = _tools({})
    r = asyncio.run(tools["apifootball_fixtures"](id=7))
    acquired = datetime.fromisoformat(r["retrieved_at_utc"])
    responded = datetime.fromisoformat(r["responded_at_utc"])
    assert responded >= acquired
    assert abs(r["age_seconds"] - (responded - acquired).total_seconds()) < 0.01


def test_nothing_acquired_means_no_acquisition_time():
    tools = _tools({})
    invalid = asyncio.run(tools["apifootball_fixtures"]())
    assert invalid["status"] == "INVALID_INPUT"
    assert invalid["retrieved_at_utc"] is None and invalid["age_seconds"] is None
    assert invalid["responded_at_utc"]


def test_gate_results_have_no_acquisition_time():
    server = Server()
    register_tools(server, Settings(enabled=True, api_key=""))
    r = asyncio.run(server.tools["apifootball_odds"](fixture=5, bet=1))
    assert r["status"] == "KEY_NOT_CONFIGURED"
    assert r["retrieved_at_utc"] is None and r["provider_timestamp_utc"] is None
    assert r["request_subject"] == {"fixture": 5, "bet": 1}


def test_a_reply_object_without_the_new_field_still_works():
    reply = ProviderReply(200, BODY, datetime.now(timezone.utc).isoformat(), 10)
    assert reply.provider_date_utc is None
