"""SX Bet hardening (issue #187): offline only (httpx.MockTransport, fake key), READ_ONLY + SIMULATION.

Covers: upcoming-only view of fixtures/markets, payout-fee (commission) reconciliation on profit with the
issue's reference case, Decimal-only money outputs with an explicit rounding rule, expiry diagnostics,
declared max provider cost per tool, hedge/cash-out formulas with fees, edge cases with fixed codes, and a
regression sweep over every registered sxbet_* tool. No test here can reach SX Bet, sign or submit.
"""
import asyncio
import inspect
import json
import re
import time
from decimal import Decimal

import httpx
import pytest

from bridge.sxbet import hedging as hm
from bridge.sxbet import positions as pm
from bridge.sxbet import simulation as sim
from bridge.sxbet import tools as sx_tools
from bridge.sxbet.client import SXBetReadClient, Settings
from bridge.sxbet.tools import TOOL_COST_CATALOG, register_sxbet_tools, upcoming_filter

from test_sxbet_derived_tools import BOOK, META, OID, router as derived_router  # shared offline fixtures

KEY = "FAKE_SX_TEST_KEY_DO_NOT_LOG"
H = "0x" + "ab" * 32
ADDR = "0x" + "4c" * 20
P = 10 ** 18
FEES_1PCT = {"refundFee": None, "makerPayoutFee": None, "takerPayoutFee": "0.01",
             "makerParlayPayoutFee": None, "takerParlayPayoutFee": "0.05"}
FUTURE, PAST = 4_000_000_000, 1_000_000_000


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            assert fn.__name__ not in self.tools
            self.tools[fn.__name__] = fn
            return fn
        return attach


def universal(fees=None, overrides=None):
    """Every documented GET answered offline. Unknown routes get an empty success body."""
    base = derived_router(overrides={"/user/fees-v3": (lambda r: _ok(fees)) if fees is not None else None} if fees
                          is not None else None)
    overrides = overrides or {}

    def handler(request):
        path, q = request.url.path, request.url.params
        if request.method != "GET":
            return httpx.Response(405)
        if path in overrides:
            return overrides[path](request)
        if path == "/orderbook-v3/snapshot/market" and q.get("showTakerPerspective") == "true":
            return _ok({"marketHash": H, "outcomeOne": [{"percentageOdds": str(50 * P), "size": "3000000"}],
                        "outcomeTwo": [], "version": "1"})
        if path == "/fixture/active":
            return _ok([{"eventId": "L1", "startDate": "2096-10-01T18:00:00Z", "status": 1},
                        {"eventId": "L0", "startDate": "2001-09-09T01:46:40Z", "status": 1}])
        if path == "/markets/active":
            return _ok({"markets": [{"marketHash": H, "gameTime": FUTURE, "status": "ACTIVE"}], "nextKey": None})
        resp = base(request)
        if resp.status_code == 418:
            return _ok({})
        return resp
    return handler


def _ok(data):
    return httpx.Response(200, json={"status": "success", "data": data})


def make(handler=None, *, key=KEY):
    calls = []

    def wrapped(request):
        calls.append(request)
        return (handler or universal())(request)

    settings = Settings(enabled=True, network="mainnet", api_key=key, max_rpm=600)
    reg = _Registry()
    names = register_sxbet_tools(reg, settings, client=SXBetReadClient(settings, transport=httpx.MockTransport(wrapped)))
    return reg.tools, calls, names


def run(c):
    return asyncio.run(c)


def _floats(obj, path=""):
    """Paths of every float in a JSON-like object (money must never be float)."""
    if isinstance(obj, float):
        return [path]
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in _floats(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _floats(v, f"{path}[{i}]")]
    return []


# Time-like floats are allowed (seconds of data age); money fields never.
_ALLOWED_FLOAT = re.compile(r"(data_age_seconds|max_data_age_seconds)$")


# ======================================================== 1. upcoming-only view
@pytest.mark.parametrize("value,reason", [
    (None, "START_TIME_MISSING"),
    ("2026-11-14 22:00:00", "TIMEZONE_UNRESOLVED"),
    ("2026-11-14T22:00:00", "TIMEZONE_UNRESOLVED"),
    ("tomorrow", "START_TIME_UNPARSEABLE"),
    ("2026-13-40T22:00:00Z", "START_TIME_UNPARSEABLE"),
    (1793548800, "START_TIME_FORMAT_DOCS_UNVERIFIED"),  # unit of a numeric startDate is not documented
])
def test_fixture_start_time_never_guessed(value, reason):
    out = upcoming_filter([{"eventId": "E", "startDate": value, "status": 1}], kind="fixtures", now_unix=PAST)
    up = out["upcoming"]
    assert up["rows"] == [] and up["status"] == "PARTIAL" and up["excluded_unresolved"][0]["reason"] == reason
    assert up["excluded_unresolved"][0]["provider_value"] == value  # raw kept, never converted


def test_fixture_upcoming_vs_started_and_boundary():
    rows = [{"eventId": "F", "startDate": "2096-10-01T18:00:00+02:00", "status": 1},
            {"eventId": "S", "startDate": "2001-09-09T01:46:40Z", "status": 1},
            {"eventId": "N", "startDate": "2001-09-09T03:46:40+02:00", "status": 1}]  # == now: already started
    out = upcoming_filter(rows, kind="fixtures", now_unix=PAST)["upcoming"]
    assert [r["eventId"] for r in out["rows"]] == ["F"] and out["rows"][0] is rows[0]
    assert [r["id"] for r in out["excluded_started"]] == ["S", "N"]
    assert out["counts"] == {"provider_rows": 3, "upcoming": 1, "excluded_started": 2, "excluded_unresolved": 0}
    assert out["status"] == "OK" and out["label"] == "DERIVED" and out["reference_now_utc"].endswith("+00:00")


@pytest.mark.parametrize("value,reason", [
    (None, "START_TIME_MISSING"), ("1793548800", "START_TIME_UNPARSEABLE"), (True, "START_TIME_UNPARSEABLE"),
    (0, "START_TIME_UNPARSEABLE"), (1.5e9, "START_TIME_UNPARSEABLE")])
def test_market_game_time_must_be_unix_int(value, reason):
    out = upcoming_filter({"markets": [{"marketHash": H, "gameTime": value, "status": "ACTIVE"}]},
                          kind="markets", now_unix=PAST)["upcoming"]
    assert out["rows"] == [] and out["excluded_unresolved"][0]["reason"] == reason


def test_market_status_active_in_the_past_is_excluded_not_hidden():
    data = {"markets": [{"marketHash": H, "gameTime": PAST - 1, "status": "ACTIVE"},
                        {"marketHash": "0x" + "cd" * 32, "gameTime": FUTURE, "status": "ACTIVE"}], "nextKey": "k"}
    out = upcoming_filter(data, kind="markets", now_unix=PAST)["upcoming"]
    assert [r["marketHash"] for r in out["rows"]] == ["0x" + "cd" * 32]
    assert out["excluded_started"][0]["provider_status"] == "ACTIVE" and "THIS_PAGE_ONLY" in out["scope"]


@pytest.mark.parametrize("data", [None, {"markets": "x"}, {}])
def test_markets_unexpected_shape_is_explicit(data):
    out = upcoming_filter(data, kind="markets", now_unix=PAST)["upcoming"]
    assert out["status"] == "UNEXPECTED_SHAPE" and out["rows"] is None


def test_tools_default_unchanged_and_opt_in_keeps_raw_data():
    t, calls, _ = make()
    plain = run(t["sxbet_fixtures_active"](league_id=1))
    assert "upcoming" not in plain and len(plain["data"]) == 2  # default output: as before
    out = run(t["sxbet_fixtures_active"](league_id=1, upcoming_only=True))
    assert out["status"] == "REAL_API_RESPONSE" and len(out["data"]) == 2  # raw rows still all there
    assert [r["eventId"] for r in out["upcoming"]["rows"]] == ["L1"]
    assert [r["id"] for r in out["upcoming"]["excluded_started"]] == ["L0"]
    m = run(t["sxbet_markets_active"](upcoming_only=True))
    assert m["upcoming"]["counts"]["upcoming"] == 1 and "upcomingOnly" not in str(calls[-1].url)
    assert all("upcoming" not in str(c.url).lower() for c in calls)  # local view, not a provider parameter


@pytest.mark.parametrize("tool,kwargs", [("sxbet_fixtures_active", {"league_id": 1, "upcoming_only": "yes"}),
                                         ("sxbet_markets_active", {"upcoming_only": 1})])
def test_upcoming_only_must_be_bool_before_network(tool, kwargs):
    t, calls, _ = make()
    out = run(t[tool](**kwargs))
    assert out["status"] == "INVALID_INPUT" and out["error"]["code"] == "INVALID_UPCOMING_ONLY" and calls == []


# ======================================================== 2+3. commission on profit, Decimal, rounding
def test_issue_187_reference_case_taker_one_percent_on_profit():
    out = pm.commission_breakdown(stake_base_units=1_000_000, return_base_units=1_878_000, is_maker=False,
                                  fees=FEES_1PCT)
    w = out["if_win"]
    assert (w["gross_profit_usdc"], w["fee_usdc_exact"], w["net_profit_usdc"], w["net_return_usdc"]) == (
        "0.878", "0.00878", "0.86922", "1.86922")
    assert (w["gross_profit_base_units"], w["fee_base_units"], w["net_profit_base_units"]) == (878_000, 8_780, 869_220)
    assert out["fee_basis"] == "PROFIT_OF_WINNING_BET" and out["provider_raw_rate"] == "0.01"
    assert out["if_lose"]["net_pnl_base_units"] == -1_000_000 and out["if_lose"]["fee_base_units"] == 0
    assert _floats(out) == []


def test_rate_comes_from_provider_never_hardcoded():
    two = dict(FEES_1PCT, takerPayoutFee="0.02")
    w = pm.commission_breakdown(stake_base_units=1_000_000, return_base_units=1_878_000, is_maker=False, fees=two)["if_win"]
    assert w["fee_base_units"] == 17_560 and w["net_profit_usdc"] == "0.86044"
    # parlay rate is not used for a single bet
    w = pm.commission_breakdown(stake_base_units=1_000_000, return_base_units=1_878_000, is_maker=False,
                                fees=dict(FEES_1PCT, takerParlayPayoutFee="0.5"))["if_win"]
    assert w["fee_base_units"] == 8_780


def test_maker_null_rate_is_documented_nothing_charged_raw_null_kept():
    out = pm.commission_breakdown(stake_base_units=1_000_000, return_base_units=1_878_000, is_maker=True, fees=FEES_1PCT)
    assert out["provider_raw_rate"] is None and out["derived_rate"] == "0"
    assert out["rate_note"] == "PROVIDER_NULL_UNSET_NOTHING_CHARGED" and out["if_win"]["net_profit_usdc"] == "0.878"


@pytest.mark.parametrize("fees,status", [
    (None, "FEE_UNRESOLVED_FEES_NOT_SUPPLIED"),
    ("x", "FEE_UNRESOLVED_FEES_MALFORMED"),
    ({k: v for k, v in FEES_1PCT.items() if k != "takerPayoutFee"}, "FEE_UNRESOLVED_FEES_MALFORMED"),
    (dict(FEES_1PCT, takerPayoutFee=0.01), "FEE_UNRESOLVED_FEES_MALFORMED"),   # float rate refused
    (dict(FEES_1PCT, takerPayoutFee="1.5"), "FEE_UNRESOLVED_FEES_MALFORMED"),
])
def test_missing_or_malformed_fee_is_null_never_zero(fees, status):
    w = pm.commission_breakdown(stake_base_units=1_000_000, return_base_units=1_878_000, is_maker=False, fees=fees)["if_win"]
    assert w["status"] == status
    for k in ("fee_exact", "fee_base_units", "net_profit_base_units", "net_profit_usdc", "net_return_base_units"):
        assert w[k] is None, k
    assert w["gross_profit_base_units"] == 878_000  # gross stays known


def test_fee_rounding_rule_is_conservative_and_exact_value_kept():
    # 1% of 333 base units = 3.33 -> conservative 4, exact "3.33"
    w = pm.commission_breakdown(stake_base_units=1_000, return_base_units=1_333, is_maker=False, fees=FEES_1PCT)["if_win"]
    assert w["fee_exact"] == "3.33" and w["fee_base_units"] == 4 and w["net_profit_base_units"] == 329
    assert w["net_profit_exact"] == "329.67" and "ROUND_CEILING" in pm.ROUNDING_RULE["conservative_fields"]
    assert "DOCS_UNVERIFIED" in pm.ROUNDING_RULE["exchange_rounding"]


def test_void_needs_refund_fee_semantics():
    ok = pm.commission_breakdown(stake_base_units=1_000_000, return_base_units=2_000_000, is_maker=False, fees=FEES_1PCT)
    assert ok["if_void"] == {"status": "OK", "gross_pnl_base_units": 0, "net_pnl_base_units": 0,
                             "provider_raw_refund_fee": None}
    unk = pm.commission_breakdown(stake_base_units=1_000_000, return_base_units=2_000_000, is_maker=False,
                                  fees=dict(FEES_1PCT, refundFee="0.001"))
    assert unk["if_void"]["status"] == "REFUND_FEE_DOCS_UNVERIFIED" and unk["if_void"]["net_pnl_base_units"] is None


@pytest.mark.parametrize("kw,code", [({"stake_base_units": 0}, "INVALID_STAKE_BASE_UNITS"),
                                     ({"stake_base_units": 1.0}, "INVALID_STAKE_BASE_UNITS"),
                                     ({"return_base_units": -1}, "INVALID_RETURN_BASE_UNITS"),
                                     ({"is_maker": "no"}, "INVALID_ROLE")])
def test_commission_rejects_bad_input(kw, code):
    args = dict(stake_base_units=1_000_000, return_base_units=2_000_000, is_maker=False, fees=FEES_1PCT)
    with pytest.raises(ValueError, match=code):
        pm.commission_breakdown(**{**args, **kw})


def _sim_args(**kw):
    a = dict(market_hash=H, betting_outcome_one=True, stake_base_units=2_000_000, percentage_odds=str(50 * P),
             time_in_force="IOC", expiry=int(time.time()) + 3600, idempotency_key="hard-key-0001")
    a.update(kw)
    return a


def test_simulate_order_fees_opt_in_and_unchanged_call_count_by_default():
    t, calls, _ = make()
    out = run(t["sxbet_simulate_order"](**_sim_args()))
    assert out["net_pnl"]["status"] == "FEES_NOT_REQUESTED" and out["net_pnl"]["taker_filled"] is None
    assert len(calls) == out["network_calls_made"] == 3 and "/user/fees-v3" not in [c.url.path for c in calls]


def test_simulate_order_net_pnl_partial_fill_taker_and_gtc_maker_projection():
    t, calls, _ = make(universal(fees=FEES_1PCT))
    out = run(t["sxbet_simulate_order"](**_sim_args(stake_base_units=5_000_000, time_in_force="GTC",
                                                     include_fees=True, check_balance=True)))
    assert out["status"] == "SIMULATED" and out["fill"]["fill"] == "PARTIAL"
    assert out["network_calls_made"] == len(calls) == 5 == TOOL_COST_CATALOG["sxbet_simulate_order"]["max_provider_calls"]
    n = out["net_pnl"]
    assert n["status"] == "OK" and n["provider_fees_raw"]["takerPayoutFee"] == "0.01"
    tk = n["taker_filled"]["if_win"]  # 3 USDC filled at 50% -> return 6, profit 3, fee 0.03
    assert (tk["gross_profit_usdc"], tk["fee_usdc_exact"], tk["net_profit_usdc"]) == ("3", "0.03", "2.97")
    mk = n["maker_resting_projection"]  # 2 USDC resting at 50% as maker, maker rate null -> 0
    assert mk["role"] == "MAKER" and mk["if_win"]["net_profit_usdc"] == "2" and mk["rate_note"]
    assert _floats(out) == [] and KEY not in json.dumps(out)
    assert out["signed"] is False and out["submitted"] is False


@pytest.mark.parametrize("fees_handler,key,status,reason", [
    (lambda r: httpx.Response(500, text="secret"), KEY, "FEES_UNRESOLVED", "SXBET_HTTP_ERROR"),
    (lambda r: _ok({"takerPayoutFee": "0.01"}), KEY, "FEES_UNRESOLVED", "FEES_MALFORMED"),
    (None, "", "FEES_UNRESOLVED", "SXBET_KEY_NOT_CONFIGURED"),
])
def test_simulate_order_fee_failures_are_explicit(fees_handler, key, status, reason):
    overrides = {"/user/fees-v3": fees_handler} if fees_handler else None
    t, _, _ = make(universal(overrides=overrides), key=key)
    out = run(t["sxbet_simulate_order"](**_sim_args(include_fees=True)))
    n = out["net_pnl"]
    assert n["status"] == status and n["reason"] == reason
    if reason == "FEES_MALFORMED":  # gross known, every net figure null (never 0)
        w = n["taker_filled"]["if_win"]
        assert w["gross_profit_base_units"] == 2_000_000 and w["net_profit_base_units"] is None and w["fee_base_units"] is None
    else:
        assert n["taker_filled"] is None
    assert "secret" not in json.dumps(out)


# ======================================================== 4. expiry diagnostics
def test_expiry_bounds_components():
    b = sim.expiry_bounds(8000, 1_000)
    assert b["min_expiry_unix"] == 1_000 + 8 + 2 + sim.EXPIRY_SAFETY_SECONDS and b["max_expiry_unix"] == 1_000 + 86_400
    assert b["min_is_lower_bound_only"] is False and b["components"]["betting_delay_seconds_rounded_up"] == 8
    assert sim.expiry_bounds(8001, 0)["components"]["betting_delay_seconds_rounded_up"] == 9
    lb = sim.expiry_bounds(None, 1_000)
    assert lb["min_is_lower_bound_only"] is True and lb["min_expiry_unix"] == 1_000 + 2 + sim.EXPIRY_SAFETY_SECONDS


def test_build_intent_refuses_unknown_betting_delay():
    meta = {**sim.parse_metadata(META, "mainnet")}
    meta.pop("max_betting_delay_ms")
    with pytest.raises(sim.IntentError, match="METADATA_BETTING_DELAY_MISSING"):
        sim.build_intent(network="mainnet", metadata=meta, market_hash=H, betting_outcome_one=True,
                         stake_base_units=1_000_000, requested_percentage_odds=50 * P, time_in_force="IOC",
                         expiry=10_000, idempotency_key="k" * 8, now=1_000)


@pytest.mark.parametrize("delta,code", [(-60, "EXPIRY_TOO_SOON"), (5, "EXPIRY_TOO_SOON"), (90_000, "EXPIRY_TOO_FAR")])
def test_expiry_outside_any_window_refused_before_network(delta, code):
    t, calls, _ = make()
    out = run(t["sxbet_simulate_order"](**_sim_args(expiry=int(time.time()) + delta)))
    assert out["status"] == "INVALID_INPUT" and out["error"]["code"] == code and calls == []
    d = out["expiry_diagnostics"]
    assert d["min_is_lower_bound_only"] is True and abs(d["requested_expiry_unix"] - (d["now_unix"] + delta)) <= 2
    assert {"min_expiry_unix", "max_expiry_unix", "components", "note"} <= set(d)


def test_expiry_inside_betting_delay_refused_with_full_diagnostics_after_one_read():
    t, calls, _ = make()
    exp = int(time.time()) + 15  # above the lower bound (12 s) but below 8 s delay + 2 + 10
    out = run(t["sxbet_simulate_order"](**_sim_args(expiry=exp)))
    assert out["error"]["code"] == "EXPIRY_TOO_SOON" and [c.url.path for c in calls] == ["/metadata/obv3"]
    d = out["expiry_diagnostics"]
    assert d["min_is_lower_bound_only"] is False and d["components"]["max_betting_delay_ms"] == 8000
    assert d["min_expiry_unix"] - d["now_unix"] == 20 and d["requested_expiry_unix"] == exp


# ======================================================== 5. declared max cost per tool
def test_cost_catalog_covers_exactly_the_registered_tools_and_is_in_health():
    t, _, names = make()
    assert set(TOOL_COST_CATALOG) == set(names) and len(names) == 46
    assert all(type(v["max_provider_calls"]) is int and v["max_provider_calls"] >= 0 and v["basis"]
               for v in TOOL_COST_CATALOG.values())
    h = run(t["sxbet_health"]())
    assert h["tool_cost_catalog"]["tools"] == TOOL_COST_CATALOG and h["network_calls_made"] == 0


# ======================================================== 6. hedge / cash-out formulas with fees (SIMULATION)
def _hedge(**kw):
    position = {"market_hash": H, "data_conflict": False,
                "pnl_base_units": {"if_outcome_one": 8_780_000, "if_outcome_two": -10_000_000},
                "projected_payout_fee_base_units": {"if_outcome_one": "87800", "if_outcome_two": "0"}}
    book = {"marketHash": H, "outcomeOne": [{"percentageOdds": str(50 * P), "size": "50000000"}], "outcomeTwo": []}
    args = dict(market_position=position, book=book, market={"marketHash": H, "status": "ACTIVE"},
                fees=dict(FEES_1PCT, makerPayoutFee="0.01"), data_age_seconds=0.5, max_data_age_seconds=10,
                odds_ladder_step_size=125, min_order_base_units=1_000_000)
    args.update(kw)
    return hm.simulate_hedge(**args)


def test_full_hedge_equalises_outcomes_and_applies_fees_on_profit_only():
    # 10 USDC on outcome one at decimal 1.878: +8.78 / -10. Back outcome two at 50% (decimal 2.0):
    # stake 9.39 returns 18.78 -> both outcomes -0.61 before fees.
    out = _hedge()
    assert out["status"] == hm.AVAILABLE and out["hedge_outcome"] == "outcome_two"
    assert out["hedge_stake_base_units"] == "9390000" and out["hedge_return_base_units"] == "18780000"
    assert out["after"]["pnl_base_units"] == {"if_outcome_one": -610_000, "if_outcome_two": -610_000}
    f = out["fees"]
    assert f["hedge_taker_payout_fee_if_hedge_wins_base_units"] == "93900"  # 1% of the hedge profit 9.39
    assert f["pnl_after_projected_fees_base_units"] == {"if_outcome_one": "-697800", "if_outcome_two": "-703900"}
    assert f["worst_case_after_projected_fees_base_units"] == "-703900" and f["rounding_rule"] is pm.HEDGE_ROUNDING_RULE
    assert [p for p in _floats(out) if not _ALLOWED_FLOAT.search(p)] == []


def test_hedge_without_fee_data_is_uncertain_and_net_is_null():
    out = _hedge(fees=None)
    assert out["status"] == hm.UNCERTAIN and "FEE_UNVERIFIABLE_FEES_NOT_SUPPLIED" in out["uncertain_reasons"]
    assert out["fees"]["pnl_after_projected_fees_base_units"] == {"if_outcome_one": None, "if_outcome_two": None}
    assert out["fees"]["worst_case_after_projected_fees_base_units"] is None


@pytest.mark.parametrize("kw,reason", [
    ({"market": {"marketHash": H, "status": "INACTIVE"}}, "MARKET_NOT_ACTIVE"),
    ({"book": {"marketHash": H, "outcomeOne": []}}, "NO_LIQUIDITY"),
    ({"book": {"marketHash": H}}, "ORDER_BOOK_SIDE_MISSING"),
    ({"book": None}, "ORDER_BOOK_MISSING"),
    ({"market_position": None}, "POSITION_DATA_MISSING"),
    ({"min_order_base_units": 10_000_000}, "HEDGE_STAKE_BELOW_MIN_ORDER"),
])
def test_hedge_edge_cases_fixed_codes_no_fake_price(kw, reason):
    out = _hedge(**kw)
    assert out["status"] == hm.UNAVAILABLE and out["reason"] == reason
    assert "after" not in out or out["after"] is None or reason == "HEDGE_STAKE_BELOW_MIN_ORDER"
    assert out.get("effective_taker_percentage_odds") is None and out["signed"] is False and out["submitted"] is False


# ======================================================== 7. simulation edge cases
def _intent(**kw):
    a = dict(network="mainnet", metadata=sim.parse_metadata(META, "mainnet"), market_hash=H, betting_outcome_one=True,
             stake_base_units=5_000_000, requested_percentage_odds=40 * P, time_in_force="IOC", expiry=10_000,
             idempotency_key="edge-key-001", now=1_000)
    a.update(kw)
    return sim.build_intent(**a)


@pytest.mark.parametrize("levels,status", [
    ([], "NO_LIQUIDITY"),
    ([(41 * P, 9_000_000)], "NO_LIQUIDITY_AT_OR_BETTER_THAN_LIMIT"),
    ([(39 * P, 1_000_000)], "INSUFFICIENT_LIQUIDITY"),
    ([(39 * P, 9_000_000)], "SUFFICIENT_LIQUIDITY"),
])
def test_fill_liquidity_status_codes(levels, status):
    book = {"marketHash": H, "outcomeOne": [{"percentageOdds": str(p), "size": str(s)} for p, s in levels]}
    out = sim.simulate_fill(_intent(), book, min_resting_base_units=100_000)
    assert out["liquidity_status"] == status
    if status != "SUFFICIENT_LIQUIDITY" and out["fill"] == "NONE":
        assert out["effective_percentage_odds"] is None and out["potential_return_base_units"] is None


@pytest.mark.parametrize("market,code", [
    ({"outcomeOneName": "A"}, "MARKET_OUTCOMES_MISSING"),
    ({"outcomeOneName": "A", "outcomeTwoName": " "}, "MARKET_OUTCOMES_MISSING"),
    (None, "MARKET_DATA_MISSING"),
])
def test_check_outcomes(market, code):
    assert sim.check_outcomes(market) == code
    assert sim.check_outcomes({"outcomeOneName": "A", "outcomeTwoName": "B"}) is None


def test_simulate_order_refuses_market_without_outcome_names_and_skips_book():
    def find(request):
        return _ok([{"marketHash": H, "status": "ACTIVE", "outcomeOneName": "A"}])
    t, calls, _ = make(universal(overrides={"/markets/find": find}))
    out = run(t["sxbet_simulate_order"](**_sim_args()))
    assert out["status"] == "REFUSED" and out["error"]["code"] == "MARKET_OUTCOMES_MISSING"
    assert [c.url.path for c in calls] == ["/metadata/obv3", "/markets/find"]


def test_simulate_order_suspended_market_refused():
    t, calls, _ = make(universal(overrides={"/markets/find": lambda r: _ok(
        [{"marketHash": H, "status": "INACTIVE", "outcomeOneName": "A", "outcomeTwoName": "B"}])}))
    out = run(t["sxbet_simulate_order"](**_sim_args()))
    assert out["error"]["code"] == "MARKET_INACTIVE" and out["fill"]["status"] == "NOT_SIMULATABLE"
    assert out["market"]["market"]["status"] == "INACTIVE"  # raw provider status kept


# ======================================================== 8. regression over ALL sxbet_* tools
SIGNATURES = {
    "sxbet_balance": "",
    "sxbet_best_odds": "market_hashes, show_taker_perspective=None",
    "sxbet_close_position_preview": "market_hash, max_slippage_percentage_odds='0', max_data_age_seconds=10",
    "sxbet_exposure_pnl": "event_id=None",
    "sxbet_fees": "",
    "sxbet_fills": "trade_id=None, order_id=None, status=None, start_date=None, end_date=None, sort_asc=None, per_page=None, next_key=None",
    "sxbet_fixture_status": "sportx_event_ids",
    "sxbet_fixtures_active": "league_id, upcoming_only=False",
    "sxbet_health": "",
    "sxbet_hedge_simulation": "market_hash, mode='FULL', fraction=None, hedge_stake_base_units=None, max_slippage_percentage_odds='0', max_data_age_seconds=10",
    "sxbet_leagues": "sport_id=None, active_only=True",
    "sxbet_ledger": "event_type=None, per_page=None, next_key=None",
    "sxbet_live_scores": "sportx_event_ids",
    "sxbet_markets_active": "sport_ids=None, league_id=None, event_id=None, only_main_line=None, live_only=None, bet_group=None, type=None, page_size=None, pagination_key=None, game_time=None, upcoming_only=False",
    "sxbet_markets_find": "market_hashes",
    "sxbet_markets_popular": "",
    "sxbet_metadata": "",
    "sxbet_open_orders": "market_hash=None, event_id=None, sort_by=None, sort_asc=None, per_page=None, next_key=None",
    "sxbet_order": "order_id",
    "sxbet_order_by_client_id": "client_order_id",
    "sxbet_order_status": "order_id",
    "sxbet_orderbook_event": "event_id, next_key=None, show_taker_perspective=None",
    "sxbet_orderbook_market": "market_hash, show_taker_perspective=None",
    "sxbet_positions": "status, event_id=None, sort_by=None, sort_asc=None, per_page=None, next_key=None",
    "sxbet_positions_summary": "event_id=None",
    "sxbet_privacy": "",
    "sxbet_proxy_address_check": "proxy_address",
    "sxbet_proxy_deploy_pending": "",
    "sxbet_proxy_funding_pending": "",
    "sxbet_proxy_funding_status": "session_id",
    "sxbet_proxy_wallet": "",
    "sxbet_public_market_orders": "market_hash, per_page=None, next_key=None",
    "sxbet_public_positions": "user_address, status, event_id=None, sort_by=None, sort_asc=None, per_page=None, next_key=None",
    "sxbet_public_trades": "market_hash=None, event_id=None, per_page=None, next_key=None",
    "sxbet_public_user_orders": "user_address, per_page=None, next_key=None",
    "sxbet_reconciliation_diagnostics": "market_hash=None, ws_messages=None",
    "sxbet_resting_orders_scope": "order_ids=None, market_hash=None, event_id=None",
    "sxbet_risk_status": "",
    "sxbet_rules_evaluate": "market_hash, rules=None, max_slippage_percentage_odds='0'",
    "sxbet_safe_info": "safe_address",
    "sxbet_search": "query",
    "sxbet_simulate_order": "market_hash, betting_outcome_one, stake_base_units, percentage_odds, time_in_force, expiry, idempotency_key, maker_address=None, check_balance=False, include_fees=False",
    "sxbet_sports": "",
    "sxbet_teams": "sport_id=None, id=None, page=None, per_page=None",
    "sxbet_trade": "trade_id",
    "sxbet_trades": "market_hash=None, status=None, start_date=None, end_date=None, sort_asc=None, per_page=None, next_key=None",
}

VALID = {
    "sxbet_balance": {}, "sxbet_best_odds": {"market_hashes": [H]},
    "sxbet_close_position_preview": {"market_hash": H}, "sxbet_exposure_pnl": {}, "sxbet_fees": {},
    "sxbet_fills": {"order_id": OID}, "sxbet_fixture_status": {"sportx_event_ids": ["L1"]},
    "sxbet_fixtures_active": {"league_id": 1, "upcoming_only": True}, "sxbet_health": {},
    "sxbet_hedge_simulation": {"market_hash": H}, "sxbet_leagues": {"sport_id": 5}, "sxbet_ledger": {},
    "sxbet_live_scores": {"sportx_event_ids": ["L1"]}, "sxbet_markets_active": {"upcoming_only": True},
    "sxbet_markets_find": {"market_hashes": [H]}, "sxbet_markets_popular": {}, "sxbet_metadata": {},
    "sxbet_open_orders": {}, "sxbet_order": {"order_id": OID}, "sxbet_order_by_client_id": {"client_order_id": "c-1"},
    "sxbet_order_status": {"order_id": OID}, "sxbet_orderbook_event": {"event_id": "L1"},
    "sxbet_orderbook_market": {"market_hash": H}, "sxbet_positions": {"status": ["MATCHED"]},
    "sxbet_positions_summary": {}, "sxbet_privacy": {}, "sxbet_proxy_address_check": {"proxy_address": ADDR},
    "sxbet_proxy_deploy_pending": {}, "sxbet_proxy_funding_pending": {},
    "sxbet_proxy_funding_status": {"session_id": "s-1"}, "sxbet_proxy_wallet": {},
    "sxbet_public_market_orders": {"market_hash": H}, "sxbet_public_positions": {"user_address": ADDR, "status": ["SETTLED"]},
    "sxbet_public_trades": {}, "sxbet_public_user_orders": {"user_address": ADDR},
    "sxbet_reconciliation_diagnostics": {}, "sxbet_resting_orders_scope": {"market_hash": H}, "sxbet_risk_status": {},
    "sxbet_rules_evaluate": {"market_hash": H}, "sxbet_safe_info": {"safe_address": ADDR}, "sxbet_search": {"query": "inter"},
    "sxbet_simulate_order": {**_sim_args(check_balance=True, include_fees=True), "expiry": None},  # expiry set at run
    "sxbet_sports": {}, "sxbet_teams": {}, "sxbet_trade": {"trade_id": "t-1"}, "sxbet_trades": {},
}
# One invalid input per tool that takes any argument: refused with a fixed code before any network call.
INVALID = {
    "sxbet_best_odds": {"market_hashes": ["0x1"]}, "sxbet_close_position_preview": {"market_hash": "x"},
    "sxbet_exposure_pnl": {"event_id": "../x"}, "sxbet_fills": {"per_page": 0},
    "sxbet_fixture_status": {"sportx_event_ids": []}, "sxbet_fixtures_active": {"league_id": 0},
    "sxbet_hedge_simulation": {"market_hash": H, "mode": "ALL"}, "sxbet_leagues": {"sport_id": -1},
    "sxbet_ledger": {"event_type": "TRANSFER"}, "sxbet_live_scores": {"sportx_event_ids": ["a b"]},
    "sxbet_markets_active": {"page_size": 0}, "sxbet_markets_find": {"market_hashes": []},
    "sxbet_open_orders": {"sort_by": "x"}, "sxbet_order": {"order_id": "1"},
    "sxbet_order_by_client_id": {"client_order_id": "a/b"}, "sxbet_order_status": {"order_id": "1"},
    "sxbet_orderbook_event": {"event_id": ""}, "sxbet_orderbook_market": {"market_hash": "1"},
    "sxbet_positions": {"status": ["OPEN"]}, "sxbet_positions_summary": {"event_id": "/"},
    "sxbet_proxy_address_check": {"proxy_address": "0x1"}, "sxbet_proxy_funding_status": {"session_id": "../"},
    "sxbet_public_market_orders": {"market_hash": "1"}, "sxbet_public_positions": {"user_address": ADDR, "status": []},
    "sxbet_public_trades": {"per_page": 101}, "sxbet_public_user_orders": {"user_address": "x"},
    "sxbet_reconciliation_diagnostics": {"market_hash": "1"}, "sxbet_resting_orders_scope": {},
    "sxbet_rules_evaluate": {"market_hash": H, "rules": "x"}, "sxbet_safe_info": {"safe_address": "x"},
    "sxbet_search": {"query": ""}, "sxbet_simulate_order": {**_sim_args(), "percentage_odds": "x"},
    "sxbet_teams": {"per_page": 0}, "sxbet_trade": {"trade_id": "../x"}, "sxbet_trades": {"status": "OPEN"},
}
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
_ALLOWED_STATUS = {"REAL_API_RESPONSE", "DERIVED_FROM_REAL_API", "PARTIAL", "SIMULATED", "NO_ACTION", "ALERT",
                   "ORDER_PROPOSAL", hm.AVAILABLE, hm.PARTIAL, hm.UNAVAILABLE, hm.UNCERTAIN, None}


def _sig(fn):
    return ", ".join(p.name if p.default is inspect.Parameter.empty else f"{p.name}={p.default!r}"
                     for p in inspect.signature(fn).parameters.values())


def test_every_tool_schema_is_pinned():
    t, _, names = make()
    assert sorted(names) == sorted(SIGNATURES) and len(names) == 46
    assert {n: _sig(t[n]) for n in names} == SIGNATURES
    assert set(VALID) == set(SIGNATURES) and set(INVALID) == {n for n, s in SIGNATURES.items() if s}


def _walk_flags(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk_flags(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_flags(v)


def _assert_safe(name, out, calls):
    text = json.dumps(out, default=str)
    assert KEY not in text, name
    assert all(c.method == "GET" and c.url.host == "api.sx.bet" for c in calls), name
    for k, v in _walk_flags(out):
        if k in ("signed", "submitted", "executable", "cancel_sent", "real_cancel_sent", "guaranteed_cashout"):
            assert v is False, (name, k)
        if k in ("live_trading", "LIVE_TRADING"):
            assert str(v).startswith("UNAVAILABLE"), (name, k, v)
        if k == "write_tools":
            assert v == [], name
    err = out.get("error") if isinstance(out, dict) else None
    if err:
        assert _CODE.fullmatch(err["code"]), (name, err)


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_every_tool_runs_offline_safely_within_declared_cost(name):
    t, calls, _ = make(universal(fees=FEES_1PCT))
    kwargs = dict(VALID[name])
    if name == "sxbet_simulate_order":
        kwargs["expiry"] = int(time.time()) + 3600
    out = run(t[name](**kwargs))
    _assert_safe(name, out, calls)
    assert out.get("status") in _ALLOWED_STATUS or name in ("sxbet_health",), (name, out.get("status"), out.get("error"))
    assert out["network_calls_made"] == len(calls) <= TOOL_COST_CATALOG[name]["max_provider_calls"], name
    if name in ("sxbet_positions_summary", "sxbet_exposure_pnl", "sxbet_simulate_order", "sxbet_order_status",
                "sxbet_resting_orders_scope", "sxbet_hedge_simulation", "sxbet_close_position_preview",
                "sxbet_rules_evaluate", "sxbet_reconciliation_diagnostics"):
        # The declared maximum is tight: the full-success path reaches it.
        assert len(calls) == TOOL_COST_CATALOG[name]["max_provider_calls"], (name, [c.url.path for c in calls])


@pytest.mark.parametrize("name", sorted(INVALID))
def test_every_tool_refuses_invalid_input_with_fixed_code_before_network(name):
    t, calls, _ = make()
    out = run(t[name](**INVALID[name]))
    assert out["status"] == "INVALID_INPUT" and _CODE.fullmatch(out["error"]["code"]), (name, out)
    assert calls == [] and out["network_calls_made"] == 0, name
    _assert_safe(name, out, calls)


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_no_key_means_no_authenticated_call(name):
    t, calls, _ = make(universal(fees=FEES_1PCT), key="")
    kwargs = dict(VALID[name])
    if name == "sxbet_simulate_order":
        kwargs["expiry"] = int(time.time()) + 3600
    out = run(t[name](**kwargs))
    assert all("x-sx-api-key" not in c.headers for c in calls), name
    _assert_safe(name, out, calls)


def test_money_outputs_have_no_float_across_pnl_tools():
    t, _, _ = make(universal(fees=FEES_1PCT))
    for name in ("sxbet_positions_summary", "sxbet_exposure_pnl", "sxbet_hedge_simulation",
                 "sxbet_close_position_preview", "sxbet_rules_evaluate", "sxbet_simulate_order"):
        kwargs = dict(VALID[name])
        if name == "sxbet_simulate_order":
            kwargs["expiry"] = int(time.time()) + 3600
        out = run(t[name](**kwargs))
        assert [p for p in _floats(out) if not _ALLOWED_FLOAT.search(p)] == [], name


def test_no_live_trading_path_is_reachable_from_any_tool():
    # Structural: no tool takes a mode/live/sign/key argument and the module has no submit/sign capability.
    for name, sig in SIGNATURES.items():
        assert not re.search(r"\b(mode=\'LIVE|live_trading|private_key|signature|sign\b|submit|api_key)", sig), name
    src = inspect.getsource(sx_tools)
    assert "import signer" not in src and "from signer" not in src
    t, _, _ = make()
    assert run(t["sxbet_risk_status"]())["live_trading"] == "UNAVAILABLE"
    assert run(t["sxbet_health"]())["available_modes"]["LIVE_TRADING"] == "UNAVAILABLE"


def test_review_p2_hedge_rounding_rule_declares_exact_unrounded_fees():
    """Review P2-a: the hedge fee values are exact Decimals, so the rule attached to them must not claim a ceiling."""
    import inspect
    from bridge.sxbet import hedging, positions
    src = inspect.getsource(hedging)
    assert '"rounding_rule": pm.HEDGE_ROUNDING_RULE' in src and '"rounding_rule": pm.ROUNDING_RULE' not in src
    rule = positions.HEDGE_ROUNDING_RULE
    assert "NOT rounded" in rule["hedge_fee_fields"] and "CEILING" not in str(rule)
    assert "DOCS_UNVERIFIED" in rule["exchange_rounding"]


def test_review_p2_fee_rate_scale_is_declared_verified_from_official_contract():
    """Review P2-b, updated INTENTIONALLY (issue #187 D9, 12-11-2026): the fraction scale was DOCS_UNVERIFIED;
    api-reference/get-user-fees-v3 now states "Every rate is a decimal fraction string, so "0.05" is 5%".
    The scale moves from the unverified list to a verified statement citing that page; nothing else changes."""
    from bridge.sxbet import positions
    assert not any(item.startswith("FEE_RATE_SCALE:") for item in positions.COMMISSION_DOCS_UNVERIFIED)
    assert "VERIFIED" in positions.FEE_RATE_SCALE_VERIFIED
    assert "api-reference/get-user-fees-v3" in positions.FEE_RATE_SCALE_VERIFIED
    assert "per fill" in positions.ROUNDING_RULE["conservative_fields"]
