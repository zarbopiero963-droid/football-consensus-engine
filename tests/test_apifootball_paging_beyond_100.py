"""FIX-04 (G04): page > 100 reachable where the provider pages further (players/profiles: 250
rows per page). Positive-integer validation stays; the bound is the transport's own paging
bound. current/total, the last page and a page past the end are handled; nothing is fetched
ahead (never auto-download every page)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.contracts import ENDPOINTS, MAX_PAGE, ParameterError, validate
from bridge.api_football.errors import ProviderDataError
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import register_tools
from bridge.api_football.transport import InjectedHTTPXTransport

KEY = "LOCAL_FAKE_KEY_PAGING_ONLY"
PAGED = [n for n, e in ENDPOINTS.items() if e.pagination]


def test_paginated_tools_are_the_documented_four():
    assert sorted(PAGED) == ["apifootball_odds", "apifootball_odds_mapping", "apifootball_player_profiles",
                             "apifootball_players"]


BASE = {"apifootball_odds": {"fixture": 1}, "apifootball_odds_mapping": {}, "apifootball_player_profiles": {},
        "apifootball_players": {"team": 33, "season": 2026}}


@pytest.mark.parametrize("tool", PAGED)
@pytest.mark.parametrize("page", [1, 100, 101, 250, 4321, MAX_PAGE])
def test_pages_above_100_are_accepted(tool, page):
    assert validate(tool, {**BASE[tool], "page": page})[1]["page"] == page


@pytest.mark.parametrize("tool", PAGED)
@pytest.mark.parametrize("page", [0, -1, MAX_PAGE + 1, True, 2.0, "2"])
def test_non_positive_or_out_of_bound_pages_are_refused(tool, page):
    with pytest.raises(ParameterError):
        validate(tool, {**BASE[tool], "page": page})


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


def _tools(response, paging, seen):
    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"get": "players/profiles", "parameters": dict(request.url.params),
                                         "errors": [], "results": len(response), "paging": paging,
                                         "response": response})
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler))
    client = ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=KEY), quota=Quota(),
                                     retry=RetryPolicy(base_delay=0.0, max_delay=0.0))
    server = Server()
    register_tools(server, settings, client=client)
    return server.tools


def test_page_250_of_a_long_catalog_is_fetched_and_points_to_the_next_page_only():
    seen = []
    tools = _tools([{"player": {"id": 1}}], {"current": 250, "total": 1400}, seen)
    out = asyncio.run(tools["apifootball_player_profiles"](page=250))
    assert seen == [{"page": "250"}]          # exactly one request: nothing fetched ahead
    assert out["status"] == "REAL_API_RESPONSE" and out["pagination"] == {"current": 250, "total": 1400}
    st = out["pagination_state"]
    assert st["state"] == "MORE_PAGES" and st["has_next"] and st["next_page"] == 251
    assert st["next_parameters"] == {"page": 251} and st["auto_fetch"] is False
    # The suggested next call passes validation as a plain parameter set.
    assert validate("apifootball_player_profiles", st["next_parameters"])[1] == {"page": 251}


def test_last_page_has_no_next():
    seen = []
    out = asyncio.run(_tools([{"player": {"id": 1}}], {"current": 1400, "total": 1400}, seen)
                      ["apifootball_player_profiles"](page=1400))
    assert out["pagination_state"]["state"] == "LAST_PAGE" and out["pagination_state"]["next_page"] is None


def test_page_past_the_end_is_an_explicit_empty_answer():
    seen = []
    out = asyncio.run(_tools([], {"current": 1500, "total": 1400}, seen)["apifootball_player_profiles"](page=1500))
    assert out["status"] == "REAL_API_RESPONSE" and out["data"]["response"] == []
    assert out["pagination_state"]["state"] == "PAGE_OUT_OF_RANGE" and out["pagination_state"]["has_next"] is False


def test_no_pages_listing():
    seen = []
    out = asyncio.run(_tools([], {"current": 1, "total": 0}, seen)["apifootball_player_profiles"](search="zzzzzz"))
    assert out["pagination_state"]["state"] == "NO_PAGES"


@pytest.mark.parametrize("paging", [{"current": 1500, "total": 1400}, {"current": 0, "total": 0},
                                    {"current": 2, "total": 100_001}, {"current": "2", "total": 3}])
def test_rows_on_an_impossible_page_stay_invalid(paging):
    seen = []
    out = asyncio.run(_tools([{"player": {"id": 1}}], paging, seen)["apifootball_player_profiles"](page=2))
    assert out["status"] == "PROVIDER_ERROR" and out["error"] == {"code": "PROVIDER_INVALID_PAGINATION"}


def test_unpaginated_tools_have_no_pagination_state():
    seen = []
    out = asyncio.run(_tools([{"x": 1}], {"current": 1, "total": 1}, seen)["apifootball_countries"]())
    assert "pagination_state" not in out
