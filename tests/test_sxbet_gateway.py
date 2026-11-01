"""Submission gateway: disabled by default; validation, idempotency, timeout => UNKNOWN, reconcile-then-retry.

The only transport here is the in-memory FakeTransport below. No real transport exists in the repository;
nothing in this file or in signer/ can reach the network.
"""
import json
import threading

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import gateway as gw  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from sxbet_signer_kit import NOW, SIGNER_ID, Clock, approve, context, make_intent, make_signer  # noqa: E402


class FakeTransport:
    """In-memory stand-in for the exchange. Records every payload; scripted replies."""

    def __init__(self):
        self.sent = []
        self.replies = []
        self.orders = {}        # order_id -> OrderV3 row
        self.by_client = {}
        self.fills = {}
        self.lock = threading.Lock()

    def submit_orders(self, payload):
        with self.lock:
            self.sent.append(json.loads(json.dumps(payload)))
            r = self.replies.pop(0) if self.replies else "ok"
        if r == "timeout":
            raise gw.TransportTimeout()
        if r == "boom":
            raise ConnectionError("socket closed")
        if callable(r):
            return r(payload)
        return r if isinstance(r, dict) else {"http_status": 200, "body": {"status": "success", "data": {"orders": [
            {"orderId": self.expected_id, "status": "SUBMITTED", "commandId": "c-1",
             "clientOrderId": payload["orders"][0]["clientOrderId"]}]}}}

    def get_order(self, order_id):
        row = self.orders.get(order_id)
        return {"kind": "FOUND", "order": row} if row else {"kind": "NOT_FOUND"}

    def get_order_by_client_id(self, cid):
        row = self.by_client.get(cid)
        return {"kind": "FOUND", "order": row} if row else {"kind": "NOT_FOUND"}

    def get_fills(self, order_id):
        return {"kind": "OK", "fills": self.fills.get(order_id, []), "complete": True}


@pytest.fixture
def throwaway():
    acct = Account.create()
    return acct, acct.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()


def write_flag(path, owner, *, signer_id=SIGNER_ID, network="mainnet", issued=NOW, expires=NOW + 3600,
               live="ENABLED", key=None):
    body = {"live_trading": live, "signer_id": signer_id, "network": network, "issued_at": issued,
            "expires_at": expires}
    body["signature"] = (key or owner).sign(gw.live_flag_message(body)).hex()
    path.write_text(json.dumps(body))
    return str(path)


def enabled_flag(tmp_path, owner, **kw):
    return gw.load_live_trading_flag(write_flag(tmp_path / "flag.json", owner, **kw), owner.public_key(),
                                     signer_id=SIGNER_ID, network="mainnet", now=NOW)


def setup(tmp_path, throwaway, owner, clock=None, key="order-key-G001", flag_kw=None):
    clock = clock or Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock)
    i = make_intent(throwaway[0].address, idempotency_key=key)
    signed = s.sign_order(i, approve(owner, i), context(i))
    t = FakeTransport()
    t.expected_id = signed["order_id"]
    g = gw.SubmissionGateway(enabled_flag(tmp_path, owner, **(flag_kw or {})), owner_public_key=owner.public_key(),
                             transport=t, ledger=s._ledger,
                             audit=AuditLog(str(tmp_path / "gw-audit.jsonl")), odds_ladder_step_size=125,
                             network="mainnet", clock=clock)
    return s, i, signed, t, g, clock


def refused(fn):
    with pytest.raises(gw.GatewayRefused) as e:
        fn()
    return e.value.code


def row(oid, status, remaining, reason=None, total=5_000_000):
    return {"id": oid, "totalBetSize": str(total), "remainingSize": str(remaining), "status": status,
            "inactiveReason": reason}


# ---------------------------------------------------------------- disabled by default
def test_default_construction_refuses(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    kw = dict(owner_public_key=owner.public_key(), transport=FakeTransport(), ledger=s._ledger,
              audit=AuditLog(str(tmp_path / "a")), odds_ladder_step_size=125, network="mainnet",
              clock=lambda: NOW)
    with pytest.raises(gw.GatewayDisabled):
        gw.SubmissionGateway(gw.DISABLED, **kw)
    with pytest.raises(gw.GatewayDisabled):
        gw.SubmissionGateway(gw.load_live_trading_flag(None, owner.public_key(), signer_id=SIGNER_ID,
                                                       network="mainnet", now=NOW), **kw)
    # A hand-built flag object is not an owner-signed one.
    with pytest.raises(gw.GatewayDisabled):
        gw.SubmissionGateway(gw.LiveTradingFlag(True, "HAND_MADE", SIGNER_ID, "mainnet", NOW + 10), **kw)
    with pytest.raises(gw.GatewayDisabled):
        gw.SubmissionGateway(True, **kw)


@pytest.mark.parametrize("kw,source", [
    ({"key": Ed25519PrivateKey.generate()}, "FLAG_SIGNATURE_INVALID"),
    ({"signer_id": "other"}, "FLAG_NOT_FOR_THIS_SIGNER"),
    ({"network": "testnet"}, "FLAG_NOT_FOR_THIS_SIGNER"),
    ({"live": "DISABLED"}, "FLAG_NOT_FOR_THIS_SIGNER"),
    ({"expires": NOW}, "FLAG_EXPIRED_OR_TTL_INVALID"),
    ({"issued": NOW - 10, "expires": NOW + 2 * 86_400}, "FLAG_EXPIRED_OR_TTL_INVALID"),
])
def test_flag_must_be_owner_signed_bound_and_fresh(tmp_path, owner, kw, source):
    f = enabled_flag(tmp_path, owner, **kw)
    assert f.enabled is False and f.source == source


def test_unsigned_or_garbage_flag_disabled(tmp_path, owner):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"live_trading": "ENABLED", "signer_id": SIGNER_ID, "network": "mainnet",
                             "issued_at": NOW, "expires_at": NOW + 60}))
    assert gw.load_live_trading_flag(str(p), owner.public_key(), signer_id=SIGNER_ID, network="mainnet",
                                     now=NOW).enabled is False
    p.write_text("nope")
    assert gw.load_live_trading_flag(str(p), owner.public_key(), signer_id=SIGNER_ID, network="mainnet",
                                     now=NOW).source == "FLAG_UNREADABLE"


# ---------------------------------------------------------------- payload + validation
def test_payload_matches_documented_post_orders_v3_shape(tmp_path, throwaway, owner):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    out = g.submit(signed, i)
    assert out["state"] == od.SUBMITTED and out["sent"] is True
    (payload,) = t.sent
    assert set(payload) == {"orders"} and len(payload["orders"]) == 1
    o = payload["orders"][0]
    assert set(o) == gw.PAYLOAD_ORDER_KEYS == {"marketHash", "maker", "totalBetSize", "percentageOdds", "salt",
                                                "expiry", "baseToken", "isMakerBettingOutcomeOne", "timeInForce",
                                                "orderSignature", "clientOrderId"}
    assert o["clientOrderId"] == i["idempotency_key"] and isinstance(o["expiry"], int)
    assert s._ledger.get(i["idempotency_key"]).state == od.SUBMITTED
    assert s.committed_exposure_base_units == 5_000_000  # SUBMITTED still fully reserved


@pytest.mark.parametrize("tamper,code", [
    (lambda sg, i: ({**sg, "intent_hash": "sha256:" + "0" * 64}, i), "SIGNED_ORDER_INTENT_MISMATCH"),
    (lambda sg, i: ({**sg, "order_id": "0x" + "0" * 64}, i), "SIGNED_ORDER_ID_MISMATCH"),
    (lambda sg, i: ({**sg, "order": {**sg["order"], "totalBetSize": "9000000"}}, i),
     "SIGNED_ORDER_PAYLOAD_NOT_THE_APPROVED_INTENT"),
    (lambda sg, i: ({**sg, "order": {**sg["order"], "timeInForce": "FOK"}}, i),
     "SIGNED_ORDER_PAYLOAD_NOT_THE_APPROVED_INTENT"),
    (lambda sg, i: ({**sg, "order": {**sg["order"], "waitForOutcome": True}}, i), "SIGNED_ORDER_PAYLOAD_KEYS"),
    (lambda sg, i: (sg, {**i, "total_bet_size": "6000000"}), "SIGNED_ORDER_INTENT_MISMATCH"),
    (lambda sg, i: ({**sg, "order": {**sg["order"], "orderSignature": "0x" + "11" * 65}}, i),
     "SIGNATURE_UNRECOVERABLE"),
])
def test_signed_order_must_be_exactly_the_approved_one(tmp_path, throwaway, owner, tamper, code):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    bad_signed, bad_intent = tamper(signed, i)
    assert refused(lambda: g.submit(bad_signed, bad_intent)) == code
    assert t.sent == [] and s._ledger.get(i["idempotency_key"]).state == od.SIGNED


def test_signature_by_another_key_refused(tmp_path, throwaway, owner):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    from signer import eip712
    from signer.intent import validate
    other = Account.create()
    forged = other.sign_message(eip712.signable(validate(i, odds_ladder_step_size=125))).signature.to_0x_hex()
    bad = {**signed, "order": {**signed["order"], "orderSignature": forged}}
    assert refused(lambda: g.submit(bad, i)) == "SIGNATURE_NOT_BY_MAKER" and t.sent == []


def test_order_not_in_ledger_refused(tmp_path, throwaway, owner):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    s2, i2, signed2, _, _, _ = setup(other_dir, (throwaway[0], throwaway[1]), owner, key="order-key-G777")
    assert refused(lambda: g.submit(signed2, i2)) == "ORDER_NOT_AUTHORIZED_IN_LEDGER"


# ---------------------------------------------------------------- idempotency + duplicates + races
def test_same_client_order_id_never_sent_twice(tmp_path, throwaway, owner):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    first = g.submit(signed, i)
    second = g.submit(signed, i)
    assert first["sent"] is True and second == {"client_order_id": i["idempotency_key"],
                                                 "order_id": signed["order_id"], "state": od.SUBMITTED, "sent": False}
    assert len(t.sent) == 1


def test_concurrent_submits_send_once(tmp_path, throwaway, owner):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    out = []
    barrier = threading.Barrier(12)

    def worker():
        barrier.wait()
        out.append(g.submit(signed, i))

    ts = [threading.Thread(target=worker) for _ in range(12)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert len(t.sent) == 1 and sum(1 for o in out if o["sent"]) == 1


def test_expired_signed_order_not_sent(tmp_path, throwaway, owner):
    clock = Clock()
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner, clock=clock)
    clock.t = i["expiry"] - 5
    assert refused(lambda: g.submit(signed, i)) == "ORDER_EXPIRED_OR_TOO_SOON_NOT_SENT" and t.sent == []


# ---------------------------------------------------------------- reply classification
@pytest.mark.parametrize("reply,state", [
    ("timeout", od.UNKNOWN),
    ("boom", od.UNKNOWN),
    ({"http_status": 503, "body": {"message": "Service Unavailable", "statusCode": 503}}, od.UNKNOWN),
    ({"http_status": 401, "body": {"message": "BAD_AUTH"}}, od.UNKNOWN),
    ({"http_status": 200, "body": {"status": "success", "data": {"orders": []}}}, od.UNKNOWN),
    ({"http_status": 200, "body": {"status": "success", "data": {"orders": [
        {"orderId": "0x" + "12" * 32, "status": "SUBMITTED", "commandId": "c"}]}}}, od.UNKNOWN),
    ({"http_status": 200, "body": "<html>"}, od.UNKNOWN),
    ({"http_status": 400, "body": {"message": "PROXY_NOT_DEPLOYED", "maker": "0x"}}, od.REJECTED),
    ({"http_status": 400, "body": {"message": ["bad"], "error": "Bad Request", "statusCode": 400}}, od.REJECTED),
])
def test_reply_classification_fails_closed(tmp_path, throwaway, owner, reply, state):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    t.replies = [reply]
    out = g.submit(signed, i)
    assert out["state"] == state
    o = s._ledger.get(i["idempotency_key"])
    assert o.state == state
    # M3 (intentional change): a REJECTED reply after the signature was released keeps the stake
    # reserved until the order's expiry has passed; see test_m3_http_400_after_signature_...
    assert s.committed_exposure_base_units == 5_000_000


def test_failed_row_is_rejected(tmp_path, throwaway, owner):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    t.replies = [{"http_status": 200, "body": {"status": "success", "data": {"orders": [
        {"orderId": signed["order_id"], "status": "FAILED", "commandId": "c"}]}}}]
    assert g.submit(signed, i)["state"] == od.REJECTED


# ---------------------------------------------------------------- timeout -> UNKNOWN -> reconcile -> (no) retry
def test_timeout_but_order_live_is_never_resent(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner)
    t.replies = ["timeout"]
    assert g.submit(signed, i)["state"] == od.UNKNOWN
    assert refused(lambda: g.submit(signed, i)) == "STATE_UNKNOWN_RECONCILE_FIRST"
    t.orders[signed["order_id"]] = row(signed["order_id"], "ACTIVE", 5_000_000)
    assert g.reconcile(i["idempotency_key"])["state"] == od.OPEN
    assert g.submit(signed, i)["sent"] is False and len(t.sent) == 1


def test_timeout_then_reconciled_absent_allows_one_safe_resend(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner)
    t.replies = ["timeout"]
    g.submit(signed, i)
    assert g.reconcile(i["idempotency_key"])["state"] == od.UNKNOWN  # too soon: still unknown
    clock.t += od.ABSENT_SETTLE_SECONDS + 1
    assert g.reconcile(i["idempotency_key"])["state"] == od.SIGNED
    out = g.submit(signed, i)
    assert out["state"] == od.SUBMITTED and len(t.sent) == 2
    assert t.sent[0] == t.sent[1]  # identical order, same clientOrderId
    assert s.committed_exposure_base_units == 5_000_000


def test_crash_mid_send_leaves_unknown(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner)

    class Crash(BaseException):
        pass

    def die(payload):
        raise Crash()

    t.replies = [die]
    with pytest.raises(Crash):
        g.submit(signed, i)
    r = make_signer(tmp_path, throwaway, owner, clock=clock)
    assert r._ledger.get(i["idempotency_key"]).state == od.UNKNOWN  # write-ahead persisted
    assert r.committed_exposure_base_units == 5_000_000


def test_reconcile_partial_fill_cancel_and_settle_through_gateway(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner)
    g.submit(signed, i)
    oid = signed["order_id"]
    t.orders[oid] = row(oid, "ACTIVE", 2_000_000)
    assert g.reconcile(i["idempotency_key"])["state"] == od.PARTIALLY_FILLED
    t.orders[oid] = row(oid, "INACTIVE", 2_000_000, "USER_REQUESTED")
    assert g.reconcile(i["idempotency_key"])["state"] == od.CANCELLED
    assert s.committed_exposure_base_units == 3_000_000
    t.fills[oid] = [{"orderId": oid, "fillAmount": "3000000", "status": "SETTLED",
                     "settlement": {"outcome": 1, "settleReturnAmount": "6000000"}}]
    assert g.reconcile(i["idempotency_key"])["state"] == od.SETTLED
    assert s.committed_exposure_base_units == 0 and s._ledger.realized_pnl_since(0) == 3_000_000
    # Conflicting evidence later (order ACTIVE again) is reported, never applied.
    t.orders[oid] = row(oid, "ACTIVE", 5_000_000)
    assert g.reconcile(i["idempotency_key"])["state"] == od.SETTLED


def test_reconcile_lookup_errors_learn_nothing(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner)
    t.replies = ["timeout"]
    g.submit(signed, i)

    def broken(_):
        raise TimeoutError()

    t.get_order = broken
    t.get_fills = broken
    clock.t += 10_000
    assert g.reconcile(i["idempotency_key"])["state"] == od.UNKNOWN


def test_gateway_audit_is_allowlisted_and_has_no_signature(tmp_path, throwaway, owner):
    s, i, signed, t, g, _ = setup(tmp_path, throwaway, owner)
    g.submit(signed, i)
    text = (tmp_path / "gw-audit.jsonl").read_text()
    assert signed["order"]["orderSignature"][2:30] not in text
    assert throwaway[1].removeprefix("0x") not in text.lower()
    assert [json.loads(l)["decision"] for l in text.splitlines()] == [od.SUBMITTED]


# ================================================================ review fixes (M3, M4)
def test_m3_http_400_after_signature_keeps_stake_reserved_until_expiry(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner)
    t.replies = [{"http_status": 400, "body": {"message": "PROXY_NOT_DEPLOYED"}}]
    assert g.submit(signed, i)["state"] == od.REJECTED
    # The signed order is still postable by whoever holds it until it expires.
    assert s.committed_exposure_base_units == 5_000_000
    clock.t = i["expiry"] + od.EXPIRY_GRACE_SECONDS + 1
    assert s.committed_exposure_base_units == 0


def test_m4_flag_cannot_be_constructed_directly():
    with pytest.raises(gw.GatewayDisabled):
        gw.LiveTradingFlag(True, "OWNER_SIGNED", SIGNER_ID, "mainnet", NOW + 3600)


def test_m4_expired_flag_refuses_submit(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner, flag_kw={"expires": NOW + 600})
    clock.t = NOW + 600
    with pytest.raises(gw.GatewayRefused) as e:
        g.submit(signed, i)
    assert e.value.code == "LIVE_TRADING_FLAG_EXPIRED"
    assert t.sent == [] and s._ledger.get(i["idempotency_key"]).state == od.SIGNED
    # Reconciliation is read-only and keeps working after the flag expired.
    assert g.reconcile(i["idempotency_key"])["state"] == od.SIGNED


def test_m4_gateway_reverifies_owner_signature_and_ignores_attributes(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner, flag_kw={"expires": NOW + 600})
    flag = g._flag
    # Constructed with another owner key: the flag's signature does not verify.
    kw = dict(transport=t, ledger=s._ledger, audit=AuditLog(str(tmp_path / "a2")), odds_ladder_step_size=125,
              network="mainnet", clock=clock)
    with pytest.raises(gw.GatewayDisabled, match="LIVE_TRADING_FLAG_SIGNATURE_INVALID"):
        gw.SubmissionGateway(flag, owner_public_key=Ed25519PrivateKey.generate().public_key(), **kw)
    with pytest.raises(gw.GatewayDisabled, match="LIVE_TRADING_OWNER_KEY_INVALID"):
        gw.SubmissionGateway(flag, owner_public_key=b"\x00" * 32, **kw)
    with pytest.raises(AttributeError):
        flag.enabled = True
    # Bytes edited in place (bypassing the immutability guard) to extend the expiry: refused on submit.
    body = json.loads(flag._message)
    body["expires_at"] = NOW + 3000
    object.__setattr__(flag, "_message", json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    clock.t = NOW + 700
    with pytest.raises(gw.GatewayRefused) as e:
        g.submit(signed, i)
    assert e.value.code == "LIVE_TRADING_FLAG_SIGNATURE_INVALID" and t.sent == []


def test_m4_expired_flag_refuses_construction(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    flag = enabled_flag(tmp_path, owner, expires=NOW + 60)
    with pytest.raises(gw.GatewayDisabled, match="LIVE_TRADING_FLAG_EXPIRED"):
        gw.SubmissionGateway(flag, owner_public_key=owner.public_key(), transport=FakeTransport(), ledger=s._ledger,
                             audit=AuditLog(str(tmp_path / "a")), odds_ladder_step_size=125, network="mainnet",
                             clock=lambda: NOW + 60)


def test_l3_poisoned_ledger_refuses_send_before_anything_is_sent(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner)
    s._ledger._poisoned = True
    with pytest.raises(gw.GatewayRefused) as e:
        g.submit(signed, i)
    assert e.value.code == "LEDGER_POISONED" and t.sent == []


# ================================================================ H3: REJECTED later found executed
def test_h3_rejected_400_later_found_filled_is_recorded_audited_and_replayed(tmp_path, throwaway, owner):
    s, i, signed, t, g, clock = setup(tmp_path, throwaway, owner)
    t.replies = [{"http_status": 400, "body": {"message": "PROXY_NOT_DEPLOYED"}}]
    assert g.submit(signed, i)["state"] == od.REJECTED
    oid = signed["order_id"]
    t.orders[oid] = row(oid, "INACTIVE", 0, "FILLED")
    out = g.reconcile(i["idempotency_key"])
    assert out["state"] == od.FILLED and out["changed"] is True
    decisions = [json.loads(l)["decision"] for l in (tmp_path / "gw-audit.jsonl").read_text().splitlines()]
    assert "EVIDENCE_REJECTED_ORDER_EXECUTED" in decisions
    clock.t = i["expiry"] + 10_000  # long after the rejected hold would have ended
    r = make_signer(tmp_path, throwaway, owner, clock=clock)  # restart: rebuilt by replay
    o = r._ledger.get(i["idempotency_key"])
    assert o.state == od.FILLED and o.filled == 5_000_000 and r.committed_exposure_base_units == 5_000_000
