"""Fixed, distinct MCP error codes for API-FOOTBALL failures (G05, G06, G09, G11). No network:
the provider is httpx.MockTransport or a fake transport behind the REAL resilience client."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from bridge.api_football.client import UnsupportedEndpoint
from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.errors import (
    ProviderDataError, ProviderDeclaredErrors, ProviderHTTPError, ProviderUnavailable, RequestQueueFull,
    classify_declared_errors,
)
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import register_tools
from bridge.api_football.transport import InjectedHTTPXTransport, ProviderReply

KEY = "LOCAL_FAKE_KEY_ERROR_CODES_ONLY"
SECRET_VALUE = "SECRET-PROVIDER-TEXT https://dashboard.example.invalid/account?token=abc"
OFFLINE = "Offline PR-03 tool: never claim real coverage, live odds or PIT data."


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
    """Each get() is one provider request; items are replies or exceptions, in order."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0

    async def get(self, path, params=None):
        self.calls += 1
        item = self.items.pop(0) if self.items else ok()
        if isinstance(item, BaseException):
            raise item
        return item


def ok(data=None, paging=None, day=50, minute=5):
    body = {"get": "x", "parameters": {}, "errors": [], "results": 0,
            "paging": paging or {"current": 1, "total": 1}, "response": [] if data is None else data}
    return ProviderReply(200, body, datetime.now(timezone.utc).isoformat(), 10,
                         quota_remaining_day=day, quota_remaining_minute=minute)


class Server:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def tools_for(transport=None, *, client=None, quota=None):
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True)
    if client is None:
        client = ResilientFootballClient(settings, transport, quota=quota or Quota(),
                                         retry=RetryPolicy(base_delay=0.0, max_delay=0.0))
    server = Server()
    register_tools(server, settings, client=client)
    return server.tools, client


def call(tools, name, **args):
    return asyncio.run(tools[name](**args))


def _clean(result):
    text = json.dumps(result)
    assert KEY not in text and "SECRET-PROVIDER-TEXT" not in text and "example.invalid" not in text
    assert "http://" not in text.replace(OFFICIAL_BASE_URL, "") and "dashboard" not in text
    return result


# ---- G05: distinct fixed codes ----------------------------------------------------------------

@pytest.mark.parametrize("status,code,attempts", [
    (401, "PROVIDER_AUTH_FAILED", 1), (403, "PROVIDER_AUTH_FAILED", 1),
    (429, "PROVIDER_RATE_LIMITED", 1),
    (500, "PROVIDER_SERVER_ERROR", 3), (503, "PROVIDER_SERVER_ERROR", 3), (599, "PROVIDER_SERVER_ERROR", 1),
    (400, "PROVIDER_REQUEST_REJECTED", 1), (404, "PROVIDER_REQUEST_REJECTED", 1),
])
def test_provider_http_errors_get_distinct_fixed_codes(status, code, attempts):
    transport = Transport(*[ProviderHTTPError(status)] * 3)
    tools, _ = tools_for(transport)
    r = _clean(call(tools, "apifootball_countries"))
    assert r["status"] == "PROVIDER_ERROR"
    assert r["error"] == {"code": code, "http_status": status}
    assert r["http_status"] == status
    # G09: the real number of requests the resilience layer sent, retries included.
    assert r["network_calls_made"] == transport.calls == attempts
    assert OFFLINE not in r["warnings"]


def test_429_reports_a_numeric_retry_after_only():
    tools, _ = tools_for(Transport(ProviderHTTPError(429, retry_after_seconds=7)))
    r = call(tools, "apifootball_countries")
    assert r["error"] == {"code": "PROVIDER_RATE_LIMITED", "http_status": 429, "retry_after_seconds": 7}


@pytest.mark.parametrize("header,expect", [("7", 7), ("0", 0), ("86400", 86400), ("86401", None),
                                           ("Wed, 21 Oct 2015 07:28:00 GMT", None), ("-1", None),
                                           ("1.5", None), ("", None)])
def test_transport_parses_retry_after_as_seconds_only(header, expect):
    def handler(request):
        return httpx.Response(429, headers={"retry-after": header} if header else {}, text=SECRET_VALUE)

    async def go():
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(ProviderHTTPError) as exc:
                await InjectedHTTPXTransport(http, api_key=KEY).get("/countries")
        return exc.value
    err = asyncio.run(go())
    assert err.status == 429 and err.retry_after_seconds == expect and SECRET_VALUE not in str(err)


def test_retry_after_is_ignored_outside_429():
    def handler(request):
        return httpx.Response(503, headers={"retry-after": "9"})

    async def go():
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(ProviderHTTPError) as exc:
                await InjectedHTTPXTransport(http, api_key=KEY).get("/countries")
        return exc.value
    assert asyncio.run(go()).retry_after_seconds is None


def test_open_circuit_is_circuit_open_with_no_request():
    transport = Transport()
    tools, client = tools_for(transport)
    client.breaker._open_until = float("inf")
    r = call(tools, "apifootball_countries")
    assert r["error"] == {"code": "CIRCUIT_OPEN"} and r["network_calls_made"] == 0 == transport.calls


class _Raises:
    def __init__(self, exc):
        self.exc = exc

    async def get(self, path, params=None):
        raise self.exc


@pytest.mark.parametrize("exc,code", [
    (RequestQueueFull(), "QUEUE_TIMEOUT"),
    (ProviderUnavailable("PROVIDER_TIMEOUT"), "PROVIDER_TIMEOUT"),
    (ProviderUnavailable("PROVIDER_NO_REPLY", retryable=False), "PROVIDER_NO_REPLY"),
    (ProviderDataError("PROVIDER_REPLY_CONTRACT_INVALID"), "PROVIDER_REPLY_CONTRACT_INVALID"),
    (UnsupportedEndpoint(SECRET_VALUE), "UNSUPPORTED_ENDPOINT"),
    (ValueError(SECRET_VALUE), "REQUEST_VALIDATION_FAILED"),
    (RuntimeError(SECRET_VALUE), "PROVIDER_REQUEST_FAILED"),
])
def test_client_layer_failures_keep_their_own_fixed_code(exc, code):
    tools, _ = tools_for(client=_Raises(exc))
    r = _clean(call(tools, "apifootball_countries"))
    assert r["status"] == "PROVIDER_ERROR" and r["error"] == {"code": code} and r["network_calls_made"] == 0


def test_transport_reply_of_the_wrong_type_is_reply_contract_invalid():
    tools, _ = tools_for(Transport({"not": "a ProviderReply"}))
    r = call(tools, "apifootball_countries")
    assert r["error"] == {"code": "PROVIDER_REPLY_CONTRACT_INVALID"} and r["network_calls_made"] == 1


class _Returns:
    def __init__(self, data):
        self.data = data

    async def get(self, path, params=None):
        return self.data


def test_non_object_client_reply_and_bad_status_shape_have_fixed_codes():
    tools, _ = tools_for(client=_Returns([SECRET_VALUE]))
    assert _clean(call(tools, "apifootball_countries"))["error"] == {"code": "PROVIDER_REPLY_CONTRACT_INVALID"}
    tools, _ = tools_for(client=_Returns({"cache_status": "MISS", "provider_attempts": 2,
                                          "data": {"response": {"account": {"email": SECRET_VALUE}}}}))
    r = _clean(call(tools, "apifootball_status"))
    # The request did reach the provider: the count is not lost because a local check failed.
    assert r["error"] == {"code": "INVALID_STATUS_RESPONSE"} and r["network_calls_made"] == 2


# ---- G06: HTTP 200 with provider `errors` -----------------------------------------------------

def _declared(errors, headers=None, path="/leagues"):
    def handler(request):
        return httpx.Response(200, headers=headers or {}, json={"errors": errors, "response": []})

    async def go():
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(ProviderDeclaredErrors) as exc:
                await InjectedHTTPXTransport(http, api_key=KEY).get(path)
        return exc.value
    return asyncio.run(go())


def test_transport_keeps_declared_error_keys_and_quota_headers_never_values():
    err = _declared({"plan": SECRET_VALUE, "rateLimit": SECRET_VALUE, "bad key!": "x", 7: "y"},
                    {"x-ratelimit-requests-remaining": "0", "x-ratelimit-remaining": "4"})
    assert err.code == "PROVIDER_DECLARED_ERRORS" and isinstance(err, ProviderDataError)
    assert err.error_keys == ("OTHER", "plan", "rateLimit")
    assert (err.quota_remaining_day, err.quota_remaining_minute) == (0, 4)
    assert SECRET_VALUE not in repr(err.__dict__) and SECRET_VALUE not in str(err)
    assert _declared([{"token": SECRET_VALUE}, "text"]).error_keys == ("token",)


@pytest.mark.parametrize("keys,params,exposed,category", [
    (("plan",), (), ["plan"], "PLAN_NOT_INCLUDED"),
    (("rateLimit",), (), ["rateLimit"], "RATE_LIMIT"),
    (("requests",), (), ["requests"], "DAILY_QUOTA"),
    (("token",), (), ["token"], "AUTH"),
    (("season",), ("league", "season"), ["season"], "INVALID_PARAMETER"),
    (("season",), ("league",), ["OTHER"], "UNKNOWN"),  # not a parameter of this endpoint
    (("access",), (), ["OTHER"], "UNKNOWN"),  # no repository evidence for this key
    (("plan", "token"), (), ["plan", "token"], "UNKNOWN"),  # mixed: no single category
    (("OTHER", "plan"), (), ["OTHER", "plan"], "UNKNOWN"),
    ((), (), [], "UNKNOWN"),
])
def test_declared_error_classification(keys, params, exposed, category):
    assert classify_declared_errors(keys, params) == (exposed, category)


def test_declared_errors_reach_the_mcp_client_as_keys_and_category_with_quota():
    quota = Quota()
    transport = Transport(ProviderDeclaredErrors(("plan",), quota_remaining_day=12, quota_remaining_minute=3))
    tools, _ = tools_for(transport, quota=quota)
    r = _clean(call(tools, "apifootball_leagues", id=39, season=2010))
    assert r["error"] == {"code": "PROVIDER_DECLARED_ERRORS", "keys": ["plan"], "category": "PLAN_NOT_INCLUDED"}
    assert r["quota_remaining"] == {"day": 12, "minute": 3}
    assert r["network_calls_made"] == 1 == transport.calls  # never retried
    assert quota.marked == []
    tools, _ = tools_for(Transport(ProviderDeclaredErrors(("season",))))
    r = call(tools, "apifootball_leagues", id=39, season=2010)
    assert r["error"]["category"] == "INVALID_PARAMETER" and r["quota_remaining"] is None


@pytest.mark.parametrize("keys,day,minute,marked", [
    (("requests",), None, None, ["day"]),
    (("rateLimit",), None, None, ["minute"]),
    (("plan",), 0, None, ["day"]),
    (("plan",), 4, 0, ["minute"]),
    (("token",), 3, 2, []),
])
def test_declared_errors_reconcile_the_shared_quota(keys, day, minute, marked):
    quota = Quota()
    tools, _ = tools_for(Transport(ProviderDeclaredErrors(keys, quota_remaining_day=day,
                                                           quota_remaining_minute=minute)), quota=quota)
    call(tools, "apifootball_countries")
    assert quota.marked == marked


def test_declared_errors_end_to_end_through_the_http_transport():
    def handler(request):
        return httpx.Response(200, headers={"x-ratelimit-requests-remaining": "0", "x-ratelimit-remaining": "9"},
                              json={"errors": {"requests": SECRET_VALUE}, "response": []})

    async def go():
        async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler)) as http:
            quota = Quota()
            tools, _ = tools_for(InjectedHTTPXTransport(http, api_key=KEY), quota=quota)
            return await tools["apifootball_countries"](), quota
    r, quota = asyncio.run(go())
    assert _clean(r)["error"] == {"code": "PROVIDER_DECLARED_ERRORS", "keys": ["requests"], "category": "DAILY_QUOTA"}
    assert r["quota_remaining"] == {"day": 0, "minute": 9} and quota.marked == ["day"]


# ---- G09: network_calls_made is the real attempt count ----------------------------------------

def test_retries_that_end_in_success_are_all_counted():
    transport = Transport(ProviderHTTPError(503), ProviderHTTPError(502), ok())
    tools, _ = tools_for(transport)
    r = call(tools, "apifootball_countries")
    assert r["status"] == "REAL_API_RESPONSE" and r["network_calls_made"] == 3 == transport.calls


def test_a_cache_hit_still_counts_zero():
    transport = Transport(ok(), ok())
    tools, _ = tools_for(transport)
    call(tools, "apifootball_countries")
    r = call(tools, "apifootball_countries")
    assert r["cache_status"] == "HIT" and r["network_calls_made"] == 0 and transport.calls == 1


# ---- G11 (+G09): an oversized reply keeps the provider paging --------------------------------

def test_payload_too_large_keeps_paging_and_counts_the_request():
    big = [{"player": {"id": i, "name": "x" * 300}} for i in range(1200)]
    transport = Transport(ok(big, paging={"current": 2, "total": 5}))
    tools, _ = tools_for(transport)
    r = call(tools, "apifootball_players", league=39, season=2024, page=2)
    assert r["status"] == "PAYLOAD_TOO_LARGE" and r["data"] is None
    assert r["error"] == {"code": "PROVIDER_RESPONSE_TOO_LARGE"}
    assert r["pagination"] == {"current": 2, "total": 5}
    assert r["network_calls_made"] == 1 == transport.calls
    assert OFFLINE not in r["warnings"]
    assert len(json.dumps(r)) < 4096  # the error stays small: no data, no paging extras
