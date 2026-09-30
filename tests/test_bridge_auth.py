from starlette.testclient import TestClient

from bridge.server import app


def test_healthz_reports_configuration(monkeypatch):
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    monkeypatch.delenv("MCP_OAUTH_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "browser_use_configured": False,
        "oauth_password_configured": False,
        "database_configured": False,
    }


def test_mcp_requires_bearer_token():
    client = TestClient(app)
    response = client.post("/mcp")
    assert response.status_code == 401
