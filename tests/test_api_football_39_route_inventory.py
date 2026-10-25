import asyncio
import httpx
import pytest

from scripts.api_football_39_route_inventory import ROUTES, CAP, plan, inventory, discover

ENV = {"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "true",
       "APIFOOTBALL_API_KEY": "SAFE_TEST_VALUE", "APIFOOTBALL_MAX_DAILY_REQUESTS": "39"}

async def no_wait(_):
    pass

def test_39_distinct_fixed_routes():
    assert len(ROUTES) == len(set(ROUTES)) == CAP == 39

def test_not_authorized_and_insufficient_budget():
    for update in ({"APIFOOTBALL_LIVE_AUTHORIZED": "false"}, {"APIFOOTBALL_MAX_DAILY_REQUESTS": "38"}):
        with pytest.raises(ValueError):
            asyncio.run(inventory({**ENV, **update}, sleep=no_wait))

def test_dynamic_route_never_invents_ids():
    assert plan("/fixtures/statistics", {}) is None
    assert plan("/odds/live", {"fixture": 123}) is None
    assert plan("/teams/statistics", {"team": 32}) == {"team": 32, "league": 39, "season": 2025}

def test_discovery_uses_actual_provider_ids_only():
    ids = {}
    discover("/fixtures", {"response": [{"fixture": {"id": 123, "status": {"short": "FT"}},
                       "teams": {"home": {"id": 10}, "away": {"id": 20}}}]}, ids)
    assert ids == {"fixture": 123, "h2h": "10-20"}
    discover("/fixtures", {"response": [{"fixture": {"id": 124, "status": {"short": "1H"}}}]}, ids)
    assert ids["live_fixture"] == 124

def test_empty_is_not_data_available():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.url.host == "v3.football.api-sports.io"
        assert request.headers["x-apisports-key"] == ENV["APIFOOTBALL_API_KEY"]
        return httpx.Response(200, json={"errors": {}, "response": [], "results": 0},
                              headers={"x-ratelimit-requests-remaining": "200", "x-ratelimit-remaining": "10"})
    report = asyncio.run(inventory(ENV, transport=httpx.MockTransport(handler), sleep=no_wait))
    assert len(report["records"]) == 39
    assert len(calls) <= 39
    assert len(calls) == report["requests_attempted"]
    assert report["records"][0]["outcome"] == "PASS_EMPTY_NOT_AVAILABILITY"
    assert any(r["outcome"] == "BLOCKED_MISSING_REAL_PREREQUISITES" for r in report["records"])
    assert ENV["APIFOOTBALL_API_KEY"] not in str(report)

def test_429_stops_without_retry():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, json={"errors": {"x": "NO"}}, headers={})
    report = asyncio.run(inventory(ENV, transport=httpx.MockTransport(handler), sleep=no_wait))
    assert calls == ["/status"]
    assert report["requests_attempted"] == 1
    assert any(r["outcome"] == "BLOCKED_QUOTA_OR_PROVIDER" for r in report["records"])

def test_known_zero_quota_stops():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"errors": {}, "response": {"ok": True}},
                              headers={"x-ratelimit-requests-remaining": "0"})
    report = asyncio.run(inventory(ENV, transport=httpx.MockTransport(handler), sleep=no_wait))
    assert calls == ["/status"]
    assert report["requests_attempted"] == 1
