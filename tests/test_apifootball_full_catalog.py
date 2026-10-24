"""PR-03 full 38-football-endpoint certification, no real API access.

All endpoint calls in this suite return explicit OFFLINE statuses or
use fixture-backed Python-only mocks; secrets and HTTP sockets are not used.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from bridge.api_football.catalog import FOOTBALL_PATHS, ACCOUNT_PATHS, ALLOWED_PATHS
from bridge.api_football.config import Settings
from bridge.api_football.contracts import ENDPOINTS, ParameterError, validate
from bridge.api_football.tools import register_tools
from bridge.api_football.client import MockFootballClient


class FakeMCP:
    def __init__(self):
        self.funcs = {}

    def tool(self):
        def wrapped(fn):
            assert fn.__name__ not in self.funcs
            self.funcs[fn.__name__] = fn
            return fn
        return wrapped


NEW_CASES = (
    ("apifootball_timezones", {}, "/timezone"),
    ("apifootball_team_seasons", {"team": 33}, "/teams/seasons"),
    ("apifootball_team_countries", {}, "/teams/countries"),
    ("apifootball_venues", {"id": 123}, "/venues"),
    ("apifootball_standings", {"league": 39, "season": 2025}, "/standings"),
    ("apifootball_fixture_rounds", {"league": 39, "season": 2025, "dates": True, "current": False}, "/fixtures/rounds"),
    ("apifootball_fixture_headtohead", {"h2h": "33-34"}, "/fixtures/headtohead"),
    ("apifootball_fixture_players", {"fixture": 123}, "/fixtures/players"),
    ("apifootball_coaches", {"team": 33}, "/coachs"),
    ("apifootball_player_seasons", {}, "/players/seasons"),
    ("apifootball_player_profiles", {"page": 1}, "/players/profiles"),
    ("apifootball_player_squads", {"team": 33}, "/players/squads"),
    ("apifootball_player_teams", {"player": 742}, "/players/teams"),
    ("apifootball_top_scorers", {"league": 39, "season": 2025}, "/players/topscorers"),
    ("apifootball_top_assists", {"league": 39, "season": 2025}, "/players/topassists"),
    ("apifootball_top_yellow_cards", {"league": 39, "season": 2025}, "/players/topyellowcards"),
    ("apifootball_top_red_cards", {"league": 39, "season": 2025}, "/players/topredcards"),
    ("apifootball_transfers", {"player": 742}, "/transfers"),
    ("apifootball_trophies", {"coach": 12}, "/trophies"),
    ("apifootball_sidelined", {"player": 742}, "/sidelined"),
    ("apifootball_odds_mapping", {}, "/odds/mapping"),
    ("apifootball_live_bets", {"id": "23"}, "/odds/live/bets"),
)


def test_complete_unique_inventory_is_38_football_plus_status():
    assert len(FOOTBALL_PATHS) == 38
    assert len(set(FOOTBALL_PATHS)) == 38
    assert ACCOUNT_PATHS == ("/status",)
    assert len(ALLOWED_PATHS) == 39
    assert {ep.path for ep in ENDPOINTS.values()} == ALLOWED_PATHS
    assert len(ENDPOINTS) == 40  # /leagues also has coverage report
    assert len(NEW_CASES) == 22
    assert len({row[2] for row in NEW_CASES}) == 22
    registry = FakeMCP()
    names = register_tools(registry, Settings(enabled=True))
    assert len(names) == 42
    assert set(names) == set(registry.funcs)
    assert set(ENDPOINTS).issubset(names)
    assert set(names) - set(ENDPOINTS) == {"apifootball_health", "apifootball_quota_report"}


@pytest.mark.parametrize("name,args,path", NEW_CASES)
def test_each_missing_provider_endpoint_has_a_real_typed_mcp_function(name, args, path):
    registry = FakeMCP()
    register_tools(registry, Settings(enabled=True))
    fn = registry.funcs[name]
    assert inspect.iscoroutinefunction(fn)
    assert fn.__doc__ and len(fn.__doc__) > 22
    signature = inspect.signature(fn)
    assert not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
    assert not {"api_key", "url", "path"} & set(signature.parameters)
    endpoint, checked = validate(name, args)
    assert endpoint.path == path
    response = asyncio.run(fn(**args))
    assert response["provider"] == "API-FOOTBALL"
    assert response["endpoint"] == path
    assert response["status"] == "KEY_NOT_CONFIGURED"
    assert response["network_calls_made"] == 0
    assert response["data"] is None
    assert response["provider_timestamp_utc"] is None
    assert response["parameters"] == checked


@pytest.mark.parametrize("name,args,path", NEW_CASES)
def test_even_with_key_all_new_endpoints_remain_offline(name, args, path):
    # Only a dummy secret; no actual HTTP adapter is passed to MCP.
    secret = "CI_FAKE_KEY_NEVER_LOGGED"
    registry = FakeMCP()
    register_tools(registry, Settings(enabled=True, api_key=secret))
    result = asyncio.run(registry.funcs[name](**args))
    assert result["endpoint"] == path
    assert result["status"] == "REAL_API_TEST_NOT_AUTHORIZED"
    assert result["data"] is None
    assert result["network_calls_made"] == 0
    assert secret not in str(result)


@pytest.mark.parametrize("name,args,code", (
    ("apifootball_venues", {}, "VENUE_FILTER_REQUIRED"),
    ("apifootball_coaches", {}, "COACH_FILTER_REQUIRED"),
    ("apifootball_player_squads", {}, "SQUAD_FILTER_REQUIRED"),
    ("apifootball_transfers", {}, "PERSON_OR_TEAM_FILTER_REQUIRED"),
    ("apifootball_trophies", {}, "PERSON_OR_TEAM_FILTER_REQUIRED"),
    ("apifootball_sidelined", {}, "PERSON_OR_TEAM_FILTER_REQUIRED"),
    ("apifootball_standings", {"league": 39}, "REQUIRED_PARAMETER_MISSING"),
    ("apifootball_team_seasons", {"team": 0}, "INVALID_INTEGER_PARAMETER"),
    ("apifootball_fixture_rounds", {"league": 39, "season": 2025, "dates": "true"}, "INVALID_BOOLEAN_PARAMETER"),
    ("apifootball_fixture_rounds", {"league": 39, "season": 2025, "current": 1}, "INVALID_BOOLEAN_PARAMETER"),
    ("apifootball_fixture_headtohead", {"h2h": "33-34\nURL:http://localhost"}, "INVALID_HEAD_TO_HEAD_FILTER"),
    ("apifootball_fixture_headtohead", {"h2h": "0-34"}, "INVALID_HEAD_TO_HEAD_FILTER"),
    ("apifootball_fixture_headtohead", {"h2h": "33/34"}, "INVALID_HEAD_TO_HEAD_FILTER"),
    ("apifootball_fixture_headtohead", {"h2h": "33-34", "last": 101}, "PAGE_OR_BATCH_TOO_LARGE"),
    ("apifootball_player_profiles", {"page": 101}, "PAGE_OR_BATCH_TOO_LARGE"),
    ("apifootball_live_bets", {"id": 23}, "INVALID_TEXT_PARAMETER"),
    ("apifootball_live_bets", {"id": "23A"}, "INVALID_LIVE_BET_ID"),
    ("apifootball_live_bets", {"search": "AB"}, "SEARCH_TOO_SHORT"),
    ("apifootball_odds_mapping", {"url": "http://127.0.0.1"}, "PARAMETER_NOT_SUPPORTED"),
))
def test_adversarial_parameter_cases_are_rejected_before_provider(name, args, code):
    with pytest.raises(ParameterError, match=code):
        validate(name, args)


def test_round_booleans_are_normalized_to_provider_lowercase_values():
    ep, params = validate("apifootball_fixture_rounds", {
        "league": 39, "season": 2025, "dates": True, "current": False,
    })
    assert params == {
        "league": 39, "season": 2025, "dates": "true", "current": "false",
    }
    assert ep.path == "/fixtures/rounds"


def test_live_and_prematch_bet_id_types_are_separated():
    ep, live = validate("apifootball_live_bets", {"id": "42"})
    assert ep.path == "/odds/live/bets" and live == {"id": "42"}
    ep, pre = validate("apifootball_bets", {"id": 42})
    assert ep.path == "/odds/bets" and pre == {"id": 42}
    with pytest.raises(ParameterError):
        validate("apifootball_live_bets", {"id": 42})


def test_mock_full_endpoint_catalog_preserves_zero_null_empty_absent():
    fixtures = {}
    for _, _, path in NEW_CASES:
        fixtures[path] = {
            "get": path.lstrip("/"), "results": 1,
            "response": [{"count": 0, "xg": None, "empty": ""}],
            "paging": {"current": 1, "total": 1},
            "errors": [],
        }
    mock = MockFootballClient(fixtures)
    registry = FakeMCP()
    register_tools(registry, Settings(enabled=True, api_key="FAKE_LOCAL_KEY"), test_mock=mock)

    async def run_all():
        results = []
        for name, args, path in NEW_CASES:
            r = await registry.funcs[name](**args)
            assert r["status"] == "MOCK_VERIFIED", (name, r)
            assert r["endpoint"] == path
            assert r["data"]["response"][0] == {
                "count": 0, "xg": None, "empty": "",
            }
            assert "xgot" not in r["data"]["response"][0]
            assert r["provider_timestamp_utc"] is None
            assert r["network_calls_made"] == 0
            assert r["source_contract"] in (
                "DOCUMENTED_OFFICIAL", "PENDING_PARAMETER_RECONCILIATION"
            )
            results.append(r)
        return results

    result = asyncio.run(run_all())
    assert len(result) == 22
    assert len(mock.calls) == 22
    assert len({path for path, _ in mock.calls}) == 22


def test_disabled_registry_and_original_browser_discovery_remain_unchanged():
    import bridge.server as server
    registry = FakeMCP()
    assert register_tools(registry, Settings()) == ()
    assert not registry.funcs
    assert server.APIFOOTBALL_REGISTERED_TOOLS == ()

    async def check_browser_tools():
        tools = await server.mcp.list_tools()
        names = {t.name for t in tools}
        assert len(names) == 34
        assert all(name.startswith("browser_") for name in names)
    asyncio.run(check_browser_tools())


def test_real_mcp_sdk_discovery_all_42_tools_with_no_arbitrary_url():
    from mcp.server import MCPServer

    async def go():
        registry = MCPServer("full-catalog-offline-test")
        expected = register_tools(registry, Settings(enabled=True))
        discovered = await registry.list_tools()
        assert len(discovered) == len(expected) == 42
        assert {t.name for t in discovered} == set(expected)
        for tool in discovered:
            assert tool.description and tool.input_schema["type"] == "object"
            assert "url" not in tool.input_schema.get("properties", {})
            assert "api_key" not in tool.input_schema.get("properties", {})
        rounds = next(t for t in discovered if t.name == "apifootball_fixture_rounds")
        assert "dates" in rounds.input_schema["properties"]
        assert "league" in rounds.input_schema.get("required", [])
        h2h = next(t for t in discovered if t.name == "apifootball_fixture_headtohead")
        assert "h2h" in h2h.input_schema.get("required", [])
    asyncio.run(go())
