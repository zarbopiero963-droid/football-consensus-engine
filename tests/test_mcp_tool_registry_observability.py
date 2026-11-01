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
    assert len(sx) == 36 and len(names) == len(sx) + len(af) + len(browser)
    h = out["healthz"]
    assert h["mcp_tools_total"] == len(names)
    assert h["mcp_tools_by_component"] == {"browser": len(browser), "apifootball": len(af), "sxbet": 36, "other": 0}
    assert h["deploy_commit"] == "0123456789ab"
    lines = _registry_lines(logs)
    assert lines and f"total={len(names)}" in lines[0] and "sxbet=36" in lines[0] and "commit=0123456789ab" in lines[0]
    assert FAKE_KEY not in logs and FAKE_KEY not in json.dumps(out)


def test_sxbet_disabled_catalogue_reports_zero_sxbet_and_unknown_commit():
    out, logs = _run({"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "false"})
    h = out["healthz"]
    assert not [n for n in out["tool_names"] if n.startswith("sxbet_")]
    assert h["mcp_tools_by_component"]["sxbet"] == 0 and h["mcp_tools_total"] == len(out["tool_names"])
    assert h["deploy_commit"] is None
    assert "sxbet=0" in _registry_lines(logs)[0]


BROWSER_TOOLS = sorted("""browser_back browser_cleanup_report browser_click browser_close_page browser_create
browser_create_cloak browser_fill browser_focus browser_forward browser_get_dom browser_get_elements browser_get_html
browser_get_network_responses browser_health browser_hover browser_list_pages browser_list_sessions browser_new_page
browser_open_url browser_press browser_reap browser_reload browser_screenshot browser_scroll browser_select_option
browser_session_status browser_set_checked browser_stop browser_stop_all browser_stop_all_idle browser_switch_page
browser_type browser_wait browser_wait_for_selector""".split())
SXBET_TOOLS = sorted("""sxbet_balance sxbet_best_odds sxbet_fees sxbet_fills sxbet_fixture_status
sxbet_fixtures_active sxbet_health sxbet_leagues sxbet_ledger sxbet_live_scores sxbet_markets_active
sxbet_markets_find sxbet_markets_popular sxbet_metadata sxbet_open_orders sxbet_orderbook_event
sxbet_orderbook_market sxbet_positions sxbet_public_trades sxbet_search sxbet_sports sxbet_teams
sxbet_trades sxbet_order sxbet_order_by_client_id sxbet_trade sxbet_public_market_orders sxbet_public_user_orders
sxbet_public_positions sxbet_privacy sxbet_proxy_wallet sxbet_proxy_deploy_pending sxbet_proxy_funding_pending
sxbet_proxy_funding_status sxbet_proxy_address_check sxbet_safe_info""".split())


def test_browser_and_sxbet_catalogues_are_pinned():
    """API-FOOTBALL work must never add, drop or rename browser/SX Bet tools by accident."""
    out, _ = _run({"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "false",
                   "SXBET_ENABLED": "true", "SXBET_API_KEY": FAKE_KEY})
    names = out["tool_names"]
    assert sorted(n for n in names if n.startswith("browser_")) == BROWSER_TOOLS and len(BROWSER_TOOLS) == 34
    assert sorted(n for n in names if n.startswith("sxbet_")) == SXBET_TOOLS and len(SXBET_TOOLS) == 36
    assert len([n for n in names if n.startswith("apifootball_")]) == 42
