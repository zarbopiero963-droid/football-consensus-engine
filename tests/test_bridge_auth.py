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
