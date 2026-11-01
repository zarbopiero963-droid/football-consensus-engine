"""Tool-registry observability (roadmap #130, SX Bet ChatGPT discovery P0).

A client reported 76 tools (= 34 browser + 42 API-FOOTBALL, no sxbet_*) while
/healthz later showed 23 SX Bet tools. Without a per-process record of the
published catalogue, nobody could prove which catalogue a given instance served.
This pins: /healthz reports the real tools/list total per component and the
deployed commit, the startup log records the same counts, and both agree with
the MCP tools/list payload. No secret is ever reflected.
"""
import json
import os
import subprocess
import sys
import textwrap

FAKE_KEY = "FAKE_SX_KEY_FOR_REGISTRY_TEST_ONLY"

SCRIPT = textwrap.dedent(r'''
    import asyncio, json, logging
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
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
    auth = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "Host": host, "Authorization": "Bearer TEST_TOKEN_ONLY"}
    out = {}
    with TestClient(srv.app, base_url="https://" + host) as c:
        out["healthz"] = c.get("/healthz", headers={"Host": host}).json()
        r = c.post("/mcp/", headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        out["tool_names"] = [t["name"] for t in r.json()["result"]["tools"]]
    print("RESULT_JSON=" + json.dumps(out))
''')


def _run(extra_env):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("APIFOOTBALL_", "SXBET_", "RENDER_GIT_COMMIT"))}
    env.pop("DATABASE_URL", None)
    env.update({"PYTHONPATH": os.getcwd(), **extra_env})
    proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(line[len("RESULT_JSON="):]), proc.stdout + proc.stderr


def _registry_lines(logs):
    return [l for l in logs.splitlines() if "MCP_TOOL_REGISTRY" in l]


def test_sxbet_enabled_catalogue_is_reported_by_healthz_and_startup_log():
    out, logs = _run({"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "false",
                      "SXBET_ENABLED": "true", "SXBET_API_KEY": FAKE_KEY,
                      "RENDER_GIT_COMMIT": "0123456789abcdef0123456789abcdef01234567"})
    names = out["tool_names"]
    sx = [n for n in names if n.startswith("sxbet_")]
    af = [n for n in names if n.startswith("apifootball_")]
    browser = [n for n in names if n.startswith("browser_")]
    assert len(sx) == 24 and len(names) == len(sx) + len(af) + len(browser)
    h = out["healthz"]
    assert h["mcp_tools_total"] == len(names)
    assert h["mcp_tools_by_component"] == {"browser": len(browser), "apifootball": len(af), "sxbet": 24, "other": 0}
    assert h["deploy_commit"] == "0123456789ab"
    lines = _registry_lines(logs)
    assert lines and f"total={len(names)}" in lines[0] and "sxbet=24" in lines[0] and "commit=0123456789ab" in lines[0]
    assert FAKE_KEY not in logs and FAKE_KEY not in json.dumps(out)


def test_sxbet_disabled_catalogue_reports_zero_sxbet_and_unknown_commit():
    out, logs = _run({"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "false"})
    h = out["healthz"]
    assert not [n for n in out["tool_names"] if n.startswith("sxbet_")]
    assert h["mcp_tools_by_component"]["sxbet"] == 0 and h["mcp_tools_total"] == len(out["tool_names"])
    assert h["deploy_commit"] is None
    assert "sxbet=0" in _registry_lines(logs)[0]
