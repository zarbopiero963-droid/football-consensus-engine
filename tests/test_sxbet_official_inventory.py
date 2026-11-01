"""SX Bet MCP coverage reconciled against the official OpenAPI inventory
(docs/sxbet/official_api_inventory.json, from https://docs.sx.bet/openapi.json)."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bridge.sxbet.client import SXBetReadClient, Settings
from bridge.sxbet.tools import register_sxbet_tools

INV = json.loads((Path(__file__).resolve().parents[1] / "docs/sxbet/official_api_inventory.json").read_text(encoding="utf-8"))
KEY = "FAKE_SX_TEST_KEY_DO_NOT_LOG"
H = "0x" + "cd" * 32
A = "0x" + "ab" * 20


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


def _tools():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "success", "data": {}})

    s = Settings(enabled=True, api_key=KEY)
    reg = _Registry()
    register_sxbet_tools(reg, s, client=SXBetReadClient(s, transport=httpx.MockTransport(handler)))
    return reg.tools, calls


def test_inventory_counts_and_classification():
    ops = INV["operations"]
    assert INV["operation_count"] == len(ops) == 57
    gets = [o for o in ops if o["method"] == "GET"]
    writes = [o for o in ops if o["method"] != "GET"]
    assert len(gets) == 36 and len(writes) == 21
    assert all(o["classification"] in ("PUBLIC_READ", "AUTHENTICATED_READ") for o in gets)
    assert all(o["classification"] in ("STATE_MUTATION", "REAL_FUNDS_RISK") for o in writes)
    assert {o["path"] for o in writes if o["classification"] == "REAL_FUNDS_RISK"} >= {"/orders-v3", "/user/transfer-to-proxy"}


def test_every_official_get_is_exposed_and_no_write_is():
    tools, _ = _tools()
    ops = INV["operations"]
    for o in ops:
        if o["method"] == "GET":
            assert o["mcp"] == "EXPOSED" and o["mcp_tool"] in tools, o["path"]
        else:
            assert o["mcp_tool"] is None and o["mcp"].startswith("NOT_EXPOSED"), o["path"]
    mapped = {o["mcp_tool"] for o in ops if o["mcp_tool"]}
    assert set(tools) - mapped == {"sxbet_health"}  # the only tool with no provider route
    assert len(tools) == 36


@pytest.mark.parametrize("tool,kwargs,path,params", [
    ("sxbet_order", {"order_id": H}, f"/orders-v3/{H}", {}),
    ("sxbet_order_by_client_id", {"client_order_id": "my-order_1"}, "/orders-v3/client/my-order_1", {}),
    ("sxbet_trade", {"trade_id": "abc123"}, "/trades-v3/abc123", {}),
    ("sxbet_public_market_orders", {"market_hash": H, "per_page": 5}, "/orders-v3/public", {"marketHash": H, "perPage": "5"}),
    ("sxbet_public_user_orders", {"user_address": A}, "/orders-v3/public/user", {"userAddress": A}),
    ("sxbet_public_positions", {"user_address": A, "status": ["MATCHED", "LOCKED"], "sort_by": "maxLoss"},
     "/positions-v3/public", {"userAddress": A, "status": "MATCHED,LOCKED", "sortBy": "maxLoss"}),
    ("sxbet_privacy", {}, "/user/privacy-v3", {}),
    ("sxbet_proxy_wallet", {}, "/user/proxy", {}),
    ("sxbet_proxy_deploy_pending", {}, "/user/pending-deploy-proxy", {}),
    ("sxbet_proxy_funding_pending", {}, "/user/transfer-to-proxy/pending", {}),
    ("sxbet_proxy_funding_status", {"session_id": "0x" + "ef" * 32}, "/user/transfer-to-proxy/status", {"sessionId": "0x" + "ef" * 32}),
    ("sxbet_proxy_address_check", {"proxy_address": A}, f"/user/multisig/proxy/{A}", {}),
    ("sxbet_safe_info", {"safe_address": A}, f"/user/multisig/{A}/info", {}),
])
def test_new_read_routes_send_one_authenticated_get(tool, kwargs, path, params):
    tools, calls = _tools()
    out = asyncio.run(tools[tool](**kwargs))
    assert out["status"] == "REAL_API_RESPONSE" and out["mode"] == "READ_ONLY"
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].url.path == path
    assert dict(calls[0].url.params) == params and calls[0].headers["x-sx-api-key"] == KEY
    assert KEY not in json.dumps(out)


@pytest.mark.parametrize("tool,kwargs,code", [
    ("sxbet_order", {"order_id": "../user/balance-v3"}, "INVALID_ORDER_ID"),
    ("sxbet_order_by_client_id", {"client_order_id": "a/b"}, "INVALID_CLIENT_ORDER_ID"),
    ("sxbet_order_by_client_id", {"client_order_id": "x" * 65}, "INVALID_CLIENT_ORDER_ID"),
    ("sxbet_trade", {"trade_id": "..%2f"}, "INVALID_TRADE_ID"),
    ("sxbet_public_user_orders", {"user_address": "0x123"}, "INVALID_USER_ADDRESS"),
    ("sxbet_public_positions", {"user_address": A, "status": ["OPEN"]}, "INVALID_STATUS"),
    ("sxbet_public_positions", {"user_address": A, "status": ["MATCHED"], "sort_by": "pnl"}, "INVALID_SORT_BY"),
    ("sxbet_proxy_funding_status", {"session_id": "a b"}, "INVALID_SESSION_ID"),
    ("sxbet_proxy_address_check", {"proxy_address": "0x" + "zz" * 20}, "INVALID_PROXY_ADDRESS"),
    ("sxbet_safe_info", {"safe_address": A + "00"}, "INVALID_SAFE_ADDRESS"),
])
def test_invalid_path_and_query_inputs_never_reach_the_network(tool, kwargs, code):
    tools, calls = _tools()
    out = asyncio.run(tools[tool](**kwargs))
    assert out["status"] == "INVALID_INPUT" and out["error"]["code"] == code and calls == []
