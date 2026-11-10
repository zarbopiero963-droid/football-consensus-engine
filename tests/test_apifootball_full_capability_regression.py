"""Regression across all 40 provider tools after FIX-01..FIX-09, through the REAL pinned
transport, resilience client and MCP tools over httpx.MockTransport (no network): the
request reaches the documented path with exactly the validated parameters, the reply comes
back losslessly, the FIX-07 time fields are present and labelled, network_calls_made is
exact, a repeated call is a cache HIT that keeps the acquisition time."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.contracts import ENDPOINTS, validate
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import register_tools
from bridge.api_football.transport import InjectedHTTPXTransport

KEY = "LOCAL_FAKE_KEY_REGRESSION_ONLY"
MINIMAL = {
    "apifootball_status": {}, "apifootball_countries": {}, "apifootball_leagues": {"id": 39},
    "apifootball_seasons": {}, "apifootball_fixtures": {"id": 1}, "apifootball_fixture_statistics": {"fixture": 1},
    "apifootball_fixture_events": {"fixture": 1}, "apifootball_fixture_lineups": {"fixture": 1},
    "apifootball_teams": {"id": 33}, "apifootball_team_statistics": {"league": 39, "season": 2026, "team": 33},
    "apifootball_players": {"team": 33, "season": 2026}, "apifootball_injuries": {"fixture": 1},
    "apifootball_predictions": {"fixture": 1}, "apifootball_odds": {"fixture": 1, "bet": 5},
    "apifootball_live_odds": {"fixture": 1}, "apifootball_bookmakers": {}, "apifootball_bets": {},
    "apifootball_coverage_report": {"id": 39, "season": 2026}, "apifootball_timezones": {},
    "apifootball_team_seasons": {"team": 33}, "apifootball_team_countries": {}, "apifootball_venues": {"id": 1},
    "apifootball_standings": {"league": 39, "season": 2026},
    "apifootball_fixture_rounds": {"league": 39, "season": 2026, "timezone": "Europe/Rome"},
    "apifootball_fixture_headtohead": {"h2h": "33-34", "last": 5}, "apifootball_fixture_players": {"fixture": 1},
    "apifootball_coaches": {"team": 33}, "apifootball_player_seasons": {}, "apifootball_player_profiles": {"page": 101},
    "apifootball_player_squads": {"team": 33}, "apifootball_player_teams": {"player": 7},
    "apifootball_top_scorers": {"league": 39, "season": 2026}, "apifootball_top_assists": {"league": 39, "season": 2026},
    "apifootball_top_yellow_cards": {"league": 39, "season": 2026},
    "apifootball_top_red_cards": {"league": 39, "season": 2026}, "apifootball_transfers": {"player": 7},
    "apifootball_trophies": {"player": 7}, "apifootball_sidelined": {"player": 7},
    "apifootball_odds_mapping": {"page": 2}, "apifootball_live_bets": {},
}
STATUS_BODY = {"subscription": {"plan": "X", "active": True, "end": "2027-01-01"},
               "requests": {"current": 1, "limit_day": 7500}, "account": {"email": "never@example.invalid"}}


def test_the_case_list_covers_all_40_provider_tools():
    assert set(MINIMAL) == set(ENDPOINTS) and len(MINIMAL) == 40
    for name, args in MINIMAL.items():
        validate(name, args)


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


@pytest.mark.parametrize("name", sorted(MINIMAL))
def test_every_provider_tool_end_to_end(name):
    seen = []
    row = {"fixture": {"id": 1, "timestamp": 1_794_000_000, "date": "2026-11-07T19:00:00+01:00"}, "nullable": None,
           "zero": 0}

    def handler(request):
        seen.append((request.url.path, dict(request.url.params), request.headers.get("x-apisports-key")))
        response = STATUS_BODY if name == "apifootball_status" else [row]
        return httpx.Response(200, headers={"date": "Tue, 10 Nov 2026 21:04:05 GMT"},
                              json={"get": request.url.path.strip("/"), "parameters": dict(request.url.params),
                                    "errors": [], "results": 1, "paging": {"current": 1, "total": 1},
                                    "response": response})

    settings = Settings(enabled=True, api_key=KEY, live_authorized=True)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler))
    client = ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=KEY), quota=Quota(),
                                     retry=RetryPolicy(base_delay=0.0, max_delay=0.0))
    server = Server()
    register_tools(server, settings, client=client)
    args = MINIMAL[name]
    kwargs = {("from_date" if k == "from" else "to_date" if k == "to" else k): v for k, v in args.items()}
    _, validated = validate(name, args)

    out = asyncio.run(server.tools[name](**kwargs))
    assert out["status"] == "REAL_API_RESPONSE", out.get("error")
    path, params, key = seen[0]
    assert path == ENDPOINTS[name].path and key == KEY
    assert params == {k: str(v) for k, v in validated.items()}
    assert out["network_calls_made"] == 1 and out["cache_status"] == "MISS"
    assert out["provider_timestamp_utc"] == "2026-11-10T21:04:05+00:00"
    assert out["retrieved_at_utc"] and out["responded_at_utc"] and out["age_seconds"] is not None
    assert out["time_sources"]["responded_at_utc"] == "BRIDGE_SERVER_CLOCK"
    assert set(out["request_subject"]) <= {"fixture", "id", "ids", "bet", "bookmaker"}
    text = json.dumps(out)
    assert KEY not in text and "never@example.invalid" not in text
    if name != "apifootball_status":
        assert out["data"]["response"] == [row]  # null and zero untouched
    assert ("pagination_state" in out) == ENDPOINTS[name].pagination
    assert ("combination" in out) == (name == "apifootball_fixtures")
    assert ("live_odds_state" in out) == (name == "apifootball_live_odds")
    assert ("timezone_context" in out) == ("timezone" in args)

    again = asyncio.run(server.tools[name](**kwargs))
    if name == "apifootball_status":
        assert again["cache_status"] == "MISS" and len(seen) == 2  # /status is never cached
    else:
        assert again["cache_status"] == "HIT" and again["network_calls_made"] == 0 and len(seen) == 1
        assert again["retrieved_at_utc"] == out["retrieved_at_utc"]
        assert again["cache_age_seconds"] is not None
