"""Non-regression of the existing browser MCP surface and isolation of API-FOOTBALL."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp.server import MCPServer
from starlette.testclient import TestClient

from bridge import oauth, oauth_store, server
from bridge.api_football import registry, tools
from conftest import FAKE_KEY

BASELINE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "mcp_browser_tools_baseline.json")
                      .read_text(encoding="utf-8"))
APIFOOTBALL_TOOLS = {e.tool_name for e in registry.implemented()} | {
    "apifootball_health", "apifootball_quota_report", "apifootball_endpoint_registry"}
PUBLIC = "https://football-consensus-browser-bridge.onrender.com"


def listed():
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def test_browser_tools_are_byte_identical_to_main_baseline():
    tools_now = listed()
    assert BASELINE["tool_count"] == 34
    for name, expected in BASELINE["tools"].items():
        assert name in tools_now, f"browser tool removed: {name}"
        tool = tools_now[name]
        assert tool.description == expected["description"], name
        assert tool.input_schema == expected["input_schema"], name
        assert tool.output_schema == expected["output_schema"], name


def test_tool_surface_is_browser_baseline_plus_namespaced_apifootball_only():
    names = set(listed())
    assert names == set(BASELINE["tools"]) | APIFOOTBALL_TOOLS
    assert all(name.startswith("browser_") for name in BASELINE["tools"])
    assert all(name.startswith("apifootball_") for name in names - set(BASELINE["tools"]))


def test_server_instructions_and_oauth_contract_unchanged():
    assert server.mcp.instructions.startswith("Browser Use is infrastructure only.")
    assert oauth.SCOPE == "browser:use"
    metadata = oauth.protected_resource_metadata()
    assert metadata["scopes_supported"] == ["browser:use", "offline_access"]


def test_import_failure_leaves_browser_tools_intact(monkeypatch):
    fresh = MCPServer("isolation-test")
    fresh.add_tool(server.browser_health)
    monkeypatch.setitem(sys.modules, "bridge.api_football.tools", None)
    assert server._register_optional_api_football(fresh) is False
    assert [t.name for t in asyncio.run(fresh.list_tools())] == ["browser_health"]


def test_corrupt_registry_leaves_browser_tools_intact(monkeypatch, tmp_path):
    def corrupt():
        return registry.implemented.__wrapped__(tmp_path)  # empty directory: registry files missing

    fresh = MCPServer("isolation-test")
    fresh.add_tool(server.browser_health)
    monkeypatch.setattr(registry, "implemented", corrupt)
    assert server._register_optional_api_football(fresh) is False
    assert [t.name for t in asyncio.run(fresh.list_tools())] == ["browser_health"]


def test_healthz_contract_is_unchanged(monkeypatch):
    for name in ("BROWSER_USE_API_KEY", "MCP_OAUTH_PASSWORD", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    monkeypatch.setenv("APIFOOTBALL_API_KEY", FAKE_KEY)
    response = TestClient(server.app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "browser_use_configured": False,
                               "oauth_password_configured": False, "database_configured": False}


def test_mcp_still_requires_bearer_token():
    assert TestClient(server.app).post("/mcp").status_code == 401


def _rpc(client, method, params, request_id):
    response = client.post("/mcp/", headers={
        "Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18",
    }, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="module")
def http_client():
    # The MCP session manager can run once per process: one lifespan for all HTTP tests.
    with pytest.MonkeyPatch.context() as patch:
        for name in ("APIFOOTBALL_ENABLED", "APIFOOTBALL_API_KEY", "DATABASE_URL"):
            patch.delenv(name, raising=False)
        patch.setattr(oauth_store, "valid_access_token", lambda token, resource, scope: token == "test-token")
        with TestClient(server.app, base_url=PUBLIC) as client:
            yield client


def test_http_discovery_through_oauth_lists_every_tool(http_client):
    init = _rpc(http_client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "compat-test", "version": "1"}}, 1)
    assert init["result"]["serverInfo"]["name"] == "football-consensus-browser-bridge"
    listing = _rpc(http_client, "tools/list", {}, 2)["result"]["tools"]
    names = {tool["name"] for tool in listing}
    assert names == set(BASELINE["tools"]) | APIFOOTBALL_TOOLS
    for tool in listing:
        assert tool["inputSchema"]["type"] == "object"
        assert tool.get("description")
    assert FAKE_KEY not in json.dumps(listing)


def test_http_call_with_module_disabled_is_structured_and_offline(http_client, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("no provider call may happen while disabled")

    monkeypatch.setattr(tools, "_client", None)
    monkeypatch.setattr("bridge.api_football.client.ApiFootballClient._attempt", forbidden)
    _rpc(http_client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "compat-test", "version": "1"}}, 1)
    result = _rpc(http_client, "tools/call", {"name": "apifootball_fixtures", "arguments": {"id": 1}}, 3)["result"]
    payload = json.loads(result["content"][0]["text"])
    assert result.get("isError") in (None, False)
    assert payload["status"] == "DISABLED" and payload["ok"] is False
    # Browser health stays callable in the same process after the API-FOOTBALL call.
    browser = _rpc(http_client, "tools/call", {"name": "browser_health", "arguments": {}}, 4)["result"]
    assert json.loads(browser["content"][0]["text"])["providers"]["browser_use"] == {"configured": False}


def test_registry_tool_reports_coverage_offline(http_client):
    _rpc(http_client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "compat-test", "version": "1"}}, 1)
    result = _rpc(http_client, "tools/call", {"name": "apifootball_endpoint_registry", "arguments": {}}, 5)["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["dashboard"]["endpoints_catalogued"]["value"] == 39
    assert len(payload["endpoints"]) == 39
