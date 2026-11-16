"""Issue #187, R2 (owner directive 16-11-2026): offline contract certification of `sxbet_trade` and
`sxbet_proxy_funding_status`, the two account reads the mainnet read-only audit of 16-11 could not exercise on
real data (the account has no trade and no deposit session).

Contract source: https://docs.sx.bet/openapi.json, pinned in docs/sxbet/r2_contract_snapshot.json
(GET /trades-v3/{tradeId}: 200 {data: {trade}}, 404 ErrorNest "Trade not found", 503;
 GET /user/transfer-to-proxy/status?sessionId=<permit digest, hex without 0x>: 200 {data: {sessionId, status:
 PENDING|SUCCESS|FAILED}}, 400 "TRANSFER_NOT_FOUND" (unknown session or not yours), 401 BAD_AUTH, 503).

Offline only (httpx.MockTransport, fake key): this certifies the TOOL's handling of the documented contract. It is
NOT a test on the owner's real data: on the live account both reads stay NOT_TESTED until a real tradeId /
sessionId exists (never invented). READ_ONLY: nothing here can sign, submit or move funds.
"""
import json
import pathlib

import httpx
import pytest

from test_sxbet_hardening_187 import _ok, make, run

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads((ROOT / "docs" / "sxbet" / "r2_contract_snapshot.json").read_text())
TRADE_ID = "0xac6bb30bba6ea82d8717219a4fa49d15b58fd638fba479f294b4d402376d14d6"   # the OpenAPI example id
SESSION = "9c8f5b21e4d0a7c3f18b6e94d2a05c7318fa4b62d9e0173c5a8b2f4e6019d3c7"      # the OpenAPI example digest
PROVIDER_SECRET_MSG = "PROVIDER-MSG-r2-5d1e"  # must never be echoed to the MCP client (fixed codes only)
# The official OpenAPI 200 example (pinned), never invented fields (review of 9729fe1, P3).
TRADE = SNAPSHOT["routes"]["/trades-v3/{tradeId}"]["responses"]["200"]["example"]["data"]["trade"]


def _only(path, handler):
    seen = []

    def route(request):
        seen.append(request)
        if request.method != "GET" or request.url.path != path:
            return httpx.Response(599)
        return handler(request)
    return route, seen


def _error(status, error, message=PROVIDER_SECRET_MSG):
    return lambda r: httpx.Response(status, json={"message": message, "error": error, "statusCode": status})


# ---------------------------------------------------------------- the pinned contract is the official one
def test_snapshot_pins_both_routes_from_the_official_openapi():
    assert SNAPSHOT["source"] == "https://docs.sx.bet/openapi.json"
    trade, status = SNAPSHOT["routes"]["/trades-v3/{tradeId}"], SNAPSHOT["routes"]["/user/transfer-to-proxy/status"]
    assert set(trade["responses"]) == {"200", "404", "503"}
    assert trade["responses"]["404"]["example"]["message"] == "Trade not found"
    assert set(status["responses"]) == {"200", "400", "401", "503"}
    assert status["responses"]["400"]["example"]["message"] == "TRANSFER_NOT_FOUND"
    assert status["status_enum"] == ["PENDING", "SUCCESS", "FAILED"]
    assert "without the 0x prefix" in status["session_id_description"]
    # Review 9729fe1 P3: the 200 schemas and examples are pinned too, and the trade object is TradeV3.
    t200 = trade["responses"]["200"]
    assert t200["schema"]["properties"]["data"]["properties"]["trade"]["$ref"] == "#/components/schemas/TradeV3"
    assert t200["example"]["data"]["trade"]["tradeId"] == TRADE_ID and "TradeV3" in SNAPSHOT["components"]
    assert status["responses"]["200"]["example"]["data"]["sessionId"] == SESSION


# ---------------------------------------------------------------- sxbet_trade: positive
def test_trade_found_is_returned_exactly_as_received_with_one_authenticated_get():
    route, seen = _only(f"/trades-v3/{TRADE_ID}", lambda r: _ok({"trade": TRADE}))
    t, calls, _ = make(route)
    out = run(t["sxbet_trade"](trade_id=TRADE_ID))
    assert out["status"] == "REAL_API_RESPONSE" and out["data"] == {"trade": TRADE}
    assert out["network_calls_made"] == 1 and len(seen) == 1
    assert seen[0].headers.get("x-sx-api-key") and seen[0].url.params == httpx.QueryParams()
    assert out["path_parameters"] == {"tradeId": TRADE_ID}                          # S1 D8: id echoed, not hidden


# ---------------------------------------------------------------- sxbet_trade: negative / non-existent
def test_trade_unknown_id_documented_404_is_not_found_never_proof_of_absence():
    route, _ = _only(f"/trades-v3/{TRADE_ID}", _error(404, "Not Found", "Trade not found"))
    t, _, _ = make(route)
    out = run(t["sxbet_trade"](trade_id=TRADE_ID))
    assert out["status"] == "NOT_FOUND" and out["http_status"] == 404 and out["error"]["code"] == "SXBET_NOT_FOUND"
    assert out["not_found"]["proof_of_absence"] is False
    assert "Trade not found" not in json.dumps(out["error"])                         # provider text never echoed


def test_trade_404_without_the_documented_body_is_a_provider_error():
    route, _ = _only(f"/trades-v3/{TRADE_ID}", lambda r: httpx.Response(404, text="<html>gateway</html>"))
    t, _, _ = make(route)
    out = run(t["sxbet_trade"](trade_id=TRADE_ID))
    assert out["status"] == "PROVIDER_ERROR" and out["http_status"] == 404


@pytest.mark.parametrize("status, code", [(401, "SXBET_UNAUTHORIZED"), (503, "SXBET_HTTP_ERROR"),
                                          (500, "SXBET_HTTP_ERROR")])
def test_trade_provider_errors_use_fixed_codes_and_never_echo_the_body(status, code):
    route, _ = _only(f"/trades-v3/{TRADE_ID}", _error(status, "X"))
    t, _, _ = make(route)
    out = run(t["sxbet_trade"](trade_id=TRADE_ID))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == code and out["data"] is None
    assert PROVIDER_SECRET_MSG not in json.dumps(out)


@pytest.mark.parametrize("bad", ["", "a/b", "../orders-v3", "x?y=1", "a" * 129, "0x 12", "trade#1", None, 123])
def test_trade_invalid_ids_are_refused_before_any_network_call(bad):
    t, calls, _ = make(lambda r: _ok({"trade": TRADE}))
    out = run(t["sxbet_trade"](trade_id=bad))
    assert out["status"] == "INVALID_INPUT" and out["error"]["code"] == "INVALID_TRADE_ID" and calls == []


def test_trade_without_api_key_makes_no_call():
    t, calls, _ = make(lambda r: _ok({"trade": TRADE}), key="")
    out = run(t["sxbet_trade"](trade_id=TRADE_ID))
    assert out["status"] == "KEY_NOT_CONFIGURED" and calls == []


# ---------------------------------------------------------------- sxbet_trade: malformed replies
@pytest.mark.parametrize("response, code", [
    (lambda r: httpx.Response(200, text="not json"), "SXBET_INVALID_JSON"),
    (lambda r: httpx.Response(200, json={"status": "failure", "data": None}), "SXBET_PROVIDER_STATUS_NOT_SUCCESS"),
])
def test_trade_malformed_replies_are_errors_never_a_trade(response, code):
    route, _ = _only(f"/trades-v3/{TRADE_ID}", response)
    t, _, _ = make(route)
    out = run(t["sxbet_trade"](trade_id=TRADE_ID))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == code and out["data"] is None


def test_trade_success_without_a_trade_object_is_echoed_raw_never_invented():
    """A 200 whose data lacks `trade` is provider data as received: the tool adds no trade and no defaults."""
    route, _ = _only(f"/trades-v3/{TRADE_ID}", lambda r: _ok({}))
    t, _, _ = make(route)
    out = run(t["sxbet_trade"](trade_id=TRADE_ID))
    assert out["status"] == "REAL_API_RESPONSE" and out["data"] == {}


# ---------------------------------------------------------------- sxbet_proxy_funding_status
@pytest.mark.parametrize("state", ["PENDING", "SUCCESS", "FAILED"])
def test_funding_status_each_documented_state_is_returned_as_received(state):
    body = {"sessionId": SESSION, "status": state}
    route, seen = _only("/user/transfer-to-proxy/status", lambda r: _ok(body))
    t, _, _ = make(route)
    out = run(t["sxbet_proxy_funding_status"](session_id=SESSION))
    assert out["status"] == "REAL_API_RESPONSE" and out["data"] == body and out["network_calls_made"] == 1
    assert "contract_deviation" not in out
    assert dict(seen[0].url.params) == {"sessionId": SESSION} and seen[0].headers.get("x-sx-api-key")


def test_funding_status_unknown_or_foreign_session_400_is_an_error_never_a_state():
    """400 TRANSFER_NOT_FOUND (unknown sessionId, or not yours): never PENDING/SUCCESS/FAILED, never NOT_FOUND
    (that status is reserved to the documented 404 routes), provider text never echoed."""
    route, _ = _only("/user/transfer-to-proxy/status", _error(400, "Bad Request", "TRANSFER_NOT_FOUND"))
    t, _, _ = make(route)
    out = run(t["sxbet_proxy_funding_status"](session_id=SESSION))
    assert out["status"] == "PROVIDER_ERROR" and out["http_status"] == 400 and out["data"] is None
    assert "TRANSFER_NOT_FOUND" not in json.dumps(out["error"])


@pytest.mark.parametrize("status, code", [(401, "SXBET_UNAUTHORIZED"), (503, "SXBET_HTTP_ERROR")])
def test_funding_status_auth_and_availability_errors(status, code):
    route, _ = _only("/user/transfer-to-proxy/status", _error(status, "X"))
    t, _, _ = make(route)
    out = run(t["sxbet_proxy_funding_status"](session_id=SESSION))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == code
    assert PROVIDER_SECRET_MSG not in json.dumps(out)


@pytest.mark.parametrize("bad", ["", "a/b", "x?y=1", "s" * 129, "abc def", None, 7])
def test_funding_status_invalid_session_ids_make_no_call(bad):
    t, calls, _ = make(lambda r: _ok({"sessionId": SESSION, "status": "SUCCESS"}))
    out = run(t["sxbet_proxy_funding_status"](session_id=bad))
    assert out["status"] == "INVALID_INPUT" and out["error"]["code"] == "INVALID_SESSION_ID" and calls == []


@pytest.mark.parametrize("data", [{"sessionId": SESSION, "status": "DONE"}, {"sessionId": SESSION},
                                  {"sessionId": "other", "status": "SUCCESS"}])
def test_funding_status_out_of_contract_replies_are_echoed_raw_not_normalised(data):
    """A status outside the documented enum, a missing status or another session echo is returned as received:
    the tool never maps it to SUCCESS / FAILED (no invented normalisation, governance 2.3)."""
    route, _ = _only("/user/transfer-to-proxy/status", lambda r: _ok(data))
    t, _, _ = make(route)
    out = run(t["sxbet_proxy_funding_status"](session_id=SESSION))
    assert out["status"] == "REAL_API_RESPONSE" and out["data"] == data
    # Review of 9729fe1 (P2): echoed raw, but explicitly flagged as outside the documented contract.
    assert out["contract_deviation"]


def test_funding_status_is_read_only_no_write_route_reachable():
    route, seen = _only("/user/transfer-to-proxy/status", lambda r: _ok({"sessionId": SESSION, "status": "PENDING"}))
    t, _, _ = make(route)
    run(t["sxbet_proxy_funding_status"](session_id=SESSION))
    assert [r.method for r in seen] == ["GET"]


@pytest.mark.parametrize("data, codes", [
    ({"sessionId": "other", "status": "SUCCESS"}, ["SESSION_ECHO_MISMATCH"]),
    ({"sessionId": SESSION, "status": "DONE"}, ["STATUS_OUTSIDE_DOCUMENTED_ENUM"]),
    ({"sessionId": SESSION}, ["STATUS_OUTSIDE_DOCUMENTED_ENUM"]),
    ([], ["DATA_NOT_AN_OBJECT"]),
])
def test_review_9729fe1_funding_status_contract_deviation_codes(data, codes):
    route, _ = _only("/user/transfer-to-proxy/status", lambda r: _ok(data))
    t, _, _ = make(route)
    assert run(t["sxbet_proxy_funding_status"](session_id=SESSION))["contract_deviation"] == codes
