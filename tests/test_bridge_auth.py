import pytest
from starlette.testclient import TestClient

from bridge.server import app


def test_healthz_reports_configuration(monkeypatch):
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    monkeypatch.delenv("MCP_OAUTH_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert all(data[key] is value for key, value in {
        "ok": True,
        "browser_use_configured": False,
        "oauth_password_configured": False,
        "database_configured": False,
    }.items())
    assert isinstance(data["apifootball_enabled"], bool)
    assert isinstance(data["apifootball_key_configured"], bool)
    assert isinstance(data["apifootball_live_authorized"], bool)
    assert isinstance(data["apifootball_registered_count"], int)
    assert data["apifootball_registration_error_type"] is None or isinstance(data["apifootball_registration_error_type"], str)


def test_mcp_requires_bearer_token():
    client = TestClient(app)
    response = client.post("/mcp")
    assert response.status_code == 401


def test_legacy_internal_pinnacle_routes_are_removed(monkeypatch):
    """Collector-only routes were removed with the legacy collector (roadmap #130 M3)."""
    monkeypatch.setenv("COLLECTOR_BRIDGE_TOKEN", "secret-test-token")
    client = TestClient(app)
    for path in ("/internal/pinnacle/leagues", "/internal/pinnacle/league-batch"):
        response = client.get(path, headers={"Authorization": "Bearer secret-test-token"})
        assert response.status_code == 404


@pytest.mark.parametrize("value,expected", [
    # Audit F-03 (intentional behaviour change): the kill switch is FAIL-CLOSED. Only an explicit
    # 1/true/yes/on, in any case and with surrounding spaces, enables the browser tools.
    ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True), (" On ", True), ("Yes", True),
    # Missing, empty or anything else is OFF (it used to be ON: unset, "disabled", "false;" ...).
    (None, False), ("", False), ("   ", False), ("false", False), ("0", False), ("OFF", False),
    (" no ", False), ("disabled", False), ("none", False), ("false;", False), ("true;", False),
    ("enabled", False), ("2", False), ("y", False), ("tru", False), ("true false", False)])
def test_healthz_reports_browser_kill_switch(monkeypatch, value, expected):
    """WP-002: BROWSER_TOOLS_ENABLED is verifiable in production via /healthz (fail-closed, F-03)."""
    if value is None:
        monkeypatch.delenv("BROWSER_TOOLS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BROWSER_TOOLS_ENABLED", value)
    data = TestClient(app).get("/healthz").json()
    assert data["browser_tools_enabled"] is expected


@pytest.mark.parametrize("value", [None, "", "disabled", "none", "false;", "enabled"])
def test_kill_switch_fails_closed_on_every_entry_point(monkeypatch, value):
    """Audit F-03 regression (RED on main: unset or an unknown value left the tools ON and
    create() reached the provider). Missing or unknown keeps every entry point closed, before
    any provider call, for both providers; CLOAKBROWSER_ENABLED cannot reopen it."""
    import asyncio

    import httpx

    from bridge import browser_use
    if value is None:
        monkeypatch.delenv("BROWSER_TOOLS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BROWSER_TOOLS_ENABLED", value)
    monkeypatch.setenv("BROWSER_USE_API_KEY", "fake-test-key")
    monkeypatch.setenv("CLOAKBROWSER_ENABLED", "true")
    calls = []
    real = httpx.AsyncClient
    monkeypatch.setattr(browser_use.httpx, "AsyncClient", lambda *a, **k: real(
        *a, transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)), **k))
    bridge = browser_use.BrowserUseBridge()
    for coro in (bridge.create(), bridge.create_cloak(), bridge.open_url("any", "https://example.com/"),
                 bridge.html("any"), bridge.click("any", "a"), bridge.wait("any", 1)):
        with pytest.raises(RuntimeError) as err:
            asyncio.run(coro)
        assert str(err.value) == browser_use.TOOLS_DISABLED
    assert calls == []
    data = TestClient(app).get("/healthz").json()
    # The two flags stay independent: Cloak's own flag is still reported as set.
    assert data["browser_tools_enabled"] is False and data["cloak_enabled"] is True


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("true", True)])
def test_healthz_reports_cloak_flag(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CLOAKBROWSER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CLOAKBROWSER_ENABLED", value)
    assert TestClient(app).get("/healthz").json()["cloak_enabled"] is expected
