import asyncio
import httpx
import pytest

from scripts.api_football_live_smoke import MAX_CALLS, SAMPLE, preflight, run_live

BASE = {"APIFOOTBALL_ENABLED": "true",
        "APIFOOTBALL_LIVE_AUTHORIZED": "true",
        "APIFOOTBALL_API_KEY": "TEST_ONLY_PLACEHOLDER",
        "APIFOOTBALL_MAX_DAILY_REQUESTS": "4"}

@pytest.mark.parametrize("patch", [
    {"APIFOOTBALL_ENABLED": "false"},
    {"APIFOOTBALL_LIVE_AUTHORIZED": "false"},
    {"APIFOOTBALL_API_KEY": ""},
    {"APIFOOTBALL_MAX_DAILY_REQUESTS": "3"},
    {"APIFOOTBALL_BASE_URL": "https://localhost"},
])
def test_preflight_fails_closed(patch):
    with pytest.raises(ValueError):
        preflight({**BASE, **patch})

def test_live_success_is_bounded_and_redacted():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == "v3.football.api-sports.io"
        assert request.method == "GET"
        assert request.headers.get("x-apisports-key") == BASE["APIFOOTBALL_API_KEY"]
        return httpx.Response(200, json={"response": [], "results": 0, "errors": {},
                                           "parameters": []},
                              headers={"x-ratelimit-requests-remaining": "70",
                                       "x-ratelimit-remaining": "5"})
    result = asyncio.run(run_live(BASE, transport=httpx.MockTransport(handler)))
    assert len(calls) == len(SAMPLE) == MAX_CALLS
    assert result["complete"]
    assert [c.url.path for c in calls] == list(SAMPLE)
    assert BASE["APIFOOTBALL_API_KEY"] not in str(result)

@pytest.mark.parametrize("code,headers", [
    (401, {}), (429, {}), (500, {}),
    (200, {"x-ratelimit-requests-remaining": "0"}),
])
def test_stops_on_error_or_zero_quota(code, headers):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(code, json={"response": [], "errors": {}}, headers=headers)
    result = asyncio.run(run_live(BASE, transport=httpx.MockTransport(handler)))
    assert not result["complete"]
    assert len(calls) == 1
    assert result["requests_attempted"] == 1

def test_provider_errors_stop_without_body_leak():
    def handler(request):
        return httpx.Response(200, json={"errors": {"token": "SENSITIVE"}, "response": []})
    result = asyncio.run(run_live(BASE, transport=httpx.MockTransport(handler)))
    assert not result["complete"]
    assert result["results"][0]["outcome"] == "PROVIDER_ERROR_OR_INVALID_CONTRACT"
    assert "SENSITIVE" not in str(result)

def test_transport_failure_is_sanitized():
    def handler(request):
        raise httpx.ConnectError("SENSITIVE", request=request)
    result = asyncio.run(run_live(BASE, transport=httpx.MockTransport(handler)))
    assert result["results"][0]["outcome"] == "TRANSPORT_ERROR"
    assert "SENSITIVE" not in str(result)
