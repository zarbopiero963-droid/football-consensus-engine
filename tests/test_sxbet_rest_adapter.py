"""C: official REST adapter for POST /orders-v3 + read routes (request BUILDER + response PARSER).

Contract: https://docs.sx.bet/api-reference/post-orders-v3, developers/posting-orders, developers/time-in-force,
developers/order-lifecycle, developers/taking-liquidity, api-reference/get-order-v3(-by-client-id),
api-reference/get-fills-v3 (fetched 01-11-2026). The only sender is the in-memory FakeSender: nothing here
or in signer/ opens a connection, and no API key is read.
"""
import json

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import gateway as gw  # noqa: E402
from signer import orders as od  # noqa: E402
from signer import rest_adapter as ra  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from sxbet_exchange_kit import FakeSender, ok, order_row, submitted_row  # noqa: E402
from sxbet_signer_kit import NOW, SIGNER_ID, Clock, approve, context, make_intent, make_signer  # noqa: E402

LIMITS = ra.ExchangeLimits(max_create_orders=10, max_cancel_orders=100)
MAKER = "0x" + "aa" * 20
SIG = "0x" + "11" * 65
OID = "0x" + "77" * 32


@pytest.fixture
def throwaway():
    acct = Account.create()
    return acct, acct.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()


def wire(**kw):
    o = {"marketHash": "0x" + "ab" * 32, "maker": "0x" + "Aa" * 20, "totalBetSize": "5000000",
         "percentageOdds": "40000000000000000000", "salt": str(0x8FA0CD4DBB399048CAF74B8455F0FCF502FC989FC3E2720003AA10756FAA70EF),
         "expiry": NOW + 3600, "baseToken": "0x" + "1b" * 20, "isMakerBettingOutcomeOne": True,
         "timeInForce": "GTC", "orderSignature": SIG, "clientOrderId": "order-key-0001"}
    o.update(kw)
    return o


def code(fn):
    with pytest.raises(ra.AdapterRefused) as e:
        fn()
    return e.value.code


# ---------------------------------------------------------------- request builder
def test_default_salt_is_the_openapi_32_byte_hex_string():
    req = ra.build_create_orders([wire()], limits=LIMITS, options=ra.SubmitOptions())
    assert (req.method, req.path, req.query) == ("POST", "/orders-v3", ())
    (o,) = req.body["orders"]
    assert o["salt"] == "0x8fa0cd4dbb399048caf74b8455f0fcf502fc989fc3e2720003aa10756faa70ef"  # OpenAPI example
    assert int(o["salt"], 16) == int(wire()["salt"])  # same uint256 that was signed
    assert set(req.body) == {"orders"}  # waitForOutcome / maxWaitTime omitted by default
    assert isinstance(o["expiry"], int) and o["totalBetSize"] == "5000000" and o["percentageOdds"].isdigit()
    assert "postOnly" not in o


def test_small_salt_is_zero_padded_to_32_bytes():
    req = ra.build_create_orders([wire(salt="1")], limits=LIMITS, options=ra.SubmitOptions())
    assert req.body["orders"][0]["salt"] == "0x" + "0" * 63 + "1"


def test_decimal_salt_is_an_explicit_config_choice():
    req = ra.build_create_orders([wire()], limits=LIMITS, options=ra.SubmitOptions(salt_format=ra.SALT_DECIMAL))
    assert req.body["orders"][0]["salt"] == wire()["salt"]
    with pytest.raises(ra.AdapterRefused):
        ra.SubmitOptions(salt_format="hex")


def test_wait_for_outcome_and_max_wait_time():
    req = ra.build_create_orders([wire()], limits=LIMITS,
                                 options=ra.SubmitOptions(wait_for_outcome=True, max_wait_time_ms=5000))
    assert req.body["waitForOutcome"] is True and req.body["maxWaitTime"] == 5000
    req = ra.build_create_orders([wire()], limits=LIMITS, options=ra.SubmitOptions(wait_for_outcome=True))
    assert req.body["waitForOutcome"] is True and "maxWaitTime" not in req.body
    for bad in (0, 15_001, "5000", True):
        with pytest.raises(ra.AdapterRefused):
            ra.SubmitOptions(wait_for_outcome=True, max_wait_time_ms=bad)
    with pytest.raises(ra.AdapterRefused):
        ra.SubmitOptions(wait_for_outcome=False, max_wait_time_ms=5000)  # documented as ignored: never sent


def test_post_only_only_on_gtc():
    opts = ra.SubmitOptions(post_only=True)
    req = ra.build_create_orders([wire()], limits=LIMITS, options=opts)
    assert req.body["orders"][0]["postOnly"] is True
    for tif in ("IOC", "FOK"):
        assert code(lambda: ra.build_create_orders([wire(timeInForce=tif)], limits=LIMITS,
                                                    options=opts)) == "POST_ONLY_REQUIRES_GTC"


@pytest.mark.parametrize("tif", ["GTC", "IOC", "FOK"])
def test_time_in_force_values(tif):
    req = ra.build_create_orders([wire(timeInForce=tif)], limits=LIMITS, options=ra.SubmitOptions())
    assert req.body["orders"][0]["timeInForce"] == tif


@pytest.mark.parametrize("change,expected", [
    ({"timeInForce": "gtc"}, "ORDER_TIME_IN_FORCE_INVALID"),
    ({"expiry": "1800003600"}, "ORDER_EXPIRY_INVALID"),
    ({"expiry": 1_800_003_600_000.0}, "ORDER_EXPIRY_INVALID"),
    ({"totalBetSize": 5000000}, "ORDER_AMOUNT_INVALID"),
    ({"percentageOdds": "4e19"}, "ORDER_AMOUNT_INVALID"),
    ({"salt": "0x12"}, "ORDER_SALT_INVALID"),
    ({"clientOrderId": "{braced-uuid}"}, "ORDER_CLIENT_ID_INVALID"),
    ({"orderSignature": "0x12"}, "ORDER_SIGNATURE_INVALID"),
    ({"marketHash": "0x12"}, "ORDER_MARKET_HASH_INVALID"),
    ({"useProxy": True}, "ORDER_KEYS_INVALID"),
])
def test_invalid_order_fields_refused(change, expected):
    assert code(lambda: ra.build_create_orders([wire(**change)], limits=LIMITS,
                                                options=ra.SubmitOptions())) == expected


def test_batch_limits_shared_maker_and_unique_client_ids():
    many = [wire(clientOrderId=f"order-key-{i:04d}") for i in range(11)]
    assert code(lambda: ra.build_create_orders(many, limits=LIMITS, options=ra.SubmitOptions())) == \
        "BATCH_SIZE_INVALID"
    assert code(lambda: ra.build_create_orders([], limits=LIMITS, options=ra.SubmitOptions())) == \
        "BATCH_SIZE_INVALID"
    ten = ra.build_create_orders(many[:10], limits=LIMITS, options=ra.SubmitOptions())
    assert len(ten.body["orders"]) == 10
    assert code(lambda: ra.build_create_orders([wire(), wire(clientOrderId="order-key-0002",
                                                             maker="0x" + "bb" * 20)],
                                                limits=LIMITS, options=ra.SubmitOptions())) == "BATCH_MIXED_MAKERS"
    assert code(lambda: ra.build_create_orders([wire(), wire()], limits=LIMITS,
                                                options=ra.SubmitOptions())) == "BATCH_DUPLICATE_CLIENT_ORDER_ID"
    small = ra.ExchangeLimits(max_create_orders=2, max_cancel_orders=100)
    assert code(lambda: ra.build_create_orders(many[:3], limits=small, options=ra.SubmitOptions())) == \
        "BATCH_SIZE_INVALID"


def test_limits_come_from_metadata_and_are_validated():
    lim = ra.ExchangeLimits.from_metadata({"maxCreateOrders": 10, "maxCancelOrders": 100,
                                            "orderSizeMinimumBaseUnits": "1000000"})
    assert (lim.max_create_orders, lim.max_cancel_orders) == (10, 100)
    for bad in ({}, {"maxCreateOrders": 0, "maxCancelOrders": 100}, {"maxCreateOrders": "10", "maxCancelOrders": 1}):
        with pytest.raises(ra.AdapterRefused):
            ra.ExchangeLimits.from_metadata(bad)


# ---------------------------------------------------------------- response parser
EXP = [ra.ExpectedOrder(OID, "order-key-0001", 5_000_000)]


def parse(resp, wait=False, exp=EXP):
    return ra.parse_create_orders(resp, exp, wait_for_outcome=wait)


def test_submitted_row_with_command_id():
    (r,) = parse(ok([submitted_row(OID, "order-key-0001", command_id="550e8400")]))
    assert (r.state, r.reason, r.command_id, r.filled) == (od.SUBMITTED, "EXCHANGE_SUBMITTED", "550e8400", None)


def test_failed_row_is_rejected_and_keeps_command_id():
    (r,) = parse(ok([submitted_row(OID, status="FAILED", command_id="c-9")]))
    assert (r.state, r.command_id) == (od.REJECTED, "c-9")


@pytest.mark.parametrize("resp,reason", [
    (None, "TRANSPORT_TIMEOUT"),
    (ra.RestResponse(503, {"message": "Service Unavailable"}), "REPLY_NOT_DEFINITIVE"),
    (ra.RestResponse(401, {"message": "BAD_AUTH"}), "REPLY_NOT_DEFINITIVE"),
    (ra.RestResponse(200, {"status": "failure", "data": {"orders": []}}), "REPLY_NOT_DEFINITIVE"),
    (ra.RestResponse(200, "<html>"), "REPLY_NOT_DEFINITIVE"),
    (ok([]), "REPLY_ROWS_AMBIGUOUS"),
    (ok([submitted_row("0x" + "12" * 32)]), "REPLY_OTHER_ORDER_ID"),
    (ok([submitted_row(OID, "other-client-id")]), "REPLY_CLIENT_ORDER_ID_MISMATCH"),
    (ok([submitted_row(OID, status="PENDING")]), "REPLY_STATUS_UNKNOWN"),
    (ok([{"orderId": OID, "status": "SUBMITTED"}]), "REPLY_COMMAND_ID_MISSING"),
])
def test_anything_not_definitive_is_unknown(resp, reason):
    (r,) = parse(resp)
    assert (r.state, r.reason) == (od.UNKNOWN, reason)


def test_http_400_single_order_rejected_but_batch_400_unknown():
    bad = ra.RestResponse(400, {"message": "TOTAL_BET_SIZE_TOO_LOW", "marketHash": "0x", "minimum": "1"})
    assert parse(bad)[0].state == od.REJECTED
    assert parse(ra.RestResponse(400, {"message": ["orders must be an array"], "statusCode": 400}))[0].state == \
        od.REJECTED
    two = EXP + [ra.ExpectedOrder("0x" + "78" * 32, "order-key-0002", 5_000_000)]
    # Not documented that every 400 rejects every order of a batch: fail closed.
    assert [r.state for r in parse(bad, exp=two)] == [od.UNKNOWN, od.UNKNOWN]


def test_non_atomic_batch_each_order_independently():
    o2, o3 = "0x" + "78" * 32, "0x" + "79" * 32
    exp = EXP + [ra.ExpectedOrder(o2, "order-key-0002", 5_000_000), ra.ExpectedOrder(o3, "order-key-0003", 5_000_000)]
    rows = [submitted_row(OID, "order-key-0001"), submitted_row(o2, status="FAILED"),
            submitted_row("0x" + "99" * 32)]
    assert [r.state for r in parse(ok(rows), exp=exp)] == [od.SUBMITTED, od.REJECTED, od.UNKNOWN]
    # Rows out of the documented order: each matched by id only at its own position, else UNKNOWN.
    swapped = [submitted_row(o2), submitted_row(OID), submitted_row(o3)]
    assert [r.state for r in parse(ok(swapped), exp=exp)] == [od.UNKNOWN, od.UNKNOWN, od.SUBMITTED]
    # Row count different from what was sent: nothing is definitive.
    assert [r.state for r in parse(ok(rows[:2]), exp=exp)] == [od.UNKNOWN] * 3


# Intentional change (finding D): outcome.fillAmount units are DOCS_UNVERIFIED, so a reply never sets
# `filled`. An outcome with a fill keeps the order SUBMITTED (whole stake reserved); only exchange READS
# (GET /orders-v3, /fills-v3) record the matched amount. Previously FULLY_FILLED -> FILLED and
# PARTIAL_FILL_* -> PARTIALLY_FILLED with the reply's fillAmount, which an overstated value made sticky.
@pytest.mark.parametrize("outcome,state,filled,reason", [
    ({"state": "RESTED", "remainingAmount": "5000000"}, od.OPEN, 0, "OUTCOME_RESTED"),
    ({"state": "FULLY_FILLED", "remainingAmount": "0", "fillAmount": "5000000", "blendedOdds": "1"}, od.SUBMITTED,
     None, "OUTCOME_FULLY_FILLED_RECONCILE_FOR_FILL"),
    ({"state": "PARTIAL_FILL_RESTED", "remainingAmount": "3000000", "fillAmount": "2000000"}, od.SUBMITTED,
     None, "OUTCOME_PARTIAL_FILL_RESTED_RECONCILE_FOR_FILL"),
    # Remainder discarded per the docs, but only a read (INACTIVE) releases it: stays reserved.
    ({"state": "PARTIAL_FILL_DONE", "remainingAmount": "0", "fillAmount": "2000000"}, od.SUBMITTED,
     None, "OUTCOME_PARTIAL_FILL_DONE_RECONCILE_FOR_FILL"),
    ({"state": "CANCELLED", "cancelReason": "NO_LIQUIDITY"}, od.SUBMITTED, None,
     "OUTCOME_CANCELLED_RECONCILE_TO_RELEASE"),
    ({"state": "TIMEOUT"}, od.SUBMITTED, None, "OUTCOME_TIMEOUT"),
    ({"state": "FULLY_FILLED", "fillAmount": "4000000"}, od.SUBMITTED, None, "OUTCOME_MALFORMED_IGNORED"),
    ({"state": "PARTIAL_FILL_RESTED", "fillAmount": "6000000"}, od.SUBMITTED, None, "OUTCOME_MALFORMED_IGNORED"),
    ({"state": "RESTED", "fillAmount": "1"}, od.SUBMITTED, None, "OUTCOME_MALFORMED_IGNORED"),
    ({"state": "MATCHED"}, od.SUBMITTED, None, "OUTCOME_MALFORMED_IGNORED"),
    ("FULLY_FILLED", od.SUBMITTED, None, "OUTCOME_MALFORMED_IGNORED"),
])
def test_wait_for_outcome_states(outcome, state, filled, reason):
    (r,) = parse(ok([submitted_row(OID, outcome=outcome)]), wait=True)
    assert (r.state, r.filled, r.reason) == (state, filled, reason)
    assert r.outcome_state == (outcome.get("state") if isinstance(outcome, dict) else None)
    assert r.evidence_sha256 and r.evidence_sha256.startswith("sha256:")


def test_cancel_reason_documented_values_only():
    (r,) = parse(ok([submitted_row(OID, outcome={"state": "CANCELLED", "cancelReason": "POST_ONLY_WOULD_CROSS"})]),
                 wait=True)
    assert r.cancel_reason == "POST_ONLY_WOULD_CROSS"
    (r,) = parse(ok([submitted_row(OID, outcome={"state": "CANCELLED", "cancelReason": "USER_REQUESTED"})]),
                 wait=True)
    assert r.cancel_reason is None and r.reason == "OUTCOME_CANCELLED_RECONCILE_TO_RELEASE"
    (r,) = parse(ok([submitted_row(OID, outcome={"state": "CANCELLED", "fillAmount": "1000000"})]), wait=True)
    # Finding D: reserved in full until reads record the fill (a fill is never dropped, never taken from a reply).
    assert (r.state, r.filled, r.reason) == (od.SUBMITTED, None, "OUTCOME_CANCELLED_RECONCILE_FOR_FILL")


def test_outcome_without_wait_for_outcome_is_ignored():
    (r,) = parse(ok([submitted_row(OID, outcome={"state": "FULLY_FILLED", "fillAmount": "5000000"})]), wait=False)
    assert (r.state, r.reason, r.filled) == (od.SUBMITTED, "OUTCOME_UNREQUESTED_IGNORED", None)


def test_outcome_on_failed_row_ignored():
    (r,) = parse(ok([submitted_row(OID, status="FAILED", outcome={"state": "FULLY_FILLED"})]), wait=True)
    assert r.state == od.REJECTED


# ---------------------------------------------------------------- read routes
def test_reads_build_documented_paths_and_parse():
    s = FakeSender()
    a = ra.OfficialRestAdapter(s, limits=LIMITS)
    s.orders[OID] = order_row(OID, "ACTIVE", 5_000_000)
    assert a.get_order(OID) == {"kind": "FOUND", "order": s.orders[OID]}
    assert a.get_order("0x" + "01" * 32) == {"kind": "NOT_FOUND"}
    assert a.get_order_by_client_id("order-key-0001") == {"kind": "NOT_FOUND"}
    assert [(r.method, r.path) for r in s.requests] == [("GET", "/orders-v3/" + OID),
                                                       ("GET", "/orders-v3/" + "0x" + "01" * 32),
                                                       ("GET", "/orders-v3/client/order-key-0001")]
    s.get_error = 503
    assert a.get_order(OID) == {"kind": "ERROR"}
    s.get_error = "timeout"
    assert a.get_order(OID) == {"kind": "ERROR"}
    # Never builds a path from an unvalidated id.
    n = len(s.requests)
    assert a.get_order("../user/balance-v3") == {"kind": "ERROR"}
    assert a.get_order_by_client_id("a/b") == {"kind": "ERROR"}
    assert len(s.requests) == n


def test_fills_are_paginated_and_complete_only_on_the_last_page():
    s = FakeSender()
    s.fill_page = 2
    s.fills[OID] = [{"orderId": OID, "fillAmount": "1", "status": "MATCHED"} for _ in range(5)]
    a = ra.OfficialRestAdapter(s, limits=LIMITS)
    out = a.get_fills(OID)
    assert out["kind"] == "OK" and out["complete"] is True and len(out["fills"]) == 5
    q = [dict(r.query) for r in s.requests]
    assert q[0] == {"orderId": OID, "perPage": "2"} or q[0]["orderId"] == OID
    assert q[1]["nextKey"] == "2" and q[2]["nextKey"] == "4"
    capped = ra.OfficialRestAdapter(s, limits=LIMITS, max_fill_pages=2)
    out = capped.get_fills(OID)
    assert out["kind"] == "OK" and out["complete"] is False  # never presented as complete
    s.get_error = 503
    assert a.get_fills(OID) == {"kind": "ERROR"}


def test_malformed_read_bodies_are_errors():
    s = FakeSender()
    a = ra.OfficialRestAdapter(s, limits=LIMITS)
    s.get_error = None
    s.send = lambda req: ra.RestResponse(200, {"status": "success", "data": {"order": "x"}})
    assert a.get_order(OID) == {"kind": "ERROR"}
    s.send = lambda req: ra.RestResponse(200, {"status": "success", "data": {"fills": "x"}})
    assert a.get_fills(OID) == {"kind": "ERROR"}


# ---------------------------------------------------------------- gateway integration
def write_flag(path, owner, expires=NOW + 3600):
    body = {"live_trading": "ENABLED", "signer_id": SIGNER_ID, "network": "mainnet", "issued_at": NOW,
            "expires_at": expires}
    body["signature"] = owner.sign(gw.live_flag_message(body)).hex()
    path.write_text(json.dumps(body))
    return str(path)


def setup(tmp_path, throwaway, owner, *, options=None, keys=("order-key-R001",), clock=None, limits=LIMITS,
          tifs=None):
    clock = clock or Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock)
    signed = []
    for n, k in enumerate(keys):
        i = make_intent(throwaway[0].address, idempotency_key=k, stake_base_units=2_000_000,
                        time_in_force=(tifs or {}).get(k, "GTC"))
        signed.append((s.sign_order(i, approve(owner, i), context(i)), i))
    sender = FakeSender()
    adapter = ra.OfficialRestAdapter(sender, limits=limits, options=options or ra.SubmitOptions())
    flag = gw.load_live_trading_flag(write_flag(tmp_path / "flag.json", owner), owner.public_key(),
                                     signer_id=SIGNER_ID, network="mainnet", now=NOW)
    g = gw.SubmissionGateway(flag, owner_public_key=owner.public_key(), transport=adapter, ledger=s._ledger,
                             audit=AuditLog(str(tmp_path / "gw-audit.jsonl")), odds_ladder_step_size=125,
                             network="mainnet", clock=clock)
    return s, signed, sender, g, clock


def test_gateway_with_official_adapter_sends_the_documented_body(tmp_path, throwaway, owner):
    s, [(sg, i)], sender, g, _ = setup(tmp_path, throwaway, owner)
    sender.script = [ok([submitted_row(sg["order_id"], i["idempotency_key"])])]
    out = g.submit(sg, i)
    assert out["state"] == od.SUBMITTED and out["sent"] is True
    (req,) = sender.requests
    assert (req.method, req.path) == ("POST", "/orders-v3")
    (o,) = req.body["orders"]
    assert o["salt"] == "0x" + format(int(i["salt"]), "064x")
    assert {k: v for k, v in o.items() if k != "salt"} == {k: v for k, v in sg["order"].items() if k != "salt"}
    assert s._ledger.get(i["idempotency_key"]).state == od.SUBMITTED


def test_gateway_batch_is_non_atomic(tmp_path, throwaway, owner):
    keys = ("order-key-B001", "order-key-B002", "order-key-B003")
    s, signed, sender, g, _ = setup(tmp_path, throwaway, owner, keys=keys)
    (a, ia), (b, ib), (c, ic) = signed
    sender.script = [ok([submitted_row(a["order_id"]), submitted_row(b["order_id"], status="FAILED"),
                         submitted_row("0x" + "99" * 32)])]
    out = g.submit_batch(signed)
    assert [o["state"] for o in out] == [od.SUBMITTED, od.REJECTED, od.UNKNOWN]
    assert all(o["sent"] for o in out)
    (req,) = sender.requests
    assert [o["clientOrderId"] for o in req.body["orders"]] == list(keys)
    assert [s._ledger.get(k).state for k in keys] == [od.SUBMITTED, od.REJECTED, od.UNKNOWN]
    assert s.committed_exposure_base_units == 6_000_000  # nothing released by a reply
    # Idempotent: a second batch sends nothing for orders already out, UNKNOWN is reported, not sent.
    again = g.submit_batch(signed)
    assert [o["sent"] for o in again] == [False, False, False]
    assert again[2]["reason"] == "STATE_UNKNOWN_RECONCILE_FIRST" and len(sender.requests) == 1


def test_gateway_batch_above_limit_or_duplicated_refused(tmp_path, throwaway, owner):
    keys = ("order-key-C001", "order-key-C002", "order-key-C003")
    small = ra.ExchangeLimits(max_create_orders=2, max_cancel_orders=100)
    s, signed, sender, g, _ = setup(tmp_path, throwaway, owner, keys=keys, limits=small)
    with pytest.raises(gw.GatewayRefused, match="BATCH_SIZE_INVALID"):
        g.submit_batch(signed)
    with pytest.raises(gw.GatewayRefused, match="BATCH_DUPLICATE_CLIENT_ORDER_ID"):
        g.submit_batch([signed[0], signed[0]])
    assert sender.requests == [] and all(s._ledger.get(k).state == od.SIGNED for k in keys)


def test_gateway_wait_for_outcome_fully_filled(tmp_path, throwaway, owner):
    s, [(sg, i)], sender, g, _ = setup(tmp_path, throwaway, owner,
                                       options=ra.SubmitOptions(wait_for_outcome=True, max_wait_time_ms=8000))
    sender.script = [ok([submitted_row(sg["order_id"], outcome={"state": "FULLY_FILLED", "remainingAmount": "0",
                                                                "fillAmount": "2000000"})])]
    out = g.submit(sg, i)
    # Finding D (intentional change): the reply's fillAmount is not recorded; SUBMITTED, stake reserved in full.
    assert out["state"] == od.SUBMITTED and sender.requests[0].body["maxWaitTime"] == 8000
    o = s._ledger.get(i["idempotency_key"])
    assert o.filled == 0 and s.committed_exposure_base_units == 2_000_000
    sender.orders[sg["order_id"]] = order_row(sg["order_id"], "INACTIVE", 0, "FILLED", total=2_000_000)
    assert g.reconcile(i["idempotency_key"])["state"] == od.FILLED
    o = s._ledger.get(i["idempotency_key"])
    assert o.filled == 2_000_000 and s.committed_exposure_base_units == 2_000_000  # matched, held till settled


def test_overstated_reply_fill_amount_does_not_poison_later_reads(tmp_path, throwaway, owner):
    """Finding D: the reply says FULLY_FILLED (units DOCS_UNVERIFIED) but the exchange row shows a partial fill.
    Before: filled was raised from the reply and the read failed forever with FILLED_AMOUNT_INVALID."""
    s, [(sg, i)], sender, g, _ = setup(tmp_path, throwaway, owner, options=ra.SubmitOptions(wait_for_outcome=True))
    sender.script = [ok([submitted_row(sg["order_id"], outcome={"state": "FULLY_FILLED", "remainingAmount": "0",
                                                                "fillAmount": "2000000"})])]
    g.submit(sg, i)
    sender.orders[sg["order_id"]] = order_row(sg["order_id"], "ACTIVE", 500_000, total=2_000_000)
    out = g.reconcile(i["idempotency_key"])
    assert out["state"] == od.PARTIALLY_FILLED and out["reason"] != "FILLED_AMOUNT_INVALID"
    o = s._ledger.get(i["idempotency_key"])
    assert o.filled == 1_500_000 and s.committed_exposure_base_units == 2_000_000


def test_gateway_wait_for_outcome_cancelled_stays_reserved_until_read(tmp_path, throwaway, owner):
    s, [(sg, i)], sender, g, _ = setup(tmp_path, throwaway, owner, options=ra.SubmitOptions(wait_for_outcome=True),
                                       tifs={"order-key-R001": "IOC"})
    sender.script = [ok([submitted_row(sg["order_id"], outcome={"state": "CANCELLED",
                                                                "cancelReason": "NO_LIQUIDITY"})])]
    assert g.submit(sg, i)["state"] == od.SUBMITTED
    assert s.committed_exposure_base_units == 2_000_000
    sender.orders[sg["order_id"]] = order_row(sg["order_id"], "INACTIVE", 2_000_000, "NO_LIQUIDITY", total=2_000_000)
    assert g.reconcile(i["idempotency_key"])["state"] == od.CANCELLED
    assert s.committed_exposure_base_units == 0


def test_taker_ioc_partial_fill_done_releases_only_after_read(tmp_path, throwaway, owner):
    s, [(sg, i)], sender, g, _ = setup(tmp_path, throwaway, owner, options=ra.SubmitOptions(wait_for_outcome=True),
                                       tifs={"order-key-R001": "IOC"})
    sender.script = [ok([submitted_row(sg["order_id"], outcome={"state": "PARTIAL_FILL_DONE",
                                                                "fillAmount": "500000", "remainingAmount": "0"})])]
    assert g.submit(sg, i)["state"] == od.SUBMITTED  # finding D: the fill comes from the read below
    assert s.committed_exposure_base_units == 2_000_000
    sender.orders[sg["order_id"]] = order_row(sg["order_id"], "INACTIVE", 1_500_000, "NO_LIQUIDITY", total=2_000_000)
    assert g.reconcile(i["idempotency_key"])["state"] == od.CANCELLED
    assert s.committed_exposure_base_units == 500_000  # matched part only


def test_gateway_builder_refusal_happens_before_write_ahead(tmp_path, throwaway, owner):
    s, [(sg, i)], sender, g, _ = setup(tmp_path, throwaway, owner, options=ra.SubmitOptions(post_only=True),
                                       tifs={"order-key-R001": "FOK"})
    with pytest.raises(gw.GatewayRefused, match="POST_ONLY_REQUIRES_GTC"):
        g.submit(sg, i)
    assert sender.requests == [] and s._ledger.get(i["idempotency_key"]).state == od.SIGNED


def test_gateway_sender_timeout_is_unknown_and_reconciles_through_adapter(tmp_path, throwaway, owner):
    s, [(sg, i)], sender, g, clock = setup(tmp_path, throwaway, owner)
    sender.script = ["timeout"]
    assert g.submit(sg, i)["state"] == od.UNKNOWN
    sender.orders[sg["order_id"]] = order_row(sg["order_id"], "ACTIVE", 2_000_000, total=2_000_000)
    assert g.reconcile(i["idempotency_key"])["state"] == od.OPEN
    paths = [r.path for r in sender.requests if r.method == "GET"]
    assert paths == ["/orders-v3/" + sg["order_id"], "/orders-v3/client/" + i["idempotency_key"], "/fills-v3"]


def test_adapter_module_has_no_network_or_key_access():
    import ast
    import pathlib
    src = pathlib.Path(ra.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                assert n.split(".")[0] not in {"httpx", "requests", "urllib", "aiohttp", "websockets", "web3",
                                               "socket", "http", "ssl", "os"}, n
    assert "x-sx-api-key" not in src.lower() and "environ" not in src
