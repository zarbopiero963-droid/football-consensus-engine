"""D: cancellation (§2.6 financial operation): builders, parsers, owner approval per cancel, reconcile-only release.

Contract: https://docs.sx.bet/developers/cancelling-orders, api-reference/delete-orders-v3, delete-orders-v3-market,
delete-orders-v3-event, delete-orders-v3-all (fetched 01-11-2026). In-memory FakeSender only.
"""
import json

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import cancellation as cx  # noqa: E402
from signer import gateway as gw  # noqa: E402
from signer import orders as od  # noqa: E402
from signer import rest_adapter as ra  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from sxbet_exchange_kit import FakeSender, ok, order_row, submitted_row  # noqa: E402
from sxbet_signer_kit import (ACCOUNT_ID, EVENT_ID, H, NOW, SIGNER_ID, Clock, approve, context,  # noqa: E402
                              make_intent, make_signer)

LIMITS = ra.ExchangeLimits(max_create_orders=10, max_cancel_orders=100)
STAKE = 2_000_000
OID = "0x" + "77" * 32


@pytest.fixture
def throwaway():
    acct = Account.create()
    return acct, acct.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()


def code(fn, exc=cx.CancelRefused):
    with pytest.raises(exc) as e:
        fn()
    return e.value.code


# ---------------------------------------------------------------- builders
def test_cancel_by_ids_request_body():
    req = cx.build_cancel_by_ids([OID, "0x" + "78" * 32], limits=LIMITS)
    assert (req.method, req.path, req.query) == ("DELETE", "/orders-v3", ())
    assert req.body == {"orders": [{"orderId": OID}, {"orderId": "0x" + "78" * 32}]}


def test_cancel_by_ids_limits():
    ids = ["0x" + format(n, "064x") for n in range(101)]
    assert code(lambda: cx.build_cancel_by_ids(ids, limits=LIMITS)) == "CANCEL_BATCH_SIZE_INVALID"
    assert len(cx.build_cancel_by_ids(ids[:100], limits=LIMITS).body["orders"]) == 100
    assert code(lambda: cx.build_cancel_by_ids([], limits=LIMITS)) == "CANCEL_BATCH_SIZE_INVALID"
    assert code(lambda: cx.build_cancel_by_ids([OID, OID], limits=LIMITS)) == "CANCEL_DUPLICATE_ORDER_ID"
    assert code(lambda: cx.build_cancel_by_ids(["0x12"], limits=LIMITS)) == "CANCEL_ORDER_ID_INVALID"
    assert code(lambda: cx.build_cancel_by_ids([OID.upper()], limits=LIMITS)) == "CANCEL_ORDER_ID_INVALID"


def test_scope_builders():
    assert cx.build_cancel_market(H) == ra.RestRequest("DELETE", "/orders-v3/market", (("marketHash", H),), None)
    assert cx.build_cancel_event(EVENT_ID) == ra.RestRequest("DELETE", "/orders-v3/event", (("eventId", EVENT_ID),),
                                                             None)
    assert cx.build_cancel_all() == ra.RestRequest("DELETE", "/orders-v3/all", (), None)
    assert code(lambda: cx.build_cancel_market("0x12")) == "CANCEL_MARKET_HASH_INVALID"
    assert code(lambda: cx.build_cancel_event("L1&x=1")) == "CANCEL_EVENT_ID_INVALID"


# ---------------------------------------------------------------- parsers
O2, O3 = "0x" + "78" * 32, "0x" + "79" * 32


def by_ids(data, status=200):
    return ra.RestResponse(status, {"status": "success", "data": data})


def test_parse_by_ids_three_buckets():
    resp = by_ids({"cancelled": [{"orderId": OID, "commandId": "c1"}],
                   "notCancelled": [{"orderId": O2, "reason": "NOT_ACTIVE"}],
                   "unconfirmed": [{"orderId": O3, "commandId": "c3"}]})
    out = cx.parse_cancel_by_ids(resp, [OID, O2, O3])
    assert (out[OID].bucket, out[OID].command_id) == (cx.CANCELLED, "c1")
    assert (out[O2].bucket, out[O2].reason) == (cx.NOT_CANCELLED, "NOT_ACTIVE")
    assert out[O3].bucket == cx.UNCONFIRMED


@pytest.mark.parametrize("resp,bucket", [
    (None, cx.UNKNOWN),
    (ra.RestResponse(500, {"message": "Failed to publish cancel command(s) to the orderbook"}), cx.UNKNOWN),
    (ra.RestResponse(200, {"status": "failure"}), cx.UNKNOWN),
    (by_ids({"cancelled": [], "notCancelled": [], "unconfirmed": []}), cx.UNKNOWN),  # id missing from every bucket
    (by_ids({"cancelled": [{"orderId": OID}], "notCancelled": [{"orderId": OID, "reason": "NOT_FOUND"}]}),
     cx.UNKNOWN),  # in two buckets
    (by_ids({"cancelled": "x"}), cx.UNKNOWN),
])
def test_parse_by_ids_fails_closed(resp, bucket):
    assert cx.parse_cancel_by_ids(resp, [OID])[OID].bucket == bucket


def test_parse_by_ids_upper_case_and_undocumented_reason():
    resp = by_ids({"cancelled": [], "notCancelled": [{"orderId": OID.upper().replace("0X", "0x"),
                                                      "reason": "SOMETHING_NEW"}], "unconfirmed": []})
    r = cx.parse_cancel_by_ids(resp, [OID])[OID]
    assert (r.bucket, r.reason) == (cx.NOT_CANCELLED, "UNDOCUMENTED_REASON")


def test_parse_scope_only_confirms_the_command():
    resp = ra.RestResponse(200, {"status": "success", "data": {"cancelledSubmitted": [
        {"orderId": OID, "commandId": "c1"}]}})
    out = cx.parse_cancel_scope(resp)
    assert out.accepted is True and out.submitted == ((OID, "c1"),)
    for bad in (None, ra.RestResponse(500, {}), ra.RestResponse(200, {"status": "success", "data": {}}),
                ra.RestResponse(200, {"status": "success", "data": {"cancelledSubmitted": [{"x": 1}]}})):
        assert cx.parse_cancel_scope(bad).accepted is False


# ---------------------------------------------------------------- flow
def write_flag(path, owner, expires=NOW + 3600):
    body = {"live_trading": "ENABLED", "signer_id": SIGNER_ID, "network": "mainnet", "issued_at": NOW,
            "expires_at": expires}
    body["signature"] = owner.sign(gw.live_flag_message(body)).hex()
    path.write_text(json.dumps(body))
    return str(path)


class Env:
    pass


def setup(tmp_path, throwaway, owner, keys=("order-key-X001",), flag_expires=NOW + 3600):
    e = Env()
    e.clock = Clock()
    e.signer = make_signer(tmp_path, throwaway, owner, clock=e.clock)
    e.sender = FakeSender()
    e.adapter = ra.OfficialRestAdapter(e.sender, limits=LIMITS)
    flag = gw.load_live_trading_flag(write_flag(tmp_path / "flag.json", owner, flag_expires), owner.public_key(),
                                     signer_id=SIGNER_ID, network="mainnet", now=NOW)
    e.gw = gw.SubmissionGateway(flag, owner_public_key=owner.public_key(), transport=e.adapter,
                                ledger=e.signer._ledger, audit=AuditLog(str(tmp_path / "gw-audit.jsonl")),
                                odds_ladder_step_size=125, network="mainnet", clock=e.clock)
    e.svc = cx.CancellationService(e.gw, owner_public_key=owner.public_key(), account_id=ACCOUNT_ID)
    e.orders = []
    for k in keys:
        i = make_intent(throwaway[0].address, idempotency_key=k, stake_base_units=STAKE)
        sg = e.signer.sign_order(i, approve(owner, i), context(i))
        e.sender.script = [ok([submitted_row(sg["order_id"], k)])]
        assert e.gw.submit(sg, i)["state"] == od.SUBMITTED
        e.sender.orders[sg["order_id"]] = order_row(sg["order_id"], "ACTIVE", STAKE, total=STAKE)
        e.orders.append((sg["order_id"], k))
    e.maker = throwaway[0].address.lower()
    e.owner = owner
    e.sender.requests.clear()
    return e


_n = [0]


def cancel_approval(e, scope, *, order_ids=(), market_hash=None, event_id=None, ttl=60, issued=NOW, key=None,
                    nonce=None, signer_id=SIGNER_ID, **overrides):
    _n[0] += 1
    binding = cx.build_cancel_binding(account_id=ACCOUNT_ID, maker=e.maker, network="mainnet", scope=scope,
                                      order_ids=order_ids, market_hash=market_hash, event_id=event_id)
    binding.update(overrides)
    nonce = nonce or f"cancel-nonce-{_n[0]:012d}"
    msg = cx.cancel_approval_message(binding=binding, approval_nonce=nonce, issued_at=issued,
                                     expires_at=issued + ttl, signer_id=signer_id)
    return cx.CancelApproval(binding, nonce, issued, issued + ttl, signer_id, (key or e.owner).sign(msg))


def cancelled_reply(*ids):
    return by_ids({"cancelled": [{"orderId": i, "commandId": "c-" + i[2:6]} for i in ids],
                   "notCancelled": [], "unconfirmed": []})


def deletes(e):
    return [r for r in e.sender.requests if r.method == "DELETE"]


def test_cancel_confirmed_by_read_releases(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]

    def engine(req):
        e.sender.orders[oid] = order_row(oid, "INACTIVE", STAKE, "USER_REQUESTED", total=STAKE)

    e.sender.on_delete = engine
    e.sender.script = [cancelled_reply(oid)]
    rep = e.svc.cancel_orders([oid], cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["sent"] is True
    assert rep["orders"][oid]["bucket"] == cx.CANCELLED and rep["orders"][oid]["state"] == od.CANCELLED
    (req,) = deletes(e)
    assert req.path == "/orders-v3" and req.body == {"orders": [{"orderId": oid}]}
    assert e.signer.committed_exposure_base_units == 0


def test_cancelled_bucket_alone_never_releases(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    e.sender.script = [cancelled_reply(oid)]  # exchange row still ACTIVE (read lag)
    rep = e.svc.cancel_orders([oid], cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["orders"][oid]["bucket"] == cx.CANCELLED and rep["orders"][oid]["state"] == od.OPEN
    assert e.signer.committed_exposure_base_units == STAKE
    e.sender.get_error = 503  # reads failing: still nothing released
    e.clock.t += 5
    assert e.gw.reconcile(key)["state"] == od.OPEN and e.signer.committed_exposure_base_units == STAKE
    e.sender.get_error = None
    e.sender.orders[oid] = order_row(oid, "INACTIVE", STAKE, "USER_REQUESTED", total=STAKE)
    assert e.gw.reconcile(key)["state"] == od.CANCELLED and e.signer.committed_exposure_base_units == 0


def test_partially_filled_then_cancelled(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    e.sender.orders[oid] = order_row(oid, "ACTIVE", 1_500_000, total=STAKE)

    def engine(req):
        e.sender.orders[oid] = order_row(oid, "INACTIVE", 1_500_000, "USER_REQUESTED", total=STAKE)

    e.sender.on_delete = engine
    e.sender.script = [cancelled_reply(oid)]
    rep = e.svc.cancel_orders([oid], cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["orders"][oid]["state_before"] == od.PARTIALLY_FILLED  # pre-check read
    o = e.signer._ledger.get(key)
    assert o.state == od.CANCELLED and o.filled == 500_000
    assert e.signer.committed_exposure_base_units == 500_000  # matched part stays until settlement


def test_already_filled_is_not_sent_and_approval_not_consumed(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    e.sender.orders[oid] = order_row(oid, "INACTIVE", 0, "FILLED", total=STAKE)
    appr = cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid])
    rep = e.svc.cancel_orders([oid], appr)
    assert rep["sent"] is False and rep["orders"][oid]["bucket"] == "ALREADY_TERMINAL"
    assert rep["orders"][oid]["state"] == od.FILLED and deletes(e) == []
    assert not e.signer._ledger.nonce_consumed(appr.approval_nonce)


def test_fill_racing_the_cancel(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]

    def engine(req):
        e.sender.orders[oid] = order_row(oid, "INACTIVE", 0, "FILLED", total=STAKE)

    e.sender.on_delete = engine
    e.sender.script = [by_ids({"cancelled": [], "notCancelled": [{"orderId": oid, "reason": "NOT_ACTIVE"}],
                               "unconfirmed": []})]
    rep = e.svc.cancel_orders([oid], cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["orders"][oid]["bucket"] == cx.NOT_CANCELLED and rep["orders"][oid]["state"] == od.FILLED
    assert e.signer.committed_exposure_base_units == STAKE  # matched, held until settled


@pytest.mark.parametrize("reply", ["timeout",
                                   by_ids({"cancelled": [], "notCancelled": [], "unconfirmed": [
                                       {"orderId": "0x" + "77" * 32, "commandId": "c"}]})])
def test_timeout_or_unconfirmed_stays_reserved_and_nonce_is_spent(tmp_path, throwaway, owner, reply):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    e.sender.script = [reply]
    appr = cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid])
    rep = e.svc.cancel_orders([oid], appr)
    assert rep["orders"][oid]["bucket"] in (cx.UNKNOWN, cx.UNCONFIRMED)
    assert rep["orders"][oid]["state"] == od.OPEN and e.signer.committed_exposure_base_units == STAKE
    assert e.signer._ledger.nonce_consumed(appr.approval_nonce)
    assert code(lambda: e.svc.cancel_orders([oid], appr)) == "CANCEL_APPROVAL_NONCE_REPLAYED"
    assert len(deletes(e)) == 1


def test_cancel_nonce_survives_restart(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    e.sender.script = [cancelled_reply(oid)]
    appr = cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid])
    e.svc.cancel_orders([oid], appr)
    e.signer._ledger.close()
    r = od.OrderLedger.open(str(tmp_path / "ledger.jsonl"), signer_id=SIGNER_ID, signer_address=e.maker,
                            clock=e.clock)
    assert r.nonce_consumed(appr.approval_nonce)
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    (creq,) = [x for x in rows if x["kind"] == "CANCEL_REQUEST"]
    assert creq["scope"] == cx.SCOPE_ORDER_IDS and creq["order_ids"] == [oid]
    assert r.exposure().total == STAKE  # the request line changed no exposure
    r.close()


@pytest.mark.parametrize("make,expected", [
    (lambda e, oid: None, "CANCEL_APPROVAL_MISSING"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid], key=Ed25519PrivateKey.generate()),
     "CANCEL_APPROVAL_SIGNATURE_INVALID"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid], issued=NOW - 120),
     "CANCEL_APPROVAL_EXPIRED"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid], ttl=121), "CANCEL_APPROVAL_TTL_TOO_LONG"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid], signer_id="other"),
     "CANCEL_APPROVAL_WRONG_SIGNER"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[O2]), "CANCEL_APPROVAL_BINDING_MISMATCH"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid, O2]), "CANCEL_APPROVAL_BINDING_MISMATCH"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_MARKET, market_hash=H), "CANCEL_APPROVAL_BINDING_MISMATCH"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid], account_id="other-account"),
     "CANCEL_APPROVAL_BINDING_MISMATCH"),
    (lambda e, oid: cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid], maker="0x" + "cc" * 20),
     "CANCEL_APPROVAL_BINDING_MISMATCH"),
    (lambda e, oid: "approved: cancel it", "CANCEL_APPROVAL_MISSING"),  # chat text is not an approval
])
def test_cancel_requires_a_matching_owner_approval(tmp_path, throwaway, owner, make, expected):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    assert code(lambda: e.svc.cancel_orders([oid], make(e, oid))) == expected
    assert deletes(e) == [] and e.signer._ledger.get(key).state == od.SUBMITTED


def test_cancel_unknown_order_or_expired_flag_refused(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    assert code(lambda: e.svc.cancel_orders([O3], cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[O3]))) == \
        "CANCEL_ORDER_NOT_IN_LEDGER"
    e.clock.t = NOW + 3600
    assert code(lambda: e.svc.cancel_orders([oid], cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid],
                                                                    issued=NOW + 3600)),
                gw.GatewayRefused) == "LIVE_TRADING_FLAG_EXPIRED"
    assert deletes(e) == []


def test_cancel_market_scope(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner, keys=("order-key-X001", "order-key-X002"))
    (o1, k1), (o2, k2) = e.orders

    def engine(req):
        e.sender.orders[o1] = order_row(o1, "INACTIVE", STAKE, "USER_REQUESTED", total=STAKE)  # o2 still lagging

    e.sender.on_delete = engine
    e.sender.script = [ra.RestResponse(200, {"status": "success", "data": {"cancelledSubmitted": [
        {"orderId": o1, "commandId": "c1"}, {"orderId": o2, "commandId": "c2"},
        {"orderId": O3, "commandId": "c3"}]}})]
    assert code(lambda: e.svc.cancel_market(H, cancel_approval(e, cx.SCOPE_EVENT, event_id=EVENT_ID))) == \
        "CANCEL_APPROVAL_BINDING_MISMATCH"
    rep = e.svc.cancel_market(H, cancel_approval(e, cx.SCOPE_MARKET, market_hash=H))
    (req,) = deletes(e)
    assert (req.path, req.query, req.body) == ("/orders-v3/market", (("marketHash", H),), None)
    assert rep["accepted"] is True and rep["foreign_order_ids"] == [O3]
    assert rep["orders"][o1]["state"] == od.CANCELLED and rep["orders"][o2]["state"] == od.OPEN
    assert e.signer.committed_exposure_base_units == STAKE


@pytest.mark.parametrize("scope", [cx.SCOPE_EVENT, cx.SCOPE_ALL])
def test_cancel_event_and_all_scopes(tmp_path, throwaway, owner, scope):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    e.sender.script = [ra.RestResponse(500, {"message": "Failed to publish cancel command(s) to the orderbook"})]
    if scope == cx.SCOPE_EVENT:
        appr = cancel_approval(e, scope, event_id=EVENT_ID)
        rep = e.svc.cancel_event(EVENT_ID, appr)
        assert deletes(e)[0].query == (("eventId", EVENT_ID),) and deletes(e)[0].path == "/orders-v3/event"
    else:
        appr = cancel_approval(e, scope)
        rep = e.svc.cancel_all(appr)
        assert deletes(e)[0].path == "/orders-v3/all" and deletes(e)[0].query == ()
    assert rep["accepted"] is False and rep["orders"][oid]["state"] == od.OPEN
    assert e.signer._ledger.nonce_consumed(appr.approval_nonce)
    assert e.signer.committed_exposure_base_units == STAKE


def test_cancel_audit_is_allowlisted(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    e.sender.script = [cancelled_reply(oid)]
    e.svc.cancel_orders([oid], cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    decisions = [json.loads(l)["decision"] for l in (tmp_path / "gw-audit.jsonl").read_text().splitlines()]
    assert "CANCEL_REQUESTED" in decisions and "CANCEL_RESULT_CANCELLED" in decisions


# ---------------------------------------------------------------- finding B: TTL / flag re-checked after the reads
def _slow_reads(e, seconds):
    """Every exchange read takes `seconds` (the pre-check does up to 3 reads per order)."""
    real = e.sender._get

    def slow(req):
        e.clock.t += seconds
        return real(req)

    e.sender._get = slow


@pytest.mark.parametrize("scope", [cx.SCOPE_ORDER_IDS, cx.SCOPE_MARKET, cx.SCOPE_ALL])
def test_approval_expiring_during_precheck_reads_is_refused_and_not_consumed(tmp_path, throwaway, owner, scope):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    appr = {cx.SCOPE_ORDER_IDS: lambda: cancel_approval(e, scope, order_ids=[oid], ttl=60),
            cx.SCOPE_MARKET: lambda: cancel_approval(e, scope, market_hash=H, ttl=60),
            cx.SCOPE_ALL: lambda: cancel_approval(e, scope, ttl=60)}[scope]()
    run = {cx.SCOPE_ORDER_IDS: lambda: e.svc.cancel_orders([oid], appr),
           cx.SCOPE_MARKET: lambda: e.svc.cancel_market(H, appr),
           cx.SCOPE_ALL: lambda: e.svc.cancel_all(appr)}[scope]
    _slow_reads(e, 25)  # 3 reads -> now = NOW + 75 >= expires_at = NOW + 60
    assert code(run) == "CANCEL_APPROVAL_EXPIRED"
    assert deletes(e) == [] and not e.signer._ledger.nonce_consumed(appr.approval_nonce)
    assert e.signer.committed_exposure_base_units == STAKE


@pytest.mark.parametrize("scope", [cx.SCOPE_ORDER_IDS, cx.SCOPE_EVENT])
def test_live_flag_expiring_during_precheck_reads_is_refused_and_not_consumed(tmp_path, throwaway, owner, scope):
    e = setup(tmp_path, throwaway, owner, flag_expires=NOW + 50)
    oid, key = e.orders[0]
    if scope == cx.SCOPE_ORDER_IDS:
        appr = cancel_approval(e, scope, order_ids=[oid], ttl=120)
        run = lambda: e.svc.cancel_orders([oid], appr)  # noqa: E731
    else:
        appr = cancel_approval(e, scope, event_id=EVENT_ID, ttl=120)
        run = lambda: e.svc.cancel_event(EVENT_ID, appr)  # noqa: E731
    _slow_reads(e, 20)  # now = NOW + 60: flag expired (NOW + 50), approval still valid (NOW + 120)
    assert code(run, gw.GatewayRefused) == "LIVE_TRADING_FLAG_EXPIRED"
    assert deletes(e) == [] and not e.signer._ledger.nonce_consumed(appr.approval_nonce)


def test_approval_still_valid_after_reads_is_sent(tmp_path, throwaway, owner):
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    appr = cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid], ttl=60)
    _slow_reads(e, 10)  # now = NOW + 30 < NOW + 60
    e.sender.script = [cancelled_reply(oid)]
    assert e.svc.cancel_orders([oid], appr)["sent"] is True
    assert len(deletes(e)) == 1 and e.signer._ledger.nonce_consumed(appr.approval_nonce)


def test_expiry_between_consume_and_send_sends_nothing(tmp_path, throwaway, owner):
    """Last check right before the request leaves: the nonce is spent (fail closed) but nothing is sent."""
    e = setup(tmp_path, throwaway, owner)
    oid, key = e.orders[0]
    appr = cancel_approval(e, cx.SCOPE_ORDER_IDS, order_ids=[oid], ttl=60)
    real_log = e.svc._log

    def slow_log(decision, *a, **k):
        real_log(decision, *a, **k)
        if decision == "CANCEL_REQUESTED":
            e.clock.t = appr.expires_at  # e.g. a slow fsync of the audit line

    e.svc._log = slow_log
    assert code(lambda: e.svc.cancel_orders([oid], appr)) == "CANCEL_APPROVAL_EXPIRED"
    assert deletes(e) == [] and e.signer._ledger.nonce_consumed(appr.approval_nonce)
    assert e.signer.committed_exposure_base_units == STAKE
    decisions = [json.loads(l)["decision"] for l in (tmp_path / "gw-audit.jsonl").read_text().splitlines()]
    assert decisions[-1] == "CANCEL_NOT_SENT"
