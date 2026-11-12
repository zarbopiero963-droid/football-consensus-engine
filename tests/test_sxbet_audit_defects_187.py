"""Issue #187 live audit (11-11, deploy 6c69521) defects D1, D2, D3, D5, D6, D8, D9: regression tests.

Offline only (httpx.MockTransport, fake key). Each test failed on main 315f778 and passes with the fix.
READ_ONLY + SIMULATION: nothing here can sign, submit or reach SX Bet.
"""
import inspect

import httpx
import pytest

from bridge.sxbet import hedging as hm
from bridge.sxbet import rules as rl
from bridge.sxbet import simulation as sim
from bridge.sxbet import tools as sx_tools

from test_sxbet_hardening_187 import ADDR, FEES_1PCT, FUTURE, H, P, _ok, make, run, universal
from test_sxbet_hedging import BOOK as HEDGE_BOOK, FEES as HEDGE_FEES, MARKET as HEDGE_MARKET

H2 = "0x" + "cd" * 32
ERROR_NEST = {"message": "PROVIDER-MSG-7f3a", "error": "Not Found", "statusCode": 404}  # ErrorNest shape
OID = "0x" + "9a" * 32


def _no_position(**extra):
    return {"market_hash": H, "grain_used": "NONE", "data_conflict": False,
            "pnl_base_units": {"if_outcome_one": 0, "if_outcome_two": 0, "if_void": 0},
            "projected_payout_fee_base_units": {"if_outcome_one": "0", "if_outcome_two": "0"},
            "matched_stake_base_units": {"total": 0}, "worst_case_liability_base_units": 0, **extra}


# ------------------------------------------------------------------ D1: no position is not "balanced"
def test_d1_hedge_without_position_is_no_open_position_not_available():
    out = hm.simulate_hedge(market_position=_no_position(), book={**HEDGE_BOOK, "marketHash": H},
                            market={**HEDGE_MARKET, "marketHash": H}, fees=HEDGE_FEES, max_data_age_seconds=10,
                            data_age_seconds=1.0, min_order_base_units=1)
    assert out["status"] == "HEDGE_UNAVAILABLE" and out["reason"] == "NO_OPEN_POSITION"
    assert out["position_status"] == "NO_OPEN_POSITION" and out["legs"] == []
    assert "order_parameters_for_owner_review" not in out


def test_d1_balanced_real_position_still_reported_as_balanced():
    pos = _no_position(pnl_base_units={"if_outcome_one": 10, "if_outcome_two": 10, "if_void": 0},
                       matched_stake_base_units={"total": 500})
    out = hm.simulate_hedge(market_position=pos, book={**HEDGE_BOOK, "marketHash": H},
                            market={**HEDGE_MARKET, "marketHash": H}, fees=HEDGE_FEES, max_data_age_seconds=10,
                            data_age_seconds=1.0, min_order_base_units=1)
    assert out["status"] == "HEDGE_AVAILABLE" and out["reason"] == "POSITION_ALREADY_BALANCED"


def test_d1_unknown_matched_stake_is_never_read_as_no_position():
    pos = _no_position(matched_stake_base_units={"total": None})
    out = hm.simulate_hedge(market_position=pos, book={**HEDGE_BOOK, "marketHash": H},
                            market={**HEDGE_MARKET, "marketHash": H}, fees=HEDGE_FEES, max_data_age_seconds=10,
                            data_age_seconds=1.0, min_order_base_units=1)
    assert out.get("reason") != "NO_OPEN_POSITION"


@pytest.mark.parametrize("rule,cfg", [
    ("stop_loss", {"enabled": True, "max_loss_base_units": 400}),
    ("take_profit", {"enabled": True, "min_profit_base_units": 100}),
    ("trailing_stop", {"enabled": True, "trail_base_units": 100, "peak_mark_to_market_base_units": 400}),
])
def test_d1_position_rules_report_no_open_position(rule, cfg):
    pos = {"market_hash": H, "mark_to_market": {"value_base_units": 0}, "worst_case_liability_base_units": 0,
           "matched_stake_base_units": {"total": 0}}
    out = rl.evaluate({rule: cfg}, market_position=pos, market={"status": "ACTIVE", "gameTime": FUTURE},
                      now_unix=1_800_000_000, data_age_seconds=1.0)
    row = next(r for r in out["results"] if r["rule"] == rule)
    assert row["result"] == "NO_ACTION" and row["reason"] == "NO_OPEN_POSITION"


def test_d1_tools_without_position_are_consistent():
    """Live audit D1: account with no bet and no order on an ACTIVE market (empty trades and orders)."""
    empty = {"/trades-v3": lambda r: _ok({"trades": [], "nextKey": None}),
             "/orders-v3": lambda r: _ok({"orders": [], "nextKey": None}),
             "/markets/find": lambda r: _ok([{"marketHash": H, "status": "ACTIVE", "outcomeOneName": "A",
                                              "outcomeTwoName": "B", "gameTime": FUTURE}])}
    t, _, _ = make(universal(fees=FEES_1PCT, overrides=empty))
    hedge = run(t["sxbet_hedge_simulation"](market_hash=H))
    close = run(t["sxbet_close_position_preview"](market_hash=H))
    rules = run(t["sxbet_rules_evaluate"](market_hash=H, rules={"stop_loss": {"enabled": True,
                                                                             "max_loss_base_units": 1}}))
    for out in (hedge, close):
        assert out["hedge"]["status"] == "HEDGE_UNAVAILABLE" and out["hedge"]["reason"] == "NO_OPEN_POSITION"
    assert rules["hedge_status"] == "HEDGE_UNAVAILABLE"
    stop = next(r for r in rules["rules"]["results"] if r["rule"] == "stop_loss")
    assert stop["result"] == "NO_ACTION" and stop["reason"] == "NO_OPEN_POSITION"


# ------------------------------------------------------------------ D2: governance texts follow §2.6
def test_d2_no_out_of_band_per_order_approval_language_left():
    for mod in (hm, rl, sim, sx_tools):
        src = inspect.getsource(mod)
        # The key name survives only as a deprecated alias (compatibility); the per-order approval wording is gone.
        assert "out-of-band owner approval" not in src and "out-of-band approval" not in src, mod.__name__
        assert "owner-approved, signer-verified" not in src, mod.__name__
        assert "per-order confirmation" not in src, mod.__name__
    t, _, _ = make()
    health, risk = run(t["sxbet_health"]()), run(t["sxbet_risk_status"]())
    assert health["live_trading"].startswith("UNAVAILABLE") and "testnet" in health["live_trading"]
    assert risk["live_trading"] == "UNAVAILABLE" and "explicit, complete owner command" in risk["live_trading_reason"]


# ------------------------------------------------------------------ D3: 404 is NOT_FOUND only where documented
@pytest.mark.parametrize("tool,kwargs,path", [
    ("sxbet_order", {"order_id": OID}, "/orders-v3/" + OID),
    ("sxbet_order_by_client_id", {"client_order_id": "cid-0001"}, "/orders-v3/client/cid-0001"),
    ("sxbet_trade", {"trade_id": "t-123"}, "/trades-v3/t-123"),
    ("sxbet_orderbook_market", {"market_hash": H}, "/orderbook-v3/snapshot/market"),
])
def test_d3_documented_404_is_not_found_not_provider_error(tool, kwargs, path):
    t, _, _ = make(universal(overrides={path: lambda r: httpx.Response(404, json=ERROR_NEST)}))
    out = run(t[tool](**kwargs))
    assert out["status"] == "NOT_FOUND" and out["http_status"] == 404
    assert "PROVIDER-MSG-7f3a" not in str(out)  # the provider message is never echoed to the client
    assert out["error"] == {"code": "SXBET_NOT_FOUND"} and out["data"] is None
    assert out["not_found"]["proof_of_absence"] is False and "openapi.json" in out["not_found"]["contract"]
    assert out["network_calls_made"] == 1


@pytest.mark.parametrize("tool,kwargs,path", [
    # 404 not documented as "not found" by the contract: stays a provider error, never an answer
    ("sxbet_safe_info", {"safe_address": ADDR}, f"/user/multisig/{ADDR}/info"),
    ("sxbet_public_market_orders", {"market_hash": H}, "/orders-v3/public"),  # 404 = identity unavailable
    ("sxbet_metadata", {}, "/metadata/obv3"),
])
def test_d3_undocumented_404_stays_provider_error(tool, kwargs, path):
    t, _, _ = make(universal(overrides={path: lambda r: httpx.Response(404, json=ERROR_NEST)}))
    out = run(t[tool](**kwargs))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == "SXBET_HTTP_ERROR"
    assert out["http_status"] == 404 and "not_found" not in out


def test_d3_other_http_errors_stay_provider_error():
    t, _, _ = make(universal(overrides={"/orders-v3/" + OID: lambda r: httpx.Response(500, text="x")}))
    out = run(t["sxbet_order"](order_id=OID))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == "SXBET_HTTP_ERROR"


@pytest.mark.parametrize("body", [
    {"statusCode": 404},                                   # no ErrorNest "Not Found"
    {"statusCode": 404, "error": "Not Found"},             # no message
    {"message": "Cannot GET /orders-v3/x", "error": "Not Found"},  # routing 404, no statusCode
    "<html>404</html>", "",
])
def test_d3_documented_route_without_error_nest_body_stays_provider_error(body):
    """Review 5bc55ce P2: an edge/CDN/routing 404 on a documented route is not a provider "not found"."""
    resp = (lambda r: httpx.Response(404, json=body)) if isinstance(body, dict) else (
        lambda r: httpx.Response(404, text=body))
    t, _, _ = make(universal(overrides={"/orders-v3/" + OID: resp}))
    out = run(t["sxbet_order"](order_id=OID))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == "SXBET_HTTP_ERROR" and "not_found" not in out


def test_d3_allowlist_equals_the_pinned_official_404_snapshot():
    """Review 5bc55ce P3: exactly the GET routes whose pinned OpenAPI 404 is an ErrorNest "Not Found"."""
    import json
    import pathlib
    snap = json.loads((pathlib.Path(__file__).parents[1] / "docs/sxbet/openapi_get_404_snapshot.json").read_text())
    documented = {p for p, r in snap["get_404_responses"].items()
                  if r["schema_ref"] == "#/components/schemas/ErrorNest" and isinstance(r["example"], dict)
                  and r["example"].get("error") == "Not Found"}
    assert documented == set(sx_tools.NOT_FOUND_DOCUMENTED)
    assert "/orders-v3/public" in snap["get_404_responses"] and "/orders-v3/public" not in documented


# ------------------------------------------------------------------ D2 compatibility of the renamed key
def test_d2_renamed_key_keeps_the_deprecated_alias_with_the_same_value():
    out = hm.simulate_hedge(market_position={**_no_position(), "matched_stake_base_units": {"total": 500},
                                             "pnl_base_units": {"if_outcome_one": 300, "if_outcome_two": -200,
                                                                "if_void": 0}},
                            book={**HEDGE_BOOK, "marketHash": H}, market={**HEDGE_MARKET, "marketHash": H},
                            fees=HEDGE_FEES, max_slippage_percentage_odds=5 * P, max_data_age_seconds=10,
                            data_age_seconds=1.0, min_order_base_units=1)
    params = out["order_parameters_for_owner_review"]
    assert params["requires_explicit_owner_command"] is True
    assert params["requires_owner_out_of_band_approval"] is True
    assert params["deprecated_fields"] == {"requires_owner_out_of_band_approval": "use requires_explicit_owner_command"}
    assert params["executable"] is False and params["signed"] is False and params["submitted"] is False


# ------------------------------------------------------------------ D5: no intent for an untradeable market
@pytest.mark.parametrize("rows,code", [
    ([], "MARKET_NOT_FOUND"),
    ([{"marketHash": H, "status": "INACTIVE", "outcomeOneName": "A", "outcomeTwoName": "B"}], "MARKET_INACTIVE"),
    ([{"marketHash": H, "status": "ACTIVE", "outcomeOneName": "A"}], "MARKET_OUTCOMES_MISSING"),
])
def test_d5_refused_market_yields_no_intent_or_intent_hash(rows, code):
    import time
    t, calls, _ = make(universal(overrides={"/markets/find": lambda r: _ok(rows)}))
    out = run(t["sxbet_simulate_order"](
        market_hash=H, betting_outcome_one=True, stake_base_units=5_000_000, percentage_odds=str(50 * P),
        time_in_force="GTC", expiry=int(time.time()) + 3600, idempotency_key="key-d5-0001", maker_address=ADDR))
    assert out["status"] == "REFUSED" and out["error"]["code"] == code
    assert out["intent"] is None and out["intent_hash"] is None and out["signing_ready"] is False
    assert "live_eligibility" not in out
    assert out["requested"] == {"market_hash": H, "betting_outcome_one": True, "stake_base_units": "5000000",
                                "requested_percentage_odds": str(50 * P), "time_in_force": "GTC",
                                "expiry": out["requested"]["expiry"]}
    assert type(out["requested"]["expiry"]) is int and out["fill"]["status"] == "NOT_SIMULATABLE"
    assert [c.url.path for c in calls] == ["/metadata/obv3", "/markets/find"]


# ------------------------------------------------------------------ D6: skipped ids are listed
def test_d6_markets_find_lists_hashes_the_provider_skipped():
    t, _, _ = make(universal(overrides={"/markets/find": lambda r: _ok([{"marketHash": H.upper().replace("0X", "0x"),
                                                                        "status": "ACTIVE"}])}))
    out = run(t["sxbet_markets_find"](market_hashes=[H, H2]))
    assert out["status"] == "REAL_API_RESPONSE"
    assert out["not_found"] == [H2] and out["lookup_complete"] is True


def test_d6_fixture_status_lists_event_ids_the_provider_skipped():
    t, _, _ = make(universal(overrides={"/fixture/status": lambda r: _ok({"L1": {"status": 1}})}))
    out = run(t["sxbet_fixture_status"](sportx_event_ids=["L1", "L999"]))
    assert out["not_found"] == ["L999"] and out["data"] == {"L1": {"status": 1}}


def test_d6_unexpected_shape_is_not_an_empty_not_found():
    t, _, _ = make(universal(overrides={"/markets/find": lambda r: _ok({"weird": True})}))
    out = run(t["sxbet_markets_find"](market_hashes=[H]))
    assert out["not_found"] is None and out["lookup_complete"] is False


# ------------------------------------------------------------------ D8: path parameters reported
@pytest.mark.parametrize("tool,kwargs,expected", [
    ("sxbet_order", {"order_id": OID}, {"orderId": OID}),
    ("sxbet_order_by_client_id", {"client_order_id": "cid-0001"}, {"clientOrderId": "cid-0001"}),
    ("sxbet_trade", {"trade_id": "t-123"}, {"tradeId": "t-123"}),
    ("sxbet_proxy_address_check", {"proxy_address": ADDR}, {"proxyAddress": ADDR}),
    ("sxbet_safe_info", {"safe_address": ADDR}, {"safeAddress": ADDR}),
])
def test_d8_path_tools_report_their_path_parameters(tool, kwargs, expected):
    t, _, _ = make(universal(overrides={"/orders-v3/" + OID: lambda r: _ok({})}))
    out = run(t[tool](**kwargs))
    assert {k: v.lower() for k, v in out["path_parameters"].items()} == {k: v.lower() for k, v in expected.items()}


# ------------------------------------------------------------------ D9: fee scale declared from the contract
def test_d9_fees_declare_official_fraction_scale_and_keep_raw():
    t, _, _ = make(universal(fees=FEES_1PCT))
    out = run(t["sxbet_fees"]())
    view = out["fee_rates"]
    assert view["scale"].startswith("DECIMAL_FRACTION") and view["scale_source"].endswith("get-user-fees-v3")
    assert view["provider_raw"] == FEES_1PCT and out["data"] == FEES_1PCT
    assert view["percent"]["takerPayoutFee"] == "1" and view["percent"]["takerParlayPayoutFee"] == "5"
    assert view["percent"]["makerPayoutFee"] is None  # provider null stays null, never 0
    assert view["null_rate_notes"]["makerPayoutFee"] == "PROVIDER_NULL_UNSET_NOTHING_CHARGED"


def test_d9_malformed_fees_are_never_zero():
    t, _, _ = make(universal(fees={"takerPayoutFee": "0.01"}))
    out = run(t["sxbet_fees"]())
    assert out["fee_rates"]["fee_status"] == "FEES_MALFORMED" and out["fee_rates"]["percent"] is None


# ------------------------------------------------------------------ fixes from the per-commit reviews
def test_review_cefd2df_p2_1_conflicting_data_is_never_no_open_position():
    out = hm.simulate_hedge(market_position=_no_position(data_conflict=True), book={**HEDGE_BOOK, "marketHash": H},
                            market={**HEDGE_MARKET, "marketHash": H}, fees=HEDGE_FEES, max_data_age_seconds=10,
                            data_age_seconds=1.0, min_order_base_units=1)
    assert out["status"] == "HEDGE_UNCERTAIN" and "CONFLICTING_POSITION_DATA" in out["uncertain_reasons"]


def test_review_cefd2df_p2_3_unknown_market_state_yields_no_intent():
    import time
    t, calls, _ = make(universal(overrides={"/markets/find": lambda r: httpx.Response(500, text="x")}))
    out = run(t["sxbet_simulate_order"](
        market_hash=H, betting_outcome_one=False, stake_base_units=5_000_000, percentage_odds=str(50 * P),
        time_in_force="GTC", expiry=int(time.time()) + 3600, idempotency_key="key-d5-0002", maker_address=ADDR))
    assert out["status"] == "PROVIDER_ERROR" and out["error"]["code"] == "SXBET_HTTP_ERROR"
    assert out["intent"] is None and out["intent_hash"] is None and out["signing_ready"] is False
    assert "live_eligibility" not in out and out["requested"]["betting_outcome_one"] is False


def test_review_cefd2df_p3_2_unknown_liability_is_not_no_position():
    pos = {"market_hash": H, "mark_to_market": {"value_base_units": None}, "worst_case_liability_base_units": None,
           "matched_stake_base_units": {"total": 0}}
    out = rl.evaluate({"stop_loss": {"enabled": True, "max_loss_base_units": 1}}, market_position=pos,
                      market={"status": "ACTIVE", "gameTime": FUTURE}, now_unix=1_800_000_000, data_age_seconds=1.0)
    row = next(r for r in out["results"] if r["rule"] == "stop_loss")
    assert row["result"] == "ALERT" and row["reason"] == "MARK_TO_MARKET_UNAVAILABLE_CANNOT_EVALUATE"


def test_review_cefd2df_p3_3_malformed_rows_and_duplicates():
    t, _, _ = make(universal(overrides={"/markets/find": lambda r: _ok([{"status": "ACTIVE"}])}))
    out = run(t["sxbet_markets_find"](market_hashes=[H]))
    assert out["not_found"] is None and out["lookup_complete"] is False
    assert sx_tools.requested_vs_returned([H2, H2.upper().replace("0X", "0x"), H], [H], case_insensitive=True)[
        "not_found"] == [H2]


def test_review_5bc55ce_p3_proposal_hash_unchanged_by_the_compat_keys():
    params = {"executable": False, "signed": False, "submitted": False, "market_hash": H,
              "betting_outcome_one": False, "stake_base_units": "212", "percentage_odds_limit": str(45 * P)}
    pos = {"market_hash": H, "mark_to_market": {"value_base_units": -500}, "worst_case_liability_base_units": 200,
           "matched_stake_base_units": {"total": 200}}

    def proposal(extra):
        hedge = {"status": "HEDGE_AVAILABLE", "order_parameters_for_owner_review": {**params, **extra}}
        return rl.evaluate({"stop_loss": {"enabled": True, "max_loss_base_units": 400}}, market_position=pos,
                           market={"status": "ACTIVE", "gameTime": FUTURE}, now_unix=1_800_000_000,
                           data_age_seconds=1.0, hedge=hedge)["proposal"]
    old = proposal({"requires_owner_out_of_band_approval": True, "note": "x"})       # shape on main 315f778
    new = proposal({"requires_explicit_owner_command": True, "requires_owner_out_of_band_approval": True,
                    "deprecated_fields": {"requires_owner_out_of_band_approval": "use ..."}, "note": "y"})
    assert old["proposal_hash"] == new["proposal_hash"]
