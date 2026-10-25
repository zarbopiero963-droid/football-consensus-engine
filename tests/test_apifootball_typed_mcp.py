"""PR-03 — deterministic MCP tool schema/validation and no-network tests.

These tests never contact API-FOOTBALL or instantiate the HTTPX transport.
No provider key is obtained from environment. Every test key is fake.
"""
from __future__ import annotations

import asyncio
import inspect

from bridge.api_football.client import MockFootballClient
from bridge.api_football.config import Settings
from bridge.api_football.contracts import ENDPOINTS, ParameterError, validate, SOURCE
from bridge.api_football.tools import register_tools


class FakeRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            assert fn.__name__ not in self.tools
            self.tools[fn.__name__] = fn
            return fn
        return attach


def _call(coro):
    return asyncio.run(coro)


def _settings(*, key=""):
    return Settings(enabled=True, api_key=key)


def test_all_endpoints_have_fixed_paths_and_no_generic_arbitrary_url_tool():
    assert len(ENDPOINTS) == 40
    assert ENDPOINTS["apifootball_fixtures"].path == "/fixtures"
    assert ENDPOINTS["apifootball_odds"].path == "/odds"
    assert ENDPOINTS["apifootball_live_odds"].path == "/odds/live"
    assert ENDPOINTS["apifootball_predictions"].path == "/predictions"
    assert SOURCE == "https://www.api-football.com/documentation_v3"
    assert not any("raw" in name or "request" in name or "url" in name for name in ENDPOINTS)


def test_all_42_tools_have_typed_signatures_and_explicit_identity():
    registry = FakeRegistry()
    names = register_tools(registry, _settings())
    assert len(names) == len(set(names)) == 42
    assert names[0] == "apifootball_health"
    assert names[-1] == "apifootball_quota_report"
    assert set(names) == set(registry.tools)
    for name, fn in registry.tools.items():
        assert inspect.iscoroutinefunction(fn)
        assert fn.__doc__ and len(fn.__doc__.strip()) >= 20
        signature = inspect.signature(fn)
        assert all(parameter.kind.name not in ("VAR_POSITIONAL", "VAR_KEYWORD") for parameter in signature.parameters.values())
        assert signature.return_annotation != inspect.Signature.empty


def test_disabling_module_registers_no_tools_even_with_mock():
    registry = FakeRegistry()
    mock = MockFootballClient({"/fixtures": {"response": []}})
    names = register_tools(registry, Settings(), test_mock=mock)
    assert names == ()
    assert registry.tools == {}
    assert mock.calls == []


def test_not_configured_tools_remain_callable_but_make_no_provider_calls():
    registry = FakeRegistry()
    register_tools(registry, _settings())
    cases = [
        ("apifootball_status", {}),
        ("apifootball_countries", {}),
        ("apifootball_leagues", {"id": 39, "season": 2026}),
        ("apifootball_seasons", {}),
        ("apifootball_fixtures", {"id": 123}),
        ("apifootball_fixture_statistics", {"fixture": 123}),
        ("apifootball_fixture_events", {"fixture": 123}),
        ("apifootball_fixture_lineups", {"fixture": 123}),
        ("apifootball_teams", {"id": 10}),
        ("apifootball_team_statistics", {"league": 39, "season": 2026, "team": 10}),
        ("apifootball_players", {"team": 10, "season": 2026}),
        ("apifootball_injuries", {"fixture": 123}),
        ("apifootball_predictions", {"fixture": 123}),
        ("apifootball_odds", {"fixture": 123, "page": 2}),
        ("apifootball_live_odds", {"fixture": 123}),
        ("apifootball_bookmakers", {}),
        ("apifootball_bets", {}),
        ("apifootball_coverage_report", {"id": 39, "season": 2026}),
    ]
    assert len(cases) == 18
    for name, args in cases:
        result = _call(registry.tools[name](**args))
        assert result["status"] == "KEY_NOT_CONFIGURED", (name, result)
        assert result["data"] is None
        assert result["network_calls_made"] == 0
        assert result["documentation_version"] == "v3.9.3"
        assert result["source_contract"] in ("DOCUMENTED_OFFICIAL", "PENDING_PARAMETER_RECONCILIATION")
        assert result["provider_timestamp_utc"] is None


def test_diagnostic_limits_are_not_fake_provider_quota():
    registry = FakeRegistry()
    register_tools(registry, _settings())
    report = _call(registry.tools["apifootball_quota_report"]())
    assert report["status"] == "CONFIGURED_LIMITS_ONLY"
    assert report["provider_quota_verified"] is False
    assert report["remaining_provider_quota"] is None
    assert report["live_requests_enabled"] is False


def test_missing_real_authorization_stays_blocked_even_if_fake_key_present():
    key = "DUMMY_NEVER_REAL_PROVIDER_KEY"
    registry = FakeRegistry()
    register_tools(registry, _settings(key=key))
    result = _call(registry.tools["apifootball_fixtures"](id=12))
    assert result["status"] == "LIVE_NOT_AUTHORIZED"
    assert result["data"] is None
    assert result["network_calls_made"] == 0
    assert key not in str(result)


def test_fixture_filter_validation_and_stable_input_errors():
    r = FakeRegistry()
    register_tools(r, _settings())
    for args, code in [
        ({}, "FIXTURE_FILTER_REQUIRED"),
        ({"id": 0}, "INVALID_INTEGER_PARAMETER"),
        ({"id": -9}, "INVALID_INTEGER_PARAMETER"),
        ({"id": True}, "INVALID_INTEGER_PARAMETER"),
        ({"date": "2026-02-30"}, "INVALID_DATE"),
        ({"date": "2026-10-24\nHost: evil.example"}, "INVALID_TEXT_PARAMETER"),
        ({"live": "private"}, "INVALID_LIVE_FILTER"),
        ({"timezone": "Etc/GMT+13", "id": 1}, "TIMEZONE_NOT_CERTIFIED"),
        ({"league": 39}, "FIXTURE_FILTER_REQUIRED"),
        ({"team": 9}, "FIXTURE_FILTER_REQUIRED"),
        ({"last": 101, "team": 9}, "PAGE_OR_BATCH_TOO_LARGE"),
    ]:
        outcome = _call(r.tools["apifootball_fixtures"](**args))
        assert outcome["status"] == "INVALID_INPUT"
        assert outcome["error"]["code"] == code
        assert outcome["network_calls_made"] == 0


def test_odds_require_target_and_pagination_is_bounded():
    registry = FakeRegistry()
    register_tools(registry, _settings())
    no_filter = _call(registry.tools["apifootball_odds"]())
    assert no_filter["error"]["code"] == "ODDS_FILTER_REQUIRED"
    oversized = _call(registry.tools["apifootball_odds"](fixture=55, page=1_000_000))
    assert oversized["error"]["code"] == "PAGE_OR_BATCH_TOO_LARGE"
    wrong_season = _call(registry.tools["apifootball_odds"](league=39, season=1700))
    assert wrong_season["error"]["code"] == "INVALID_SEASON"
    good = _call(registry.tools["apifootball_odds"](fixture=55, bookmaker=8, bet=1, page=2))
    assert good["status"] == "KEY_NOT_CONFIGURED"
    assert good["parameters"] == {"fixture": 55, "bookmaker": 8, "bet": 1, "page": 2}


def test_prediction_and_live_odds_never_pretend_to_be_own_models():
    registry = FakeRegistry()
    register_tools(registry, _settings())
    predict = _call(registry.tools["apifootball_predictions"](fixture=50))
    live = _call(registry.tools["apifootball_live_odds"](fixture=50))
    assert predict["endpoint"] == "/predictions"
    assert live["endpoint"] == "/odds/live"
    assert predict["status"] == live["status"] == "KEY_NOT_CONFIGURED"
    assert predict["data"] is None
    assert live["provider_timestamp_utc"] is None


def test_mock_execution_preserves_raw_zero_null_absent_and_paging():
    payload = {
        "get": "fixtures", "errors": [], "results": 1,
        "paging": {"current": 1, "total": 1},
        "response": [{"id": 42, "shots": 0, "xg": None, "blank": ""}],
    }
    mock = MockFootballClient({"/fixtures": payload})
    reg = FakeRegistry()
    register_tools(reg, _settings(key="NONPRODUCTION_FAKE_KEY"), test_mock=mock)
    outcome = _call(reg.tools["apifootball_fixtures"](id=42))
    assert outcome["status"] == "MOCK_VERIFIED"
    assert outcome["cache_status"] == "TEST_FIXTURE"
    assert outcome["data"]["response"][0] == {"id": 42, "shots": 0, "xg": None, "blank": ""}
    assert "xgot" not in outcome["data"]["response"][0]
    assert outcome["pagination"] == {"current": 1, "total": 1}
    assert outcome["provider_timestamp_utc"] is None
    assert mock.calls == [("/fixtures", {"id": 42})]
    assert outcome["network_calls_made"] == 0


def test_mock_large_payload_is_rejected_without_data_leak():
    mock = MockFootballClient({"/fixtures": {"response": [{"blob": "z" * 300000}]}})
    reg = FakeRegistry()
    register_tools(reg, _settings(key="FAKE"), test_mock=mock)
    result = _call(reg.tools["apifootball_fixtures"](id=1))
    assert result["status"] == "PAYLOAD_TOO_LARGE"
    assert result["data"] is None
    assert result["error"]["code"] == "MOCK_RESPONSE_TOO_LARGE"
    assert "z" * 1000 not in str(result)


def test_mock_unexpected_error_is_redacted_and_isolated():
    class ThrowingMock:
        async def get(self, path, params=None):
            raise RuntimeError("SECRET_API_KEY_DO_NOT_LOG")

    reg = FakeRegistry()
    register_tools(reg, _settings(key="DUMMY"), test_mock=ThrowingMock())
    result = _call(reg.tools["apifootball_fixtures"](id=1))
    assert result["status"] == "MOCK_ERROR"
    assert result["error"]["code"] == "MOCK_FIXTURE_UNAVAILABLE"
    assert "SECRET_API_KEY_DO_NOT_LOG" not in str(result)


def test_backend_contract_rejects_arbitrary_path_and_unknown_query_key():
    for name, data, expected in [
        ("apifootball_unknown", {}, "UNKNOWN_TOOL"),
        ("apifootball_fixtures", {"raw_url": "http://127.0.0.1"}, "PARAMETER_NOT_SUPPORTED"),
        ("apifootball_fixture_events", {"fixture": 1, "auth": "secret"}, "PARAMETER_NOT_SUPPORTED"),
        ("apifootball_odds", {"fixture": 7, "page": False}, "INVALID_INTEGER_PARAMETER"),
    ]:
        try:
            validate(name, data)
        except ParameterError as e:
            assert str(e) == expected
        else:
            raise AssertionError((name, expected))


def test_actual_mcp_sdk_generates_valid_typed_tool_discovery_schemas():
    """Inspect the real MCP v2 SDK, not merely a fake decorator registry."""
    from mcp.server import MCPServer

    async def go():
        instance = MCPServer("apifootball-schema-test")
        names = register_tools(instance, _settings())
        listing = await instance.list_tools()
        assert len(listing) == 42
        assert {tool.name for tool in listing} == set(names)
        for tool in listing:
            assert tool.description and len(tool.description) >= 20
            schema = tool.input_schema
            assert isinstance(schema, dict)
            assert schema.get("type") == "object"
            assert "url" not in schema.get("properties", {})
            assert "api_key" not in schema.get("properties", {})
        fixtures = next(t for t in listing if t.name == "apifootball_fixtures")
        assert "id" in fixtures.input_schema["properties"]
        stats = next(t for t in listing if t.name == "apifootball_fixture_statistics")
        assert "fixture" in stats.input_schema["properties"]
        assert "fixture" in stats.input_schema.get("required", [])
        assert not stats.input_schema["properties"].get("url")
    asyncio.run(go())


def test_production_mcp_registry_still_has_exactly_original_34_browser_tools():
    """Production imports with flag absent must remain discoverable by MCP SDK."""
    import bridge.server as server

    async def go():
        listing = await server.mcp.list_tools()
        names = [t.name for t in listing]
        assert len(names) == 34
        assert len(set(names)) == 34
        assert all(n.startswith("browser_") for n in names)
        assert "browser_health" in names
        assert "browser_create_cloak" in names
        assert "browser_get_network_responses" in names
    asyncio.run(go())
