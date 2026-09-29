from starlette.testclient import TestClient

from bridge.server import app


def test_healthz_reports_configuration(monkeypatch):
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    monkeypatch.setenv("MCP_BRIDGE_TOKEN", "secret")
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "browser_use_configured": False,
        "auth_configured": True,
    }


def test_mcp_requires_bearer_token(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_TOKEN", "secret")
    client = TestClient(app)
    response = client.post("/mcp")
    assert response.status_code == 401
