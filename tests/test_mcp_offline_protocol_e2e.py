"""Offline MCP protocol E2E: HTTP → OAuth middleware → MCP → API-FOOTBALL tool.

Runs the real Starlette app in a subprocess (module-level tool registration
depends on env at import). No network: Starlette TestClient is in-process
ASGI; Cloak/Browser Use runtime is stubbed; no API key; a fixed test token is
accepted by a stubbed validator. This is OFFLINE TESTED only — it does NOT
certify the ChatGPT/Claude.ai connector path (see
docs/api_football/mcp_client_certification_runbook.md).
"""
import json
import os
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(r'''
    import asyncio, json, os
    import bridge.server as srv
    from starlette.testclient import TestClient

    async def noop(*a, **k):
        return None

    async def idle():
        await asyncio.Event().wait()

    async def stop_all():
        return {"failed": []}

    srv.bridge.prepare_cloak_runtime = noop
    srv.bridge.reaper_loop = idle
    srv.bridge.stop_all = stop_all
    srv.oauth_store.valid_access_token = lambda token, resource, scope: token == "TEST_TOKEN_ONLY"
    host = srv.public_host
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Host": host}
    auth = {**base, "Authorization": "Bearer TEST_TOKEN_ONLY"}
    out = {}

    def rpc(c, i, method, params):
        r = c.post("/mcp/", headers=auth, json={"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        return r.status_code, r.json()

    with TestClient(srv.app, base_url="https://" + host) as c:
        out["no_token"] = c.post("/mcp/", headers=base, json={}).status_code
        out["bad_token"] = c.post("/mcp/", headers={**base, "Authorization": "Bearer nope"}, json={}).status_code
        out["www_authenticate"] = c.post("/mcp/", headers=base, json={}).headers.get("www-authenticate", "")
        out["initialize"] = rpc(c, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                    "clientInfo": {"name": "offline-test", "version": "0"}})
        out["tools_list"] = rpc(c, 2, "tools/list", {})
        out["call_ok"] = rpc(c, 3, "tools/call", {"name": "apifootball_fixtures",
                                                 "arguments": {"league": 39, "season": 2026, "from_date": "2026-11-01", "to_date": "2026-11-02"}})
        out["call_bad_type"] = rpc(c, 4, "tools/call", {"name": "apifootball_fixtures", "arguments": {"league": "x"}})
        out["call_contract_reject"] = rpc(c, 5, "tools/call", {"name": "apifootball_odds", "arguments": {"page": 2}})
        out["health"] = rpc(c, 6, "tools/call", {"name": "apifootball_health", "arguments": {}})
    print("RESULT_JSON=" + json.dumps(out))
''')


def _run(extra_env):
    env = {k: v for k, v in os.environ.items() if not k.startswith("APIFOOTBALL_")}
    env.pop("DATABASE_URL", None)
    env.update({"PYTHONPATH": os.getcwd(), **extra_env})
    proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(line[len("RESULT_JSON="):]), proc.stdout + proc.stderr


def _payload(result):
    status, body = result
    assert status == 200
    return body["result"]


def test_protocol_auth_discovery_and_calls_with_api_football_enabled():
    out, logs = _run({"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "false"})
    assert out["no_token"] == 401 and out["bad_token"] == 401
    assert "resource_metadata=" in out["www_authenticate"]
    init = _payload(out["initialize"])
    assert init["capabilities"]["tools"] is not None
    tools = _payload(out["tools_list"])["tools"]
    names = {t["name"] for t in tools}
    af = {n for n in names if n.startswith("apifootball_")}
    assert len(af) == 42
    from bridge.api_football.contracts import ENDPOINTS
    by_name = {t["name"]: t for t in tools}
    for name, contract in ENDPOINTS.items():
        exposed = set(by_name[name]["inputSchema"].get("properties", {}))
        expected = {"from_date" if f == "from" else "to_date" if f == "to" else f for f in contract.fields}
        assert exposed == expected, name
    ok = json.loads(_payload(out["call_ok"])["content"][0]["text"])
    assert ok["status"] == "KEY_NOT_CONFIGURED" and ok["network_calls_made"] == 0
    assert ok["parameters"]["from"] == "2026-11-01" and ok["parameters"]["to"] == "2026-11-02"
    bad = _payload(out["call_bad_type"])
    assert bad.get("isError") is True
    rejected = json.loads(_payload(out["call_contract_reject"])["content"][0]["text"])
    assert rejected["status"] == "INVALID_INPUT" and rejected["network_calls_made"] == 0
    health = json.loads(_payload(out["health"])["content"][0]["text"])
    assert health["provider_requests_authorized"] is False and health["network_calls_made"] == 0
    assert "TEST_TOKEN_ONLY" not in logs


def test_api_football_off_registers_no_provider_tools():
    out, _ = _run({"APIFOOTBALL_ENABLED": "false"})
    names = {t["name"] for t in _payload(out["tools_list"])["tools"]}
    assert not any(n.startswith("apifootball_") for n in names)
    assert len(names) > 0  # browser tools unaffected
