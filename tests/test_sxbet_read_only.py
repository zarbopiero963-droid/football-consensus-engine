"""SX Bet READ_ONLY module: offline tests (httpx.MockTransport, fake key, no network)."""
import asyncio
import inspect
import json

import httpx
import pytest

from bridge.sxbet import client as sx_client
from bridge.sxbet.client import NETWORKS, SXBetReadClient, Settings, _RateLimiter
from bridge.sxbet.tools import register_sxbet_tools

KEY = "FAKE_SX_TEST_KEY_DO_NOT_LOG"
H = "0x" + "ab" * 32


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            assert fn.__name__ not in self.tools
            self.tools[fn.__name__] = fn
            return fn
        return attach


def _tools(handler, *, key=KEY, network="mainnet", rpm=60):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    settings = Settings(enabled=True, network=network, api_key=key, max_rpm=rpm)
    client = SXBetReadClient(settings, transport=httpx.MockTransport(wrapped))
    registry = _Registry()
    register_sxbet_tools(registry, settings, client=client)
    return registry.tools, calls


def _ok(data, **headers):
    return lambda request: httpx.Response(200, json={"status": "success", "data": data}, headers=headers)


def run(coro):
    return asyncio.run(coro)


def test_disabled_by_default_registers_nothing():
    assert Settings.from_env({}).enabled is False
    assert register_sxbet_tools(_Registry(), Settings.from_env({})) == ()


def test_no_write_capability_exists():
    # Structural guarantee: the client exposes GET only and no tool can place/cancel/approve/transfer.
    public = [n for n, _ in inspect.getmembers(SXBetReadClient, inspect.isfunction) if not n.startswith("_")]
    assert public == ["get"]
    tools, _ = _tools(_ok({}))
    banned = ("create", "place", "post", "submit", "cancel", "approve", "transfer", "withdraw", "deposit", "sign", "fill_order", "heartbeat")
    assert not [n for n in tools if any(b in n for b in banned)]
    src = inspect.getsource(sx_client)
    assert '"POST"' not in src and '"DELETE"' not in src and "client.post" not in src and "client.delete" not in src
    health = run(tools["sxbet_health"]())
    assert health["mode"] == "READ_ONLY" and health["write_tools"] == [] and health["network_calls_made"] == 0
    assert health["live_trading"].startswith("UNAVAILABLE")


def test_public_route_pinned_host_no_key_header_and_comma_arrays():
    tools, calls = _tools(_ok({"markets": [], "nextKey": "abc"}))
    out = run(tools["sxbet_markets_active"](sport_ids=[5, 6], only_main_line=True, page_size=3))
    assert out["status"] == "REAL_API_RESPONSE" and out["next_key"] == "abc" and out["network_calls_made"] == 1
    req = calls[0]
    assert str(req.url).startswith(NETWORKS["mainnet"] + "/markets/active")
    assert req.method == "GET"
    assert dict(req.url.params) == {"sportIds": "5,6", "onlyMainLine": "true", "pageSize": "3"}
    assert "x-sx-api-key" not in req.headers


def test_auth_route_sends_key_header_only_there_and_key_never_in_output():
    tools, calls = _tools(_ok({"balance": "1"}))
    out = run(tools["sxbet_balance"]())
    assert calls[0].headers["x-sx-api-key"] == KEY
    assert KEY not in json.dumps(out)


def test_auth_route_without_key_makes_no_call():
    tools, calls = _tools(_ok({}), key="")
    out = run(tools["sxbet_positions"](status=["MATCHED", "LOCKED"]))
    assert out["status"] == "KEY_NOT_CONFIGURED" and out["network_calls_made"] == 0 and calls == []


def test_testnet_uses_toronto_host():
    tools, calls = _tools(_ok([]), network="testnet")
    run(tools["sxbet_sports"]())
    assert str(calls[0].url).startswith("https://api.toronto.sx.bet/sports")


@pytest.mark.parametrize("tool,kwargs,code", [
    ("sxbet_markets_find", {"market_hashes": ["0xabc"]}, "INVALID_MARKET_HASHES"),
    ("sxbet_markets_find", {"market_hashes": [H] * 31}, "INVALID_MARKET_HASHES_COUNT"),
    ("sxbet_markets_active", {"type": [52], "bet_group": "1X2"}, "TYPE_AND_BET_GROUP_EXCLUSIVE"),
    ("sxbet_markets_active", {"page_size": 101}, "INVALID_PAGE_SIZE"),
    ("sxbet_public_trades", {"per_page": 0}, "INVALID_PER_PAGE"),
    ("sxbet_positions", {"status": ["OPEN"]}, "INVALID_STATUS"),
    ("sxbet_positions", {"status": []}, "INVALID_STATUS"),
    ("sxbet_ledger", {"event_type": "TRANSFER"}, "INVALID_EVENT_TYPE"),
    ("sxbet_fixture_status", {"sportx_event_ids": ["L1/../x"]}, "INVALID_EVENT_ID"),
    ("sxbet_search", {"query": "a\nb"}, "INVALID_QUERY"),
    ("sxbet_teams", {"per_page": 501}, "INVALID_PER_PAGE"),
])
def test_invalid_input_rejected_before_network(tool, kwargs, code):
    tools, calls = _tools(_ok({}))
    out = run(tools[tool](**kwargs))
    assert out["status"] == "INVALID_INPUT" and out["error"]["code"] == code and calls == []


@pytest.mark.parametrize("status,headers,code,retry", [
    (429, {"retry-after": "7"}, "SXBET_RATE_LIMITED", 7),
    (401, {}, "SXBET_UNAUTHORIZED", None),
    (500, {}, "SXBET_HTTP_ERROR", None),
    (302, {"location": "https://evil.example/"}, "SXBET_REDIRECT_REJECTED", None),
])
def test_http_errors_are_fixed_codes_without_body(status, headers, code, retry):
    tools, calls = _tools(lambda r: httpx.Response(status, text="SECRET_PROVIDER_BODY " + KEY, headers=headers))
    out = run(tools["sxbet_balance"]())
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == code
    assert out["error"].get("retry_after_seconds") == retry
    assert "SECRET_PROVIDER_BODY" not in json.dumps(out) and KEY not in json.dumps(out)
    assert len(calls) == 1  # no automatic retry storm


@pytest.mark.parametrize("body", [b"not json", b'{"status":"failure","data":{}}', b'{"data":{}}', b"[]"])
def test_non_success_or_invalid_json_fails_closed(body):
    tools, _ = _tools(lambda r: httpx.Response(200, content=body))
    out = run(tools["sxbet_sports"]())
    assert out["status"] == "PROVIDER_ERROR" and out["data"] is None


def test_oversized_body_and_oversized_mcp_reply_never_truncated():
    tools, _ = _tools(lambda r: httpx.Response(200, content=b'{"status":"success","data":"' + b"x" * 600_000 + b'"}'))
    out = run(tools["sxbet_sports"]())
    assert out["error"]["code"] == "SXBET_RESPONSE_TOO_LARGE" and out["data"] is None
    tools, _ = _tools(_ok({"markets": [{"n": "y" * 1000} for _ in range(300)], "nextKey": "k"}))
    out = run(tools["sxbet_markets_active"]())
    assert out["status"] == "PAYLOAD_TOO_LARGE" and out["data"] is None and "page_size" in out["error"]["hint"]


def test_local_rate_limiter_blocks_without_network():
    t = [0.0]
    limiter = _RateLimiter(2, clock=lambda: t[0])
    calls = []
    settings = Settings(enabled=True, api_key=KEY, max_rpm=2)
    client = SXBetReadClient(settings, transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200, json={"status": "success", "data": []}))[1]),
                             limiter=limiter)
    registry = _Registry()
    register_sxbet_tools(registry, settings, client=client)
    run(registry.tools["sxbet_sports"]())
    run(registry.tools["sxbet_sports"]())
    out = run(registry.tools["sxbet_sports"]())
    assert out["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED" and out["network_calls_made"] == 0 and len(calls) == 2
    t[0] = 61.0
    assert run(registry.tools["sxbet_sports"]())["status"] == "REAL_API_RESPONSE"


def test_settings_reject_unknown_network_and_hide_key():
    with pytest.raises(ValueError):
        Settings.from_env({"SXBET_NETWORK": "https://evil.example"})
    assert KEY not in repr(Settings(enabled=True, api_key=KEY))
