"""SX Bet SIMULATION: offline tests (pure functions + MCP tool over httpx.MockTransport). No signing, no key."""
import ast
import asyncio
import json
import pathlib
import time

import httpx
import pytest

from bridge.sxbet import simulation as sim
from bridge.sxbet.client import SXBetReadClient, Settings
from bridge.sxbet.tools import register_sxbet_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = "FAKE_SX_TEST_KEY_DO_NOT_LOG"
H = "0x" + "ab" * 32
ESCROW = "0x" + "0d" * 20
TOKEN = "0x" + "1b" * 20
MAKER = "0x" + "4c" * 20
NOW = 1_800_000_000
META_RAW = {
    "chainId": 4162,
    "domain": {"name": "OBv3 Escrow", "version": "1", "chainId": 4162, "verifyingContract": ESCROW},
    "activeAsset": {"symbol": "USDC", "baseToken": TOKEN, "escrowAddress": ESCROW, "decimals": 6},
    "oddsLadderStepSize": 125,
    "limits": {"orderSizeMinimum": "1", "orderSizeMinimumBaseUnits": "1000000", "minRestingOrderSize": "0.1",
               "minRestingOrderSizeBaseUnits": "100000", "maxCreateOrders": 10, "maxCancelOrders": 100},
    "bettingDelay": {"liveMsDefault": 8000, "pregameMsDefault": 0, "sportIdToMs": {}, "leagueIdToMs": {}},
}
META = sim.parse_metadata(META_RAW, "mainnet")
P40 = 40 * 10 ** 18


def intent(**kw):
    args = dict(network="mainnet", metadata=META, market_hash=H, betting_outcome_one=True,
                stake_base_units=5_000_000, requested_percentage_odds=P40, time_in_force="IOC",
                expiry=NOW + 3600, idempotency_key="sim-key-0001", now=NOW, maker=MAKER, salt=12345)
    args.update(kw)
    return sim.build_intent(**args)


def book(one=(), two=()):
    lv = lambda levels: [{"percentageOdds": str(p), "size": str(s)} for p, s in levels]
    return {"marketHash": H, "outcomeOne": lv(one), "outcomeTwo": lv(two), "version": "1"}


# ---------------------------------------------------------------- intent validation
def test_valid_intent_is_canonical_and_hash_is_stable():
    a, b = intent(), intent()
    assert a == b and sim.intent_hash(a) == sim.intent_hash(b)
    assert sim.intent_hash(a).startswith("sha256:") and len(sim.intent_hash(a)) == 71
    assert a["total_bet_size"] == "5000000" and a["percentage_odds"] == str(P40) and sim.signing_ready(a)
    assert a["verifying_contract"] == ESCROW and a["base_token"] == TOKEN and a["chain_id"] == 4162


def test_odds_round_down_preserves_raw_and_changes_hash():
    raw = 40_100_000_000_000_000_000  # 40.100% -> off ladder (step 0.125%)
    i = intent(requested_percentage_odds=raw)
    assert i["percentage_odds"] == "40000000000000000000" and i["requested_percentage_odds"] == str(raw)
    assert sim.intent_hash(i) != sim.intent_hash(intent())


@pytest.mark.parametrize("odds", [10 ** 17, 999 * 10 ** 17])
def test_ladder_exceptions_kept(odds):
    assert intent(requested_percentage_odds=odds)["percentage_odds"] == str(odds)


@pytest.mark.parametrize("kw,code", [
    ({"market_hash": "0xabc"}, "INVALID_MARKET_HASH"),
    ({"betting_outcome_one": "yes"}, "INVALID_SIDE"),
    ({"stake_base_units": 999_999}, "STAKE_BELOW_MINIMUM"),
    ({"stake_base_units": 1.5}, "INVALID_STAKE_BASE_UNITS"),
    ({"stake_base_units": True}, "INVALID_STAKE_BASE_UNITS"),
    ({"requested_percentage_odds": 0}, "ODDS_OUT_OF_BOUNDS"),
    ({"requested_percentage_odds": 10 ** 20}, "ODDS_OUT_OF_BOUNDS"),
    ({"requested_percentage_odds": 10 ** 16}, "ODDS_NOT_ON_LADDER_AFTER_ROUND_DOWN"),  # rounds to 0
    ({"requested_percentage_odds": "40"}, "INVALID_PERCENTAGE_ODDS"),
    ({"time_in_force": "GTD"}, "INVALID_TIME_IN_FORCE"),
    ({"expiry": NOW + 5}, "EXPIRY_TOO_SOON"),  # inside betting delay (8 s) + 2 s
    ({"expiry": NOW + 10 ** 6}, "EXPIRY_TOO_FAR"),
    ({"expiry": "soon"}, "INVALID_EXPIRY"),
    ({"idempotency_key": "short"}, "INVALID_IDEMPOTENCY_KEY"),
    ({"idempotency_key": "has space here"}, "INVALID_IDEMPOTENCY_KEY"),
    ({"maker": "0x123"}, "INVALID_MAKER"),
    ({"salt": -1}, "INVALID_SALT"),
    ({"network": "devnet"}, "INVALID_NETWORK"),
])
def test_malformed_intent_refused_with_fixed_code(kw, code):
    with pytest.raises(sim.IntentError) as e:
        intent(**kw)
    assert str(e.value) == code


@pytest.mark.parametrize("mutate,code", [
    (lambda m: m.update(chainId=1), "METADATA_CHAIN_INCONSISTENT"),
    (lambda m: m["domain"].update(name="Other"), "METADATA_DOMAIN_UNEXPECTED"),
    (lambda m: m["activeAsset"].update(escrowAddress="0x" + "99" * 20), "METADATA_ESCROW_MISMATCH"),
    (lambda m: m["activeAsset"].update(decimals=18), "METADATA_DECIMALS_UNEXPECTED"),
    (lambda m: m.update(oddsLadderStepSize=0), "METADATA_LADDER_STEP_INVALID"),
    (lambda m: m.pop("bettingDelay"), "METADATA_BETTING_DELAY_MISSING"),
    (lambda m: m.pop("limits"), "METADATA_INCOMPLETE"),
])
def test_metadata_cross_checks(mutate, code):
    m = json.loads(json.dumps(META_RAW))
    mutate(m)
    with pytest.raises(sim.IntentError) as e:
        sim.parse_metadata(m, "mainnet")
    assert str(e.value) == code


def test_metadata_chain_must_match_network():
    with pytest.raises(sim.IntentError) as e:
        sim.parse_metadata(META_RAW, "testnet")
    assert str(e.value) == "METADATA_CHAIN_NOT_EXPECTED_FOR_NETWORK"


def test_zero_default_limits_refuse_live_but_simulation_reports():
    out = sim.live_eligibility(intent(), sim.ExposureLimits())
    assert out["eligible"] is False and "EXPOSURE_LIMIT_ZERO" in out["reasons"]
    ok = sim.live_eligibility(intent(), sim.ExposureLimits(10_000_000, 20_000_000))
    assert ok["eligible"] is True
    over = sim.live_eligibility(intent(), sim.ExposureLimits(10_000_000, 20_000_000), current_exposure_base_units=16_000_000)
    assert over["reasons"] == ["EXPOSURE_ABOVE_LIMIT"]


# ---------------------------------------------------------------- market
def test_market_exact_match_inactive_and_missing():
    assert sim.check_market([{"marketHash": H.upper().replace("0X", "0x"), "status": "ACTIVE"}], H)["status"] == "MARKET_ACTIVE"
    assert sim.check_market([{"marketHash": H, "status": "INACTIVE"}], H)["status"] == "MARKET_INACTIVE"
    assert sim.check_market([{"marketHash": "0x" + "cd" * 32, "status": "ACTIVE"}], H)["status"] == "MARKET_NOT_FOUND"
    assert sim.check_market([{"marketHash": H, "status": "ACTIVE"}] * 2, H)["status"] == "MARKET_AMBIGUOUS"
    assert sim.check_market(None, H)["status"] == "MARKET_DATA_MISSING"


# ---------------------------------------------------------------- fill simulation
def test_full_fill_walks_cheapest_levels_first():
    b = book(one=[(38 * 10 ** 18, 3_000_000), (35 * 10 ** 18, 1_000_000), (39 * 10 ** 18, 5_000_000), (45 * 10 ** 18, 9_000_000)])
    out = sim.simulate_fill(intent(), b, min_resting_base_units=100_000)
    assert out["status"] == "SIMULATED" and out["fill"] == "FULL" and out["label"] == "DERIVED_SIMULATION"
    assert [l["percentage_odds"] for l in out["legs"]] == [str(35 * 10 ** 18), str(38 * 10 ** 18), str(39 * 10 ** 18)]
    assert [l["stake_base_units"] for l in out["legs"]] == ["1000000", "3000000", "1000000"]
    assert out["unfilled_base_units"] == "0" and out["levels_eligible"] == 3  # 45% is worse than the 40% limit
    eff = int(out["effective_percentage_odds"])
    assert 35 * 10 ** 18 < eff < 39 * 10 ** 18


def test_ioc_partial_fill_discards_remainder():
    out = sim.simulate_fill(intent(), book(one=[(39 * 10 ** 18, 2_000_000)]), min_resting_base_units=100_000)
    assert out["fill"] == "PARTIAL" and out["filled_base_units"] == "2000000"
    assert out["unfilled_base_units"] == "3000000" and out["discarded_base_units"] == "3000000" and out["resting_base_units"] == "0"


def test_fok_insufficient_liquidity_fills_nothing():
    out = sim.simulate_fill(intent(time_in_force="FOK"), book(one=[(39 * 10 ** 18, 2_000_000)]), min_resting_base_units=100_000)
    assert out["fill"] == "NONE" and out["fok_killed"] is True and out["legs"] == [] and out["filled_base_units"] == "0"


def test_gtc_remainder_rests_unless_dust():
    out = sim.simulate_fill(intent(time_in_force="GTC"), book(one=[(39 * 10 ** 18, 2_000_000)]), min_resting_base_units=100_000)
    assert out["resting_base_units"] == "3000000" and out["discarded_base_units"] == "0"
    dust = sim.simulate_fill(intent(time_in_force="GTC"), book(one=[(39 * 10 ** 18, 4_950_000)]), min_resting_base_units=100_000)
    assert dust["resting_base_units"] == "0" and dust["discarded_base_units"] == "50000"


def test_no_liquidity_and_other_side_not_used():
    out = sim.simulate_fill(intent(), book(two=[(10 * 10 ** 18, 99_000_000)]), min_resting_base_units=100_000)
    assert out["fill"] == "NONE" and out["effective_percentage_odds"] is None and out["side_read"] == "outcomeOne"
    two = sim.simulate_fill(intent(betting_outcome_one=False), book(two=[(10 * 10 ** 18, 99_000_000)]), min_resting_base_units=100_000)
    assert two["fill"] == "FULL" and two["side_read"] == "outcomeTwo"


def test_price_moved_above_limit_means_no_fill():
    out = sim.simulate_fill(intent(), book(one=[(41 * 10 ** 18, 99_000_000)]), min_resting_base_units=100_000)
    assert out["fill"] == "NONE"


@pytest.mark.parametrize("b,reason", [
    (None, "ORDER_BOOK_MISSING"),
    ({"marketHash": H}, "ORDER_BOOK_SIDE_MISSING"),
    ({"marketHash": H, "outcomeOne": [{"percentageOdds": "abc", "size": "1"}]}, "BOOK_MALFORMED"),
    ({"marketHash": H, "outcomeOne": [{"percentageOdds": str(P40)}]}, "BOOK_MALFORMED"),
    ({"marketHash": "0x" + "cd" * 32, "outcomeOne": []}, "BOOK_MARKET_MISMATCH"),
])
def test_missing_or_malformed_book_is_not_simulatable(b, reason):
    out = sim.simulate_fill(intent(), b, min_resting_base_units=100_000)
    assert out["status"] == "NOT_SIMULATABLE" and out["reason"] == reason and "fill" not in out


# ---------------------------------------------------------------- balance
def test_balance_sufficient_insufficient_and_unresolved():
    row = {"tokenAddress": TOKEN.upper().replace("0X", "0x"), "escrowAddress": ESCROW, "availableAmount": "4000000"}
    assert sim.check_balance({"balances": [row]}, base_token=TOKEN, escrow=ESCROW, stake=5_000_000)["status"] == "INSUFFICIENT_BALANCE"
    assert sim.check_balance({"balances": [row]}, base_token=TOKEN, escrow=ESCROW, stake=4_000_000)["status"] == "SUFFICIENT"
    # Missing stays missing: no row is not a zero balance.
    empty = sim.check_balance({"balances": []}, base_token=TOKEN, escrow=ESCROW, stake=1)
    assert empty["status"] == "BALANCE_UNRESOLVED" and "available_base_units" not in empty
    bad = sim.check_balance({"balances": [{**row, "availableAmount": None}]}, base_token=TOKEN, escrow=ESCROW, stake=1)
    assert bad["status"] == "BALANCE_UNRESOLVED"


# ---------------------------------------------------------------- cancel
def test_simulated_cancel():
    oid, other = "0x" + "11" * 32, "0x" + "22" * 32
    out = sim.simulate_cancel([oid, other], [{"id": oid, "status": "ACTIVE"}], max_cancel_orders=100)
    assert out["would_cancel"] == [oid] and out["not_open"] == [other] and out["real_cancel_sent"] is False
    with pytest.raises(sim.IntentError):
        sim.simulate_cancel([oid] * 2, [], max_cancel_orders=100)
    with pytest.raises(sim.IntentError):
        sim.simulate_cancel(["0x" + "%064x" % i for i in range(101)], [], max_cancel_orders=100)
    with pytest.raises(sim.IntentError):
        sim.simulate_cancel(["nope"], [], max_cancel_orders=100)
    assert sim.simulate_cancel([oid], None, max_cancel_orders=100)["status"] == "NOT_SIMULATABLE"


# ---------------------------------------------------------------- reconciliation
OID = "0x" + "77" * 32
KEYI = "idem-key-0001"


def ok200(order_id=OID):
    return {"kind": "HTTP", "http_status": 200,
            "body": {"status": "success", "data": {"orders": [{"orderId": order_id, "status": "SUBMITTED", "commandId": "c"}]}}}


def found(status, remaining, reason=None):
    return {"kind": "FOUND", "order": {"id": OID, "status": status, "remainingSize": str(remaining), "inactiveReason": reason}}


def tracker_with(outcome, ihash="sha256:" + "a" * 64):
    t = sim.SubmissionTracker()
    state = t.record_submission(KEYI, ihash, OID, 5_000_000, outcome)
    return t, ihash, state


def test_timeout_after_send_is_unknown_and_blocks_retry_until_reconciled():
    t, h, state = tracker_with({"kind": "TIMEOUT"})
    assert state == sim.UNKNOWN_AFTER_TIMEOUT and t.may_retry(KEYI, h) is False
    with pytest.raises(sim.ReconcileError):
        t.record_submission(KEYI, h, OID, 5_000_000, ok200())  # retry before reconcile: refused
    # Lookup error teaches nothing.
    assert t.reconcile(KEYI, h, {"kind": "ERROR"}, seconds_since_submit=5) == sim.UNKNOWN_AFTER_TIMEOUT
    # 404 too late (outside the documented retention window) is not proof of absence.
    assert t.reconcile(KEYI, h, {"kind": "NOT_FOUND", "fills": []}, seconds_since_submit=3600) == sim.UNKNOWN_AFTER_TIMEOUT
    # 404 without a fills check is not proof either.
    assert t.reconcile(KEYI, h, {"kind": "NOT_FOUND"}, seconds_since_submit=5) == sim.UNKNOWN_AFTER_TIMEOUT
    assert t.reconcile(KEYI, h, {"kind": "NOT_FOUND", "fills": []}, seconds_since_submit=5) == sim.NOT_ON_EXCHANGE
    assert t.may_retry(KEYI, h) is True
    assert t.record_submission(KEYI, h, OID, 5_000_000, ok200()) == sim.SUBMITTED  # same key, same order
    assert [e["to"] for e in t.history(KEYI)][0] == sim.UNKNOWN_AFTER_TIMEOUT


def test_timeout_but_order_actually_live_is_never_resent():
    t, h, _ = tracker_with({"kind": "TIMEOUT"})
    assert t.reconcile(KEYI, h, found("ACTIVE", 5_000_000), seconds_since_submit=5) == sim.SUBMITTED
    assert t.may_retry(KEYI, h) is False


@pytest.mark.parametrize("outcome", [
    {"kind": "HTTP", "http_status": 503, "body": {"message": "Service Unavailable"}},
    {"kind": "HTTP", "http_status": 200, "body": {"status": "success", "data": {}}},
    {"kind": "HTTP", "http_status": 200, "body": "<html>"},
    ok200(order_id="0x" + "88" * 32),  # id does not match our digest
    {"kind": "CONNECTION_RESET"},
])
def test_ambiguous_responses_require_reconcile(outcome):
    t, h, state = tracker_with(outcome)
    assert state == sim.AMBIGUOUS_RESPONSE and t.may_retry(KEYI, h) is False


def test_rejected_is_definitive_and_retryable_with_same_key_only():
    t, h, state = tracker_with({"kind": "HTTP", "http_status": 400, "body": {"message": "ODDS_NOT_ON_LADDER"}})
    assert state == sim.REJECTED and t.may_retry(KEYI, h)
    with pytest.raises(sim.ReconcileError) as e:
        t.record_submission(KEYI, "sha256:" + "b" * 64, OID, 5_000_000, ok200())
    assert str(e.value) == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"
    with pytest.raises(sim.ReconcileError):
        t.may_retry(KEYI, "sha256:" + "b" * 64)


@pytest.mark.parametrize("lookup,state", [
    (found("ACTIVE", 2_000_000), sim.PARTIALLY_FILLED),
    (found("INACTIVE", 2_000_000, "NO_LIQUIDITY"), sim.PARTIALLY_FILLED),  # IOC partial
    (found("INACTIVE", 0, "FILLED"), sim.FILLED),
    (found("INACTIVE", 50_000, "FILLED"), sim.FILLED),  # dust remainder
    (found("INACTIVE", 5_000_000, "USER_REQUESTED"), sim.CANCELLED),
    (found("INACTIVE", 5_000_000, "EXPIRED"), sim.CANCELLED),
    (found("PENDING", 5_000_000), sim.SUBMITTED),
])
def test_reconciliation_from_order_lookup(lookup, state):
    t, h, _ = tracker_with(ok200())
    assert t.reconcile(KEYI, h, lookup, seconds_since_submit=5) == state
    assert t.may_retry(KEYI, h) is False


def test_double_click_same_key_cannot_double_submit():
    t, h, _ = tracker_with(ok200())
    with pytest.raises(sim.ReconcileError) as e:
        t.record_submission(KEYI, h, OID, 5_000_000, ok200())
    assert str(e.value).startswith("RETRY_NOT_ALLOWED_IN_STATE_")


def test_lookup_for_other_order_id_is_ignored():
    t, h, _ = tracker_with({"kind": "TIMEOUT"})
    other = {"kind": "FOUND", "order": {"id": "0x" + "99" * 32, "status": "INACTIVE", "remainingSize": "0",
                                        "inactiveReason": "FILLED"}}
    assert t.reconcile(KEYI, h, other, seconds_since_submit=5) == sim.UNKNOWN_AFTER_TIMEOUT


# ---------------------------------------------------------------- MCP tool
class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


def _router(market_status="ACTIVE", levels=((39 * 10 ** 18, 9_000_000),), balance="9000000", meta=None):
    def handler(request):
        path = request.url.path
        if request.method != "GET":
            return httpx.Response(405)
        if path == "/metadata/obv3":
            data = meta or META_RAW
        elif path == "/markets/find":
            data = [{"marketHash": H, "status": market_status, "outcomeOneName": "A", "outcomeTwoName": "B"}]
        elif path == "/orderbook-v3/snapshot/market":
            assert request.url.params["showTakerPerspective"] == "true"
            data = book(one=levels)
        elif path == "/user/balance-v3":
            data = {"balances": [{"tokenAddress": TOKEN, "escrowAddress": ESCROW, "availableAmount": balance}]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"status": "success", "data": data})
    return handler


def _tool(handler, key=KEY):
    calls = []
    settings = Settings(enabled=True, network="mainnet", api_key=key, max_rpm=60)
    client = SXBetReadClient(settings, transport=httpx.MockTransport(lambda r: (calls.append(r), handler(r))[1]))
    reg = _Registry()
    names = register_sxbet_tools(reg, settings, client=client)
    return reg.tools["sxbet_simulate_order"], calls, names


def _args(**kw):
    a = dict(market_hash=H, betting_outcome_one=True, stake_base_units=5_000_000, percentage_odds=str(P40),
             time_in_force="IOC", expiry=int(time.time()) + 3600, idempotency_key="tool-key-0001")
    a.update(kw)
    return a


def run(c):
    return asyncio.run(c)


def test_tool_registered_as_37th_sxbet_tool_in_simulation_mode():
    _, _, names = _tool(_router())
    assert len(names) == 37 and names.count("sxbet_simulate_order") == 1


def test_tool_simulates_without_signing_or_submitting():
    fn, calls, _ = _tool(_router())
    out = run(fn(**_args(maker_address=MAKER, check_balance=True)))
    assert out["status"] == "SIMULATED" and out["mode"] == "SIMULATION"
    assert out["signed"] is False and out["submitted"] is False and out["live_trading"].startswith("UNAVAILABLE")
    assert out["fill"]["fill"] == "FULL" and out["balance"]["status"] == "SUFFICIENT"
    assert out["signing_ready"] is True and out["intent_hash"] == sim.intent_hash(out["intent"])
    assert out["live_eligibility"]["eligible"] is False and "EXPOSURE_LIMIT_ZERO" in out["live_eligibility"]["reasons"]
    assert {r.method for r in calls} == {"GET"} and out["network_calls_made"] == len(calls) == 4
    assert all(r.url.host == "api.sx.bet" for r in calls)
    assert KEY not in json.dumps(out)
    assert "orderSignature" not in json.dumps(out)


def test_tool_without_maker_is_not_signing_ready():
    fn, _, _ = _tool(_router())
    out = run(fn(**_args()))
    assert out["status"] == "SIMULATED" and out["signing_ready"] is False and out["intent"]["salt"] is None


def test_tool_inactive_market_refused_and_book_not_read():
    fn, calls, _ = _tool(_router(market_status="INACTIVE"))
    out = run(fn(**_args()))
    assert out["status"] == "REFUSED" and out["error"]["code"] == "MARKET_INACTIVE"
    assert [r.url.path for r in calls] == ["/metadata/obv3", "/markets/find"]


def test_tool_insufficient_liquidity_and_balance():
    fn, _, _ = _tool(_router(levels=((39 * 10 ** 18, 1_000_000),), balance="100"))
    out = run(fn(**_args(check_balance=True)))
    assert out["fill"]["fill"] == "PARTIAL" and out["balance"]["status"] == "INSUFFICIENT_BALANCE"


def test_tool_without_key_reports_not_simulatable_without_auth_calls():
    fn, calls, _ = _tool(_router(), key="")
    out = run(fn(**_args(check_balance=True)))
    assert out["fill"] == {"status": "NOT_SIMULATABLE", "reason": "SXBET_KEY_NOT_CONFIGURED"}
    assert out["balance"]["status"] == "BALANCE_UNRESOLVED"
    assert all("x-sx-api-key" not in r.headers for r in calls) and len(calls) == 2


@pytest.mark.parametrize("kw,code", [
    ({"percentage_odds": 40}, "INVALID_PERCENTAGE_ODDS"),
    ({"percentage_odds": "4e19"}, "INVALID_PERCENTAGE_ODDS"),
    ({"market_hash": "0x1"}, "INVALID_MARKET_HASH"),
    ({"time_in_force": "gtc"}, "INVALID_TIME_IN_FORCE"),
    ({"stake_base_units": -1}, "INVALID_STAKE_BASE_UNITS"),
])
def test_tool_rejects_bad_input_before_network(kw, code):
    fn, calls, _ = _tool(_router())
    out = run(fn(**_args(**kw)))
    assert out["status"] == "INVALID_INPUT" and out["error"]["code"] == code and calls == []


def test_tool_metadata_inconsistent_stops():
    bad = json.loads(json.dumps(META_RAW))
    bad["activeAsset"]["escrowAddress"] = "0x" + "99" * 20
    fn, calls, _ = _tool(_router(meta=bad))
    out = run(fn(**_args()))
    assert out["status"] == "METADATA_INVALID" and out["error"]["code"] == "METADATA_ESCROW_MISMATCH" and len(calls) == 1


def test_tool_book_provider_error_is_not_simulatable():
    def handler(request):
        if request.url.path == "/orderbook-v3/snapshot/market":
            return httpx.Response(500, text="secret body")
        return _router()(request)
    fn, _, _ = _tool(handler)
    out = run(fn(**_args()))
    assert out["status"] == "SIMULATED" and out["fill"] == {"status": "NOT_SIMULATABLE", "reason": "SXBET_HTTP_ERROR"}
    assert "secret body" not in json.dumps(out)


# ---------------------------------------------------------------- isolation (structural)
def _imports(path: pathlib.Path) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def test_bridge_never_imports_signer():
    offenders = [str(p) for p in (ROOT / "bridge").rglob("*.py")
                 if any(m == "signer" or m.startswith("signer.") for m in _imports(p))]
    assert offenders == []
    for p in (ROOT / "bridge").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert "eth_account" not in src and "import_module(\"signer" not in src, p


def test_signer_never_imports_bridge_or_network_libraries():
    files = list((ROOT / "signer").rglob("*.py"))
    assert files
    banned = ("bridge", "httpx", "requests", "urllib", "http", "socket", "aiohttp", "websockets")
    for p in files:
        for m in _imports(p):
            assert m.split(".")[0] not in banned, (p, m)


def test_no_submission_code_in_signer_or_sxbet_module():
    files = list((ROOT / "signer").rglob("*.py")) + list((ROOT / "bridge" / "sxbet").rglob("*.py"))
    for p in files:
        src = p.read_text(encoding="utf-8")
        for needle in ("requests.post", "httpx.post", '"POST"', '"DELETE"', "'POST'", "'DELETE'",
                       ".post(", ".delete(", "heartbeat"):
            assert needle not in src, (p, needle)


def test_signer_deps_not_in_runtime_requirements():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "eth-account" not in runtime and "eth_account" not in runtime
    assert "eth-account" in (ROOT / "signer" / "requirements.txt").read_text(encoding="utf-8")
