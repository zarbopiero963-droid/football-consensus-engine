import asyncio
import httpx
import pytest
from scripts.api_football_capability_probe import run

ENV={"APIFOOTBALL_ENABLED":"true","APIFOOTBALL_LIVE_AUTHORIZED":"true",
     "APIFOOTBALL_API_KEY":"DUMMY_SECRET","APIFOOTBALL_MAX_DAILY_REQUESTS":"4"}

def test_live_probe_is_opt_in():
    with pytest.raises(ValueError,match="LIVE_INVENTORY_NOT_AUTHORIZED"):
        asyncio.run(run({**ENV,"APIFOOTBALL_LIVE_AUTHORIZED":"false"}, transport=httpx.MockTransport(lambda r: httpx.Response(200))))

def test_live_catalog_separated_and_redacted():
    calls=[]
    def fake(request):
        calls.append(request.url.path)
        assert request.url.host == "v3.football.api-sports.io"
        return httpx.Response(200,json={"errors":{},"response":[{"id":4,"name":"Pinnacle"}],"results":1})
    data=asyncio.run(run(ENV,transport=httpx.MockTransport(fake)))
    assert calls==["/odds/bookmakers","/odds/bets","/odds/live/bets"]
    assert data[0]["phase"]=="PREMATCH"
    assert data[2]["phase"]=="LIVE"
    assert data[3]["status"]=="BLOCKED_REQUIRES_OBSERVED_FIXTURE"
    assert ENV["APIFOOTBALL_API_KEY"] not in str(data)

def test_failure_stops_before_other_calls():
    calls=[]
    def fake(request):
        calls.append(request.url.path)
        return httpx.Response(429,json={"errors":{"rate_limit":"EXCEEDED"}})
    data=asyncio.run(run(ENV,transport=httpx.MockTransport(fake)))
    assert calls==["/odds/bookmakers"]
    assert data[0]["status"]=="HTTP_ERROR"
    assert "EXCEEDED" not in str(data)
