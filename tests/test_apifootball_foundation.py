"""FCB-APIFOOTBALL-INTEGRATION-CERT-01 / PR-01: mock-only, no network."""
from __future__ import annotations

import asyncio
import ast
import inspect
import sys

import pytest

from bridge.api_football.catalog import (
    ACCOUNT_PATHS, ALLOWED_PATHS, EVIDENCE, FOOTBALL_PATHS, documented_path,
)
from bridge.api_football.client import (
    MockFootballClient, ProviderNotAuthorized, RealFootballClient, UnsupportedEndpoint,
)
from bridge.api_football.config import ConfigurationError, OFFICIAL_BASE_URL, Settings
from bridge.api_football.tools import register_tools


class FakeMCP:
    def __init__(self):
        self.registry = {}

    def tool(self):
        def decorate(func):
            if func.__name__ in self.registry:
                raise AssertionError("Duplicate MCP tool")
            self.registry[func.__name__] = func
            return func
        return decorate


def _run(coro):
    return asyncio.run(coro)


def test_catalog_is_documentary_only_with_unique_exact_paths():
    assert len(FOOTBALL_PATHS) == 38
    assert len(ACCOUNT_PATHS) == 1
    assert len(ALLOWED_PATHS) == 39
    assert EVIDENCE == "PENDING_OFFICIAL_RECONCILIATION"
    assert all(documented_path(p) for p in FOOTBALL_PATHS + ACCOUNT_PATHS)
    for path in ("/", "/api/fixtures", "/fixtures/../../healthz", "/fixtures?live=all",
                 "https://127.0.0.1/", "/odds/live/bets/2", "", None):
        assert not documented_path(path)


def test_disabled_settings_ignore_other_invalid_api_variables_and_secrets():
    settings = Settings.from_env({
        "APIFOOTBALL_ENABLED": "false",
        "APIFOOTBALL_BASE_URL": "http://127.0.0.1/secret",
        "APIFOOTBALL_MAX_RPM": "not a number",
        "APIFOOTBALL_API_KEY": "FAKE_SECRET_DO_NOT_LOG",
    })
    assert settings.enabled is False
    assert settings.configured is False
    assert settings.api_key == ""
    assert settings.base_url == OFFICIAL_BASE_URL
    assert settings.max_rpm == 8
    assert "FAKE_SECRET_DO_NOT_LOG" not in repr(settings)
    assert "FAKE_SECRET_DO_NOT_LOG" not in str(settings)


@pytest.mark.parametrize("enabled", ["yes", "on", "garbage", ""])
def test_configuration_rejects_ambiguous_enable_flag(enabled):
    with pytest.raises(ConfigurationError, match="APIFOOTBALL_ENABLED"):
        Settings.from_env({"APIFOOTBALL_ENABLED": enabled})


def test_configured_values_are_bounded_and_secret_is_redacted():
    key = "DUMMY_FAKE_TOKEN_123"
    settings = Settings.from_env({
        "APIFOOTBALL_ENABLED": "1",
        "APIFOOTBALL_API_KEY": key,
        "APIFOOTBALL_TIMEOUT_SECONDS": "4.5",
        "APIFOOTBALL_MAX_RPM": "9",
        "APIFOOTBALL_MAX_DAILY_REQUESTS": "70",
        "APIFOOTBALL_MAX_CONCURRENCY": "2",
        "APIFOOTBALL_CACHE_ENABLED": "0",
        "APIFOOTBALL_CIRCUIT_BREAKER_ENABLED": "true",
    })
    assert settings.configured is True
    assert settings.timeout_seconds == 4.5
    assert settings.max_rpm == 9
    assert settings.max_daily_requests == 70
    assert settings.cache_enabled is False
    assert settings.circuit_breaker_enabled is True
    assert key not in repr(settings)
    assert key not in str(settings)


@pytest.mark.parametrize("changed", [
    {"APIFOOTBALL_BASE_URL": "http://v3.football.api-sports.io"},
    {"APIFOOTBALL_BASE_URL": "https://127.0.0.1"},
    {"APIFOOTBALL_BASE_URL": "https://v3.football.api-sports.io@evil.example"},
    {"APIFOOTBALL_BASE_URL": "https://v3.football.api-sports.io/path"},
    {"APIFOOTBALL_BASE_URL": "https://v3.football.api-sports.io?x=y"},
    {"APIFOOTBALL_BASE_URL": "https://v3.football.api-sports.io:444"},
    {"APIFOOTBALL_MAX_RPM": "0"},
    {"APIFOOTBALL_MAX_DAILY_REQUESTS": "-1"},
    {"APIFOOTBALL_MAX_CONCURRENCY": "99"},
    {"APIFOOTBALL_TIMEOUT_SECONDS": "9999"},
    {"APIFOOTBALL_TIMEOUT_SECONDS": "nan"},
    {"APIFOOTBALL_CACHE_ENABLED": "sometimes"},
])
def test_enabled_invalid_configuration_is_fail_closed(changed):
    with pytest.raises(ConfigurationError):
        Settings.from_env({"APIFOOTBALL_ENABLED": "true", **changed})


def test_mock_client_is_network_free_strict_and_lossless(monkeypatch):
    import httpx

    class NoHttp:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Mock must not open a socket")

    monkeypatch.setattr(httpx, "AsyncClient", NoHttp)
    client = MockFootballClient({
        "/fixtures/statistics": {
            "response": [
                {"shots": 0, "xg": None, "empty": "", "missing": {"other": 4}},
            ],
            "paging": {"current": 1, "total": 1},
        },
    })
    a = _run(client.get("/fixtures/statistics", {"fixture": 123}))
    assert a["response"][0]["shots"] == 0
    assert a["response"][0]["xg"] is None
    assert a["response"][0]["empty"] == ""
    assert "xgOT" not in a["response"][0]
    a["response"][0]["shots"] = 999
    b = _run(client.get("/fixtures/statistics", {"fixture": 123}))
    assert b["response"][0]["shots"] == 0
    assert client.calls == [
        ("/fixtures/statistics", {"fixture": 123}),
        ("/fixtures/statistics", {"fixture": 123}),
    ]


def test_mock_client_rejects_urls_unknown_endpoints_bad_params_and_unprovided_fixtures():
    c = MockFootballClient({"/status": {"results": 0, "response": {}}})
    for path in ("https://example.com", "/etc/passwd", "/fixtures?date=2026-01-01"):
        with pytest.raises(UnsupportedEndpoint):
            _run(c.get(path))
    for params in ({"invalid-key": 1}, {"fixture": object()}, {"fixture": True}):
        with pytest.raises(ValueError):
            _run(c.get("/status", params))
    with pytest.raises(ProviderNotAuthorized):
        _run(c.get("/odds/live"))
    assert not any("http" in str(x) for x in c.calls)


def test_real_client_is_intentionally_non_operational_even_with_key():
    c = RealFootballClient()
    with pytest.raises(ProviderNotAuthorized, match="not authorized"):
        _run(c.get("/fixtures"))
    with pytest.raises(UnsupportedEndpoint):
        _run(c.get("http://localhost:1234"))


def test_optional_tool_registry_is_off_without_flag():
    registry = FakeMCP()
    names = register_tools(registry, Settings.from_env({"APIFOOTBALL_ENABLED": "false"}))
    assert names == ()
    assert registry.registry == {}


def test_optional_health_tool_is_mock_only_no_secret_no_request():
    key = "DUMMY_FAKE_API_KEY_VALUE"
    registry = FakeMCP()
    settings = Settings.from_env({"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_API_KEY": key})
    assert register_tools(registry, settings) == ("apifootball_health",)
    assert list(registry.registry) == ["apifootball_health"]
    result = _run(registry.registry["apifootball_health"]())
    assert result["provider"] == "API-FOOTBALL"
    assert result["status"] == "MOCK_ONLY"
    assert result["provider_requests_authorized"] is False
    assert result["network_calls_made"] == 0
    assert result["http_transport"] == "NOT_IMPLEMENTED"
    assert result["limits_configured_not_enforced"]["per_day"] == 80
    assert result["observed_at_utc"].endswith("+00:00")
    assert key not in str(result)
    assert "PENDING_OFFICIAL_RECONCILIATION" == result["documented_contract"]


def test_optional_health_warns_if_enabled_without_api_key():
    registry = FakeMCP()
    settings = Settings.from_env({"APIFOOTBALL_ENABLED": "true"})
    register_tools(registry, settings)
    result = _run(registry.registry["apifootball_health"]())
    assert result["status"] == "KEY_NOT_CONFIGURED"
    assert result["key_present"] is False
    assert result["provider_requests_authorized"] is False


def test_original_browser_tools_are_unmodified_and_number_34():
    import bridge.server as server

    source = inspect.getsource(server)
    module = ast.parse(source)
    baseline = [
        node.name for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name.startswith("browser_")
        and any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr == "tool"
            for d in node.decorator_list
        )
    ]
    assert len(baseline) == 34
    assert len(set(baseline)) == 34
    for name in (
        "browser_health", "browser_create", "browser_create_cloak", "browser_open_url",
        "browser_get_html", "browser_get_dom", "browser_screenshot",
        "browser_get_network_responses", "browser_stop", "browser_reap",
        "browser_stop_all_idle", "browser_cleanup_report",
    ):
        assert name in baseline
    assert server.APIFOOTBALL_REGISTERED_TOOLS == ()


def test_server_disabled_never_imports_optional_module(monkeypatch):
    import bridge.server as server

    monkeypatch.setenv("APIFOOTBALL_ENABLED", "false")
    monkeypatch.setitem(sys.modules, "bridge.api_football.config", None)
    fake = FakeMCP()
    assert server._register_optional_api_football_tools(fake) == ()
    assert fake.registry == {}


def test_server_enabled_registers_isolated_health_tool(monkeypatch):
    import bridge.server as server

    monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    monkeypatch.delenv("APIFOOTBALL_API_KEY", raising=False)
    fake = FakeMCP()
    assert server._register_optional_api_football_tools(fake) == ("apifootball_health",)
    assert list(fake.registry) == ["apifootball_health"]


def test_server_enabled_with_invalid_config_does_not_crash_or_log_secret(monkeypatch, caplog):
    import bridge.server as server

    token = "TEST_SECRET_NEVER_LOG"
    monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    monkeypatch.setenv("APIFOOTBALL_BASE_URL", "http://127.0.0.1")
    monkeypatch.setenv("APIFOOTBALL_API_KEY", token)
    fake = FakeMCP()
    assert server._register_optional_api_football_tools(fake) == ()
    assert fake.registry == {}
    assert "optional_apifootball_registration_failed" in caplog.text
    assert token not in caplog.text


def test_server_optional_module_import_failure_keeps_browser(monkeypatch):
    import bridge.server as server

    monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    monkeypatch.setitem(sys.modules, "bridge.api_football.config", None)
    fake = FakeMCP()
    assert server._register_optional_api_football_tools(fake) == ()
    assert fake.registry == {}


def test_oauth_and_browser_transport_still_share_same_server_application():
    from bridge.server import app, mcp_app, transport_security
    from bridge import oauth

    assert any(getattr(r, "path", None) == "/mcp" for r in app.routes)
    assert any(getattr(r, "path", None) == "/oauth/token" for r in app.routes)
    assert "https://chatgpt.com" in transport_security.allowed_origins
    assert "https://claude.ai" in transport_security.allowed_origins
    assert oauth.authorization_server_metadata()["client_id_metadata_document_supported"]
    assert mcp_app is not None
