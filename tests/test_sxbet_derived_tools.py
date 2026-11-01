"""SX Bet DERIVED MCP tools: GET-only reads composed with positions/hedging/rules (httpx.MockTransport)."""
import asyncio
import inspect
import json
import re

import httpx
import pytest

from bridge.sxbet import tools as sx_tools
from bridge.sxbet.client import SXBetReadClient, Settings
from bridge.sxbet.tools import register_sxbet_tools

KEY = "FAKE_SX_TEST_KEY_DO_NOT_LOG"
H = "0x" + "ab" * 32
OID = "0x" + "b1" * 32
TID = "0x" + "a1" * 32
ESCROW, TOKEN = "0x" + "0d" * 20, "0x" + "1b" * 20
P = 10 ** 18
META = {
    "chainId": 4162,
    "domain": {"name": "OBv3 Escrow", "version": "1", "chainId": 4162, "verifyingContract": ESCROW},
    "activeAsset": {"symbol": "USDC", "baseToken": TOKEN, "escrowAddress": ESCROW, "decimals": 6},
    "oddsLadderStepSize": 125,
    "limits": {"orderSizeMinimumBaseUnits": "1000000", "minRestingOrderSizeBaseUnits": "100000",
               "maxCreateOrders": 10, "maxCancelOrders": 100},
    "bettingDelay": {"liveMsDefault": 8000, "pregameMsDefault": 0},
}
FEES = {"refundFee": None, "makerPayoutFee": "0.01", "takerPayoutFee": "0.02",
        "makerParlayPayoutFee": None, "takerParlayPayoutFee": None}
BET = {"tradeId": TID, "marketHash": H, "isBettingOutcomeOne": True, "isMaker": False, "isParlay": False,
       "totalStake": "2000000", "totalReturn": "5000000", "netTotalReturn": "4940000",
       "weightedAverageOdds": str(40 * P), "ceRefundAmount": "0", "ceRefundFeeAmount": "0", "fillCount": 1,
       "ownOrderIds": [OID], "status": "LOCKED", "updatedAt": "2026-07-31T18:47:12.837Z", "settlement": None}
ORDER = {"id": OID, "marketHash": H, "isBettingOutcomeOne": False, "percentageOdds": str(30 * P),
         "totalBetSize": "3000000", "remainingSize": "1000000", "status": "ACTIVE", "inactiveReason": None,
         "eventId": "L1", "createdAt": "2026-07-31T18:45:07.437Z", "updatedAt": "2026-07-31T18:46:07.456Z"}
FILL = {"id": f"0x{'e1' * 32}:{OID}:{H}", "matchId": "0x" + "e1" * 32, "orderId": OID, "tradeId": TID,
        "marketHash": H, "quarterlineMarketHash": None, "isParlay": False, "fillAmount": "2000000",
        "fillOdds": str(40 * P), "returnAmount": "5000000", "netReturnAmount": "4940000", "isBettingOutcomeOne": False,
        "isMaker": True, "status": "LOCKED", "ceRefundAmount": "0", "ceRefundFeeAmount": "0",
        "updatedAt": "2026-07-31T18:47:12.837Z", "settlement": None}
POSITION = {"marketHash": H, "totalStake": "2000000", "maxWin": "3000000", "netMaxWin": "2940000",
            "maxLoss": "-2000000", "isOutcomeOneMaxWin": True, "pnl": None, "tradeCount": 1,
            "market": {"status": "ACTIVE", "marketHash": H, "sportXeventId": "L1", "outcomeOneName": "A",
                       "outcomeTwoName": "B", "gameTime": 4_000_000_000}}
# Makers resting on outcome one at 60% (size 30 USDC) offer outcome two to a taker at 40% (capacity 20 USDC).
BOOK = {"marketHash": H, "outcomeOne": [{"percentageOdds": str(60 * P), "size": "30000000"}],
        "outcomeTwo": [{"percentageOdds": str(50 * P), "size": "4000000"}], "version": "1"}


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            assert fn.__name__ not in self.tools
            self.tools[fn.__name__] = fn
            return fn
        return attach


def router(*, market_status="ACTIVE", trades_rows=None, order_404=False, overrides=None):
    overrides = overrides or {}

    def handler(request):
        path, q = request.url.path, request.url.params
        if path in overrides:
            return overrides[path](request)
        if path == "/markets/find":
            data = [{"marketHash": H, "status": market_status, "outcomeOneName": "A", "outcomeTwoName": "B",
                     "sportXeventId": "L1", "gameTime": 4_000_000_000}]
        elif path == "/metadata/obv3":
            data = META
        elif path == "/trades-v3":
            rows = trades_rows if trades_rows is not None else [BET]
            data = {"trades": [r for r in rows if r["status"] == q.get("status")]}
        elif path == "/orders-v3":
            data = {"orders": [ORDER]}
        elif path == f"/orders-v3/{OID}":
            if order_404:
                return httpx.Response(404, json={"message": "Order not found"})
            data = {"order": dict(ORDER, remainingSize="1000000")}
        elif path == "/fills-v3":
            data = {"fills": [FILL] if q.get("orderId") or q.get("status") == "LOCKED" else []}
        elif path == "/user/fees-v3":
            data = FEES
        elif path == "/user/balance-v3":
            data = {"balances": [{"tokenAddress": TOKEN, "escrowAddress": ESCROW, "availableAmount": "50000000",
                                  "pendingAvailableAmount": "0", "escrowedAmount": "2000000", "pendingEscrowAmount": "0"}]}
        elif path == "/positions-v3":
            data = {"positions": [] if q.get("status") == "SETTLED" else [POSITION]}
        elif path == "/orders-v3/odds/best":
            data = {"bestOdds": [{"marketHash": H, "outcomeOne": BOOK["outcomeOne"][0], "outcomeTwo": BOOK["outcomeTwo"][0]}]}
        elif path == "/orderbook-v3/snapshot/market":
            assert "showTakerPerspective" not in q  # maker frame: the authoritative one
            data = BOOK
        else:
            return httpx.Response(418)
        return httpx.Response(200, json={"status": "success", "data": data})
    return handler


def tools(handler=None, *, key=KEY, rpm=600):
    calls = []
    handler = handler or router()

    def wrapped(request):
        calls.append(request)
        return handler(request)

    settings = Settings(enabled=True, network="mainnet", api_key=key, max_rpm=rpm)
    client = SXBetReadClient(settings, transport=httpx.MockTransport(wrapped))
    reg = _Registry()
    register_sxbet_tools(reg, settings, client=client)
    return reg.tools, calls


def run(c):
    return asyncio.run(c)


NEW = ("sxbet_order_status", "sxbet_positions_summary", "sxbet_exposure_pnl", "sxbet_resting_orders_scope",
       "sxbet_hedge_simulation", "sxbet_close_position_preview", "sxbet_rules_evaluate", "sxbet_risk_status",
       "sxbet_reconciliation_diagnostics")


def _no_secret(out):
    text = json.dumps(out, default=str)
    assert KEY not in text


def test_new_tools_registered_and_named_without_write_verbs():
    t, _ = tools()
    assert set(NEW) <= set(t) and len(t) == 46
    banned = re.compile(r"create|place|post|submit|cancel|approve|transfer|withdraw|deposit|sign|fill_order|heartbeat")
    assert not [n for n in t if banned.search(n)]
    for name in NEW:
        assert "never" in (t[name].__doc__ or "").lower() or "no provider call" in t[name].__doc__.lower(), name


def test_risk_status_makes_no_call_and_reports_mcp_side_state():
    t, calls = tools()
    out = run(t["sxbet_risk_status"]())
    assert calls == [] and out["network_calls_made"] == 0
    assert out["live_trading"] == "UNAVAILABLE" and out["signer_reachable_from_mcp"] is False
    assert out["write_tools"] == [] and out["http_methods_available"] == ["GET"]
    assert out["rate_coordinator"] == "LOCAL_PROCESS" and out["exposure_limits"]["known_to_mcp"] is False


def test_hedge_simulation_end_to_end_gets_only():
    t, calls = tools()
    out = run(t["sxbet_hedge_simulation"](market_hash=H))
    assert {c.method for c in calls} == {"GET"} and out["network_calls_made"] == len(calls) == 7
    h = out["hedge"]
    assert out["status"] == h["status"] == "HEDGE_AVAILABLE", h.get("uncertain_reasons")
    assert h["hedge_outcome"] == "outcome_two" and h["hedge_stake_base_units"] == "2000000"
    assert h["after"]["pnl_base_units"] == {"if_outcome_one": 1_000_000, "if_outcome_two": 1_000_000}
    assert h["worst_acceptable_taker_percentage_odds"] == str(40 * P)
    assert out["signed"] is False and out["submitted"] is False and h["guaranteed_cashout"] is False
    assert out["mode"] == "SIMULATION" and out["label"] == "DERIVED"
    _no_secret(out)


def test_hedge_simulation_inactive_market_is_unavailable():
    t, _ = tools(router(market_status="INACTIVE"))
    out = run(t["sxbet_hedge_simulation"](market_hash=H))
    assert out["status"] == "HEDGE_UNAVAILABLE" and out["hedge"]["reason"] == "MARKET_NOT_ACTIVE"


def test_hedge_simulation_more_pages_makes_it_uncertain():
    def trades_more(request):
        rows = [BET] if request.url.params.get("status") == "LOCKED" else []
        return httpx.Response(200, json={"status": "success", "data": {"trades": rows, "nextKey": "k"}})
    t, _ = tools(router(overrides={"/trades-v3": trades_more}))
    out = run(t["sxbet_hedge_simulation"](market_hash=H))
    assert out["status"] == "HEDGE_UNCERTAIN" and "CONFLICTING_POSITION_DATA" in out["hedge"]["uncertain_reasons"]
    assert out["completeness"]["pages_not_read"] == ["trades"]


def test_close_position_preview_lists_resting_orders_and_full_hedge():
    t, calls = tools()
    out = run(t["sxbet_close_position_preview"](market_hash=H))
    assert {c.method for c in calls} == {"GET"} and out["preview_only"] is True
    scope = out["resting_orders_scope"]
    assert scope["would_affect_order_ids"] == [OID] and scope["cancel_sent"] is False
    assert scope["remaining_size_base_units"] == {"outcome_one": 0, "outcome_two": 1_000_000}
    assert out["hedge"]["inputs"]["mode"] == "FULL" and "NOT A GUARANTEED CASHOUT" in out["warning"]


def test_resting_orders_scope_by_ids_market_and_event():
    t, calls = tools()
    other = "0x" + "c3" * 32
    out = run(t["sxbet_resting_orders_scope"](order_ids=[OID, other]))
    assert out["scope"]["would_affect_order_ids"] == [OID] and out["scope"]["not_open_order_ids"] == [other]
    assert "not a confirmation" in out["scope"]["note"] and out["scope"]["locked_capital_released_base_units"] == 0
    assert [c.url.path for c in calls] == ["/metadata/obv3", "/orders-v3"]
    out = run(t["sxbet_resting_orders_scope"](event_id="L1"))
    assert out["scope"]["orders_affected"] == 1 and calls[-1].url.params["eventId"] == "L1"
    out = run(t["sxbet_resting_orders_scope"](market_hash=H))
    assert out["scope"]["orders_affected"] == 1 and calls[-1].url.params["marketHash"] == H


def test_order_status_combines_order_and_fills():
    t, calls = tools()
    out = run(t["sxbet_order_status"](order_id=OID))
    assert [c.url.path for c in calls] == [f"/orders-v3/{OID}", "/fills-v3"]
    assert calls[1].url.params["orderId"] == OID
    d = out["derived"]
    assert d["filled_by_order_base_units"] == 2_000_000 and d["filled_by_fills_base_units"] == 2_000_000
    assert d["fills_vs_order"] == "MATCH" and d["partially_filled"] is True
    assert out["provider_order"]["remainingSize"] == "1000000"


def test_order_status_404_is_not_proof_of_absence():
    t, _ = tools(router(order_404=True))
    out = run(t["sxbet_order_status"](order_id=OID))
    assert out["derived"]["lookup"] == "NOT_FOUND" and "proves" in out["derived"]["note"]
    assert out["derived"]["fills_count"] == 1


def test_positions_summary_and_exposure_pnl():
    t, calls = tools()
    out = run(t["sxbet_positions_summary"]())
    assert {c.method for c in calls} == {"GET"} and out["status"] == "DERIVED_FROM_REAL_API"
    m = out["position_manager"]["markets"][0]
    assert m["pnl_base_units"]["if_outcome_one"] == 3_000_000 and m["pnl_base_units"]["if_outcome_two"] == -2_000_000
    assert m["cross_checks"][0]["result"] == "MATCH"
    assert m["mark_to_market"]["value_base_units"] == 1_000_000
    assert out["position_manager"]["capital"]["available_conservative_base_units"] == 50_000_000
    n = len(calls)
    out = run(t["sxbet_exposure_pnl"](event_id="L1"))
    assert len(calls) - n == 3 and out["totals"]["mark_to_market_base_units"] == 1_000_000
    assert out["open_markets"][0]["worst_case_liability_base_units"] == 2_000_000
    assert out["totals"]["realized_pnl_base_units"] == 0


def test_reconciliation_diagnostics_reports_ws_conflict():
    t, _ = tools()
    ws = [{"channel": "account:fills_v3_#0xabc", "data": {"fill": dict(FILL, status="MATCHED",
                                                                        updatedAt="2026-07-31T18:50:00.000Z")}}]
    out = run(t["sxbet_reconciliation_diagnostics"](market_hash=H, ws_messages=ws))
    assert out["ws_messages_label"] == "CALLER_SUPPLIED"
    assert out["reconciliation"]["conflicts"][0]["reason"] == "STATUS_REGRESSION_LOCKED_TO_MATCHED"


def test_rules_evaluate_disabled_by_default_and_proposal_never_executable():
    t, _ = tools()
    out = run(t["sxbet_rules_evaluate"](market_hash=H))
    assert out["status"] == "NO_ACTION" and out["rules"]["enabled_rules"] == []
    out = run(t["sxbet_rules_evaluate"](market_hash=H, rules={"take_profit": {"enabled": True,
                                                                              "min_profit_base_units": 500_000}}))
    assert out["status"] == "ORDER_PROPOSAL"
    p = out["rules"]["proposal"]
    assert p["executable"] is False and p["signed"] is False and p["submitted"] is False
    assert out["submitted"] is False


@pytest.mark.parametrize("tool,kwargs,code", [
    ("sxbet_order_status", {"order_id": "../user/balance-v3"}, "INVALID_ORDER_ID"),
    ("sxbet_positions_summary", {"event_id": "a/b"}, "INVALID_EVENT_ID"),
    ("sxbet_exposure_pnl", {"event_id": "x"}, "INVALID_EVENT_ID"),
    ("sxbet_resting_orders_scope", {}, "PASS_EXACTLY_ONE_OF_ORDER_IDS_MARKET_HASH_EVENT_ID"),
    ("sxbet_resting_orders_scope", {"market_hash": H, "event_id": "L1"}, "PASS_EXACTLY_ONE_OF_ORDER_IDS_MARKET_HASH_EVENT_ID"),
    ("sxbet_resting_orders_scope", {"order_ids": [OID, OID.upper().replace("0X", "0x")]}, "DUPLICATE_ORDER_ID"),
    ("sxbet_resting_orders_scope", {"order_ids": ["0x12"]}, "INVALID_ORDER_IDS"),
    ("sxbet_hedge_simulation", {"market_hash": "0x1"}, "INVALID_MARKET_HASH"),
    ("sxbet_hedge_simulation", {"market_hash": H, "mode": "CASHOUT"}, "INVALID_HEDGE_MODE"),
    ("sxbet_hedge_simulation", {"market_hash": H, "max_slippage_percentage_odds": 5}, "INVALID_MAX_SLIPPAGE"),
    ("sxbet_hedge_simulation", {"market_hash": H, "mode": "PARTIAL", "fraction": "2"}, "INVALID_HEDGE_FRACTION"),
    ("sxbet_hedge_simulation", {"market_hash": H, "max_data_age_seconds": 0}, "INVALID_MAX_DATA_AGE_SECONDS"),
    ("sxbet_close_position_preview", {"market_hash": H, "max_slippage_percentage_odds": "-1"}, "INVALID_MAX_SLIPPAGE"),
    ("sxbet_rules_evaluate", {"market_hash": H, "rules": {"auto_trade": {"enabled": True}}}, "UNKNOWN_RULE"),
    ("sxbet_reconciliation_diagnostics", {"ws_messages": "x"}, "INVALID_WS_MESSAGES"),
    ("sxbet_reconciliation_diagnostics", {"ws_messages": [{}] * 501}, "INVALID_WS_MESSAGES"),
])
def test_invalid_input_makes_zero_calls(tool, kwargs, code):
    t, calls = tools()
    out = run(t[tool](**kwargs))
    assert out["status"] == "INVALID_INPUT" and out["error"]["code"] == code and calls == []
    assert out["network_calls_made"] == 0


@pytest.mark.parametrize("tool,kwargs", [
    ("sxbet_order_status", {"order_id": OID}), ("sxbet_positions_summary", {}), ("sxbet_exposure_pnl", {}),
    ("sxbet_resting_orders_scope", {"market_hash": H}), ("sxbet_hedge_simulation", {"market_hash": H}),
    ("sxbet_close_position_preview", {"market_hash": H}), ("sxbet_rules_evaluate", {"market_hash": H}),
    ("sxbet_reconciliation_diagnostics", {}),
])
def test_without_key_zero_calls(tool, kwargs):
    t, calls = tools(key="")
    out = run(t[tool](**kwargs))
    assert out["status"] == "KEY_NOT_CONFIGURED" and calls == []


def test_payload_cap_returns_payload_too_large_never_truncated():
    big = [dict(BET, tradeId="0x" + f"{i:064x}", status="LOCKED", marketHash="0x" + f"{i + 1:064x}") for i in range(900)]
    t, _ = tools(router(trades_rows=big))
    out = run(t["sxbet_positions_summary"]())
    assert out["status"] == "PAYLOAD_TOO_LARGE" and out["error"]["code"] == "SXBET_RESPONSE_EXCEEDS_MCP_SIZE_LIMIT"
    assert "position_manager" not in out and out["network_calls_made"] > 0
    assert len(json.dumps(out)) < 262_144


def test_local_rate_limit_surfaces_fixed_code_and_stops_reading():
    t, calls = tools(rpm=3)
    out = run(t["sxbet_hedge_simulation"](market_hash=H))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED"
    assert out["error"]["retry_after_seconds"] >= 1 and len(calls) == 3 and out["network_calls_made"] == 3


def test_provider_error_stops_without_leaking_body():
    def boom(request):
        return httpx.Response(500, text="internal secret detail " + KEY)
    t, calls = tools(router(overrides={"/user/fees-v3": boom}))
    out = run(t["sxbet_hedge_simulation"](market_hash=H))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == "SXBET_HTTP_ERROR"
    assert "internal secret" not in json.dumps(out) and calls[-1].url.path == "/user/fees-v3"
    _no_secret(out)


def test_derived_modules_have_no_network_or_signing_code():
    import bridge.sxbet.hedging as h
    import bridge.sxbet.positions as p
    import bridge.sxbet.rules as r
    for mod in (h, p, r):
        src = inspect.getsource(mod)
        for needle in ("import httpx", "import requests", "socket", "eth_account", "import signer", "from signer",
                       "client.get"):
            assert needle not in src, (mod.__name__, needle)
    assert "write_tools" in inspect.getsource(sx_tools)
