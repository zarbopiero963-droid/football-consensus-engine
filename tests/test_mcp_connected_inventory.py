import json
from pathlib import Path
from bridge.api_football.contracts import ENDPOINTS

SNAPSHOT=Path("evidence/mcp/connected_tools_2026-10-25.json")

def test_actual_connected_mcp_tool_inventory_42_plus_34():
    evidence=json.loads(SNAPSHOT.read_text())
    api=evidence["api_tools"]
    browser=evidence["browser_tools"]
    assert len(api)==42 and len(set(api))==42
    assert len(browser)==34 and len(set(browser))==34
    assert all(t.startswith("apifootball_") for t in api)
    assert all(t.startswith("browser_") for t in browser)
    # 40 typed provider endpoint contracts plus 2 read-only diagnostics.
    assert set(ENDPOINTS) <= set(api)
    assert len(set(api)-set(ENDPOINTS))==2
    assert evidence["deployed_baseline"]=="ee4cabd1cde5a15256651f0a4db6af814cc1bf15"
