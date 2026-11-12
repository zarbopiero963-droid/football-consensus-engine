"""S6 (issue #187): MCP -> persistent request queue -> isolated signer service -> gateway -> events.

Offline: in-memory queue with the Postgres semantics (tests/sxbet_queue_kit.py), the simulated exchange, throwaway
keys. Nothing here reaches SX Bet, mainnet or testnet; LIVE_TRADING stays UNAVAILABLE in the MCP.
"""
import json
import pathlib

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import cancellation as cx  # noqa: E402
from signer import killswitch as ks  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.request_queue import canonical_sha256, validate_request, QueueError  # noqa: E402
from signer.service import SignerService  # noqa: E402
from sxbet_e2e_kit import SIGNER_ID, USDC, Stack  # noqa: E402
from sxbet_queue_kit import MemoryQueue, cancel_request, place_request  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def s(tmp_path):
    return Stack(tmp_path)


def service(s, q, **kw):
    return SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock, **kw)


def enqueue_place(s, q, key, *, text=None, mutate=None, **kw):
    i = s.intent(key)
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx  # what the signer's OWN reads will see (test provider)
    auth = s.command(i, ctx, text=text)
    p = place_request(i, auth, created_at=int(s.clock()), **kw)
    if mutate:
        mutate(p)
    q.insert(p)
    return p


def test_place_request_is_signed_submitted_and_recorded_without_secrets(s):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-S6001")
    (res,) = service(s, q).process_once()
    assert q.states(p["request_id"]) == ["CLAIMED", "SIGNED", "SUBMITTED"]
    assert res["event"] == "SUBMITTED" and s.state("order-key-S6001") == od.SUBMITTED
    dump = json.dumps(q.events_of(p["request_id"]))
    assert "signature" not in dump and p["command"]["command_text"] not in dump and "0x" + "00" * 65 not in dump
    assert len([r for r in s.sim.writes() if r.method == "POST"]) == 1


def test_one_claim_per_request_two_instances_never_double_process(s):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-S6002")
    a, b = service(s, q), service(s, q)
    assert len(a.process_once()) == 1 and b.process_once() == []
    assert q.states(p["request_id"]).count("CLAIMED") == 1
    assert len([r for r in s.sim.writes() if r.method == "POST"]) == 1


@pytest.mark.parametrize("mutate,code", [
    (lambda p: p["intent"].update(total_bet_size="9000000"), "REQUEST_HASH_MISMATCH"),
    (lambda p: p.update(created_at=p["created_at"] - 3600), "REQUEST_EXPIRED"),
    (lambda p: p.update(extra="x"), "REQUEST_UNEXPECTED_FIELD"),
    (lambda p: p["command"].pop("client_ref_sha256"), "REQUEST_COMMAND_MALFORMED"),
])
def test_tampered_or_stale_requests_are_refused_before_signing(s, mutate, code):
    q = MemoryQueue()
    i = s.intent("order-key-S6003")
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    p = place_request(i, s.command(i, ctx), created_at=int(s.clock()))
    sha = canonical_sha256(p)            # what the MCP recorded
    mutate(p)                            # what someone altered afterwards (or a malformed writer)
    q.insert(p, sha=sha if code == "REQUEST_HASH_MISMATCH" else None)
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == code and s.ledger.get("order-key-S6003") is None


def test_command_that_does_not_match_the_order_is_refused_by_the_signer(s):
    q = MemoryQueue()
    i = s.intent("order-key-S6004")
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    other = s.intent("order-key-S6005", stake=7 * USDC)
    auth = s.command(other, ctx)          # the owner commanded 7 USDC, the intent says 5
    q.insert(place_request(i, auth, created_at=int(s.clock())))
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"].startswith("COMMAND_MISMATCH")


def test_a_command_reused_in_a_second_request_is_refused(s):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-S6006")
    service(s, q).process_once()
    i2 = s.intent("order-key-S6007")
    s.signer.config.market_data.world = s.context(i2)
    from signer import command as cmd
    reused = cmd.OwnerCommandAuthorization(**p["command"])
    q.insert(place_request(i2, reused, created_at=int(s.clock())))
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED", res
    assert res["detail"]["code"] == "COMMAND_ID_ALREADY_CONSUMED"


def test_kill_switch_engaged_refuses_new_orders_from_the_queue(s):
    q = MemoryQueue()
    ks.KillSwitch(s.ledger, AuditLog(str(s.tmp / "ks.jsonl")), owner_public_key=s.owner.public_key(),
                  network="mainnet", clock=s.clock).engage("OWNER_MANUAL_STOP")
    enqueue_place(s, q, "order-key-S6008")
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "KILL_SWITCH_ENGAGED"


def test_timeout_is_unknown_never_retried_then_the_outcome_is_learnt_from_reads(s):
    q = MemoryQueue()
    s.sim.inject("POST", "/orders-v3", "timeout_after")
    p = enqueue_place(s, q, "order-key-S6009")
    svc = service(s, q)
    (res,) = svc.process_once()
    assert res["event"] == "UNKNOWN" and len([r for r in s.sim.writes() if r.method == "POST"]) == 1
    assert svc.process_once() == []                       # never re-sent
    s.gw.reconcile("order-key-S6009")
    assert s.state("order-key-S6009") != od.UNKNOWN
    s.sim.advance(10 ** 6)                                 # expiry passes; reads confirm a final state
    s.gw.reconcile("order-key-S6009")
    svc.sync_outcomes()
    states = q.states(p["request_id"])
    assert states[:3] == ["CLAIMED", "SIGNED", "UNKNOWN"] and len([r for r in s.sim.writes() if r.method == "POST"]) == 1


def test_cancel_request_by_ids(s):
    q = MemoryQueue()
    i, sg, _ = s.place("order-key-S6010")
    s.gw.reconcile("order-key-S6010")
    auth = s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[sg["order_id"]])
    q.insert(cancel_request(auth, scope="ORDER_IDS", order_ids=[sg["order_id"]], created_at=int(s.clock())))
    (res,) = service(s, q).process_once()
    assert res["event"] == "DONE" and res["detail"]["orders"][sg["order_id"]]["bucket"] == cx.CANCELLED


def test_recovery_after_a_crash_between_signature_and_send(s, monkeypatch):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-S6011")
    svc = service(s, q)
    monkeypatch.setattr(s.gw, "submit", lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        svc.process_once()
    monkeypatch.undo()
    assert q.states(p["request_id"]) == ["CLAIMED", "SIGNED"]
    (res,) = service(s, q).recover()                       # the signature was never sent and is not re-sent
    assert res["event"] == "DONE" and res["detail"]["code"] == "INTERRUPTED_AFTER_SIGNATURE"
    assert [r for r in s.sim.writes() if r.method == "POST"] == []


def test_recovery_after_a_crash_before_the_signer_ran(s):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-S6012")
    assert q.claim(p["request_id"], SIGNER_ID)            # claimed, then the process died
    (res,) = service(s, q).recover()
    assert res["event"] == "SUBMITTED" and s.state("order-key-S6012") == od.SUBMITTED


def test_validate_request_rejects_any_key_or_signature_field(s):
    i = s.intent("order-key-S6013")
    p = place_request(i, s.command(i, s.context(i)), created_at=1)
    for bad in ("private_key", "signature", "risk_limits", "live_trading"):
        with pytest.raises(QueueError):
            validate_request({**p, bad: "x"})


def test_service_modules_import_no_network_or_database_library():
    for name in ("signer/service.py", "signer/request_queue.py"):
        src = (ROOT / name).read_text()
        for banned in ("import httpx", "import requests", "import urllib", "import socket", "import psycopg",
                       "SXBET_API_KEY", "PRIVATE_KEY"):
            assert banned not in src, (name, banned)


def test_a_request_for_the_other_network_is_never_taken_by_this_signer(s):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-S6014", network="testnet")
    assert service(s, q).process_once() == [] and q.states(p["request_id"]) == []
    assert s.ledger.get("order-key-S6014") is None


# ---------------------------------------------------------------- owner scenario: a compromised writer
# The MCP process or a compromised ChatGPT/Claude client inserts a fake, duplicated or altered command. Presence
# in the database proves nothing: the signer re-validates integrity, verifies the command against the order and
# ITS OWN reads, enforces single use and the short window, and the owner-signed limits. Residual risk (§2.6,
# accepted by the owner): a fully consistent command forged by an attacker who controls the chat text cannot be
# told apart cryptographically from the owner's; the mandatory limits and the kill switch bound the damage.
def _fresh(s, key, **kw):
    i = s.intent(key, **kw)
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    return i, ctx


def test_compromised_writer_altered_command_text_with_old_hash_is_refused(s):
    q = MemoryQueue()
    i, ctx = _fresh(s, "order-key-CW001")
    auth = s.command(i, ctx)
    p = place_request(i, auth, created_at=int(s.clock()))
    p["command"]["command_text"] = p["command"]["command_text"].replace("5", "50", 1)  # hash not updated
    q.insert(p)
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "COMMAND_TEXT_HASH_MISMATCH"
    assert s.ledger.get("order-key-CW001") is None


def test_compromised_writer_fake_command_for_a_bigger_order_is_refused(s):
    q = MemoryQueue()
    i, ctx = _fresh(s, "order-key-CW002", stake=5 * USDC)
    bigger = s.intent("order-key-CW002b", stake=9 * USDC)
    q.insert(place_request(i, s.command(bigger, ctx), created_at=int(s.clock())))  # text says 9, order says 5
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"].startswith("COMMAND_MISMATCH")


def test_compromised_writer_duplicates_a_request_with_a_new_id_is_refused(s):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-CW003")
    service(s, q).process_once()
    dup = dict(p, request_id="f" * 32)                     # same command, same order, new request id
    q.insert(dup)
    (res,) = service(s, q).process_once()
    # Review 1a9e09e P2-1 (intentional): a duplicate is a refusal, never reported with the old order's state.
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "DUPLICATE_REQUEST"
    assert "state" not in res["detail"] and len([r for r in s.sim.writes() if r.method == "POST"]) == 1


def test_compromised_writer_stale_command_replayed_later_is_refused(s):
    q = MemoryQueue()
    i, ctx = _fresh(s, "order-key-CW004")
    auth = s.command(i, ctx, received_at=int(s.clock()) - 3600)  # an old owner message
    q.insert(place_request(i, auth, created_at=int(s.clock())))
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "COMMAND_EXPIRED"


def test_compromised_writer_consistent_forged_command_is_still_bounded_by_owner_limits(s):
    """Documents the accepted residual risk: a consistent forged command passes the text checks, so the
    owner-signed limits are what refuse it (here: stake above the per-order limit)."""
    q = MemoryQueue()
    limit = s.signer._risk.limits.max_stake_per_order_base_units
    i, ctx = _fresh(s, "order-key-CW005", stake=limit + USDC)
    q.insert(place_request(i, s.command(i, ctx), created_at=int(s.clock())))
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"] in ("STAKE_ABOVE_LIMIT", "INSUFFICIENT_BALANCE")


def test_compromised_writer_cannot_carry_limits_keys_or_modes_in_a_request(s):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-CW006", mutate=lambda p: p.update(live_trading="ENABLED"))
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "REQUEST_UNEXPECTED_FIELD"


# ---------------------------------------------------------------- fixes from the independent review of 1a9e09e
def test_review_p2_1_same_key_different_intent_is_refused_never_reported_as_done(s):
    q = MemoryQueue()
    enqueue_place(s, q, "order-key-R6001")
    service(s, q).process_once()
    i2, ctx2 = _fresh(s, "order-key-R6001", stake=7 * USDC)   # same key, different stake
    q.insert(place_request(i2, s.command(i2, ctx2), created_at=int(s.clock())))
    (res,) = service(s, q).process_once()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"
    assert len([r for r in s.sim.writes() if r.method == "POST"]) == 1


def test_review_p2_2_failure_after_the_write_ahead_is_unknown_never_not_sent(s, monkeypatch):
    from signer.gateway import GatewayRefused
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-R6002")
    real = s.gw.submit

    def sent_then_ledger_failed(signed, intent):
        s.sim.inject("POST", "/orders-v3", "timeout_after")
        real(signed, intent)                                 # really sent; ledger UNKNOWN
        raise GatewayRefused("LEDGER_WRITE_FAILED")
    monkeypatch.setattr(s.gw, "submit", sent_then_ledger_failed)
    (res,) = service(s, q).process_once()
    assert res["event"] == "UNKNOWN" and res["detail"]["note"] == "RECONCILE_BEFORE_ANY_RETRY"
    assert "sent" not in res["detail"] and q.states(p["request_id"])[-1] == "UNKNOWN"


def test_review_p2_3_recover_never_runs_while_a_request_is_being_processed(s, monkeypatch):
    import threading
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-R6003")
    svc = service(s, q)
    gate, inside = threading.Event(), threading.Event()
    real = s.gw.submit

    def slow(signed, intent):
        inside.set()
        gate.wait(5)
        return real(signed, intent)
    monkeypatch.setattr(s.gw, "submit", slow)
    t = threading.Thread(target=svc.process_once)
    t.start()
    assert inside.wait(5)
    rec = []
    r = threading.Thread(target=lambda: rec.extend(svc.recover()))
    r.start()
    r.join(0.3)
    assert r.is_alive()                                      # waits for the service lock
    gate.set()
    t.join(5)
    r.join(5)
    assert rec == [] and q.states(p["request_id"]) == ["CLAIMED", "SIGNED", "SUBMITTED"]


def test_review_p2_4_repeated_recover_appends_nothing_new(s):
    q = MemoryQueue()
    s.sim.inject("POST", "/orders-v3", "timeout_after")
    p = enqueue_place(s, q, "order-key-R6004")
    svc = service(s, q)
    svc.process_once()
    before = q.states(p["request_id"])
    for _ in range(5):
        svc.recover()
    assert q.states(p["request_id"]) == before == ["CLAIMED", "SIGNED", "UNKNOWN"]


def test_review_guards_request_id_mismatch_future_created_at_and_row_network(s):
    q = MemoryQueue()
    i, ctx = _fresh(s, "order-key-R6005")
    p = place_request(i, s.command(i, ctx), created_at=int(s.clock()))
    q.insert(p, row_request_id="e" * 32)                       # row id differs from payload id
    i2, ctx2 = _fresh(s, "order-key-R6006")
    q.insert(place_request(i2, s.command(i2, ctx2), created_at=int(s.clock()) + 600))  # from the future
    i3, ctx3 = _fresh(s, "order-key-R6007")
    q.insert(place_request(i3, s.command(i3, ctx3), created_at=int(s.clock()), network="testnet"),
             row_network="mainnet")                            # row says mainnet, payload says testnet
    codes = sorted(r["detail"]["code"] for r in service(s, q).process_once())
    assert codes == ["REQUEST_EXPIRED", "REQUEST_HASH_MISMATCH", "REQUEST_NETWORK_MISMATCH"]


def test_review_recover_abandons_an_authorization_whose_signature_never_left(s, monkeypatch):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-R6008")
    # The process dies inside the signing step (BaseException): AUTHORIZED, no signature ever left.
    monkeypatch.setattr(s.signer, "_produce_signature", lambda msg: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        service(s, q).process_once()
    monkeypatch.undo()
    assert s.ledger.get("order-key-R6008").state == od.AUTHORIZED
    (res,) = service(s, q).recover()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "INTERRUPTED_BEFORE_SIGNATURE"
    assert s.ledger.get("order-key-R6008").state == od.REJECTED


def test_review_p3_cancel_failure_after_send_is_unknown(s, monkeypatch):
    from signer.gateway import GatewayRefused
    q = MemoryQueue()
    i, sg, _ = s.place("order-key-R6009")
    auth = s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[sg["order_id"]])
    monkeypatch.setattr(s.cx, "cancel_orders", lambda *a, **k: (_ for _ in ()).throw(GatewayRefused("X")))
    q.insert(cancel_request(auth, scope="ORDER_IDS", order_ids=[sg["order_id"]], created_at=int(s.clock())))
    (res,) = service(s, q).process_once()
    assert res["event"] == "UNKNOWN" and res["detail"]["note"].startswith("CANCEL_MAY_HAVE_BEEN_SENT")


# ---------------------------------------------------------------- fixes from the independent review of 67e80eb
def test_review_67e80eb_p2_b_recover_never_reports_another_requests_order(s):
    q = MemoryQueue()
    a = enqueue_place(s, q, "order-key-R7001")
    svc = service(s, q)
    svc.process_once()                                       # A placed
    i2, ctx2 = _fresh(s, "order-key-R7001", stake=7 * USDC)  # B: same key, different intent
    b = place_request(i2, s.command(i2, ctx2), created_at=int(s.clock()))
    q.insert(b)
    assert q.claim(b["request_id"], SIGNER_ID)              # claimed, then the process died
    (res,) = service(s, q).recover()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"
    assert "order_id" not in res["detail"] and q.states(a["request_id"])[-1] == "SUBMITTED"


def test_review_67e80eb_p3_3_two_service_objects_share_one_lock(s):
    q = MemoryQueue()
    assert service(s, q)._lock is service(s, q)._lock


# ---------------------------------------------------------------- P3 from the review of 6c74d55
def test_review_6c74d55_p3_1_malformed_command_id_does_not_stop_recovery(s):
    q = MemoryQueue()
    enqueue_place(s, q, "order-key-R8001")
    service(s, q).process_once()
    i2, ctx2 = _fresh(s, "order-key-R8001")
    bad = place_request(i2, s.command(i2, ctx2), created_at=int(s.clock()))
    bad["command"]["command_id"] = ["x"]
    q.insert(bad)
    assert q.claim(bad["request_id"], SIGNER_ID)
    out = service(s, q).recover()
    assert out and out[0]["event"] == "REFUSED"


def test_review_6c74d55_p3_4_a_failing_claim_never_blocks_the_next_requests(s, monkeypatch):
    q = MemoryQueue()
    a = enqueue_place(s, q, "order-key-R8002")
    b = enqueue_place(s, q, "order-key-R8003")
    real = q.claim
    monkeypatch.setattr(q, "claim", lambda rid, sid: (_ for _ in ()).throw(RuntimeError("lock timeout"))
                        if rid == a["request_id"] else real(rid, sid))
    out = service(s, q).process_once()
    assert out[0]["event"] == "CLAIM_FAILED" and out[0]["detail"] == {"code": "CLAIM_FAILED",
                                                                        "error_type": "RuntimeError"}
    assert "lock timeout" not in json.dumps(out)              # exception class only, never the message
    assert q.states(b["request_id"])[-1] == "SUBMITTED"


# ---------------------------------------------------------------- P3 from the review of 20970af
def test_review_20970af_p3_a_every_claim_failing_is_surfaced_not_silent(s, monkeypatch, caplog):
    from signer.service import QueueClaimUnavailable
    q = MemoryQueue()
    a = enqueue_place(s, q, "order-key-R9001")
    monkeypatch.setattr(q, "claim", lambda rid, sid: (_ for _ in ()).throw(PermissionError("secret dsn")))
    with caplog.at_level("WARNING"), pytest.raises(QueueClaimUnavailable):
        service(s, q).process_once()
    assert "PermissionError" in caplog.text and "secret dsn" not in caplog.text
    assert q.states(a["request_id"]) == [] and [r for r in s.sim.writes() if r.method == "POST"] == []
    assert service(s, MemoryQueue()).process_once() == []   # an empty queue is not a failure


def test_review_20970af_p3_d_claim_committed_then_raised_is_resumed_once_by_recover(s, monkeypatch):
    q = MemoryQueue()
    a = enqueue_place(s, q, "order-key-R9002")
    b = enqueue_place(s, q, "order-key-R9003")
    real = q.claim

    def committed_then_dropped(rid, sid):
        ok = real(rid, sid)
        if rid == a["request_id"]:
            raise ConnectionError("dropped after commit")
        return ok
    monkeypatch.setattr(q, "claim", committed_then_dropped)
    out = service(s, q).process_once()
    assert out[0]["event"] == "CLAIM_FAILED" and q.states(a["request_id"]) == ["CLAIMED"]
    monkeypatch.undo()
    assert [r["request_id"] for r in service(s, q).process_once()] == []  # no longer pending: not taken twice
    (res,) = service(s, q).recover()
    assert res["event"] == "SUBMITTED" and service(s, q).recover() == []
    posts = [r for r in s.sim.writes() if r.method == "POST"]
    assert len(posts) == 2                                       # one per request, never a second for A
    assert q.states(b["request_id"])[-1] == "SUBMITTED"


def _crash_on_refusal_event(q, monkeypatch):
    real = q.append_event

    def append(rid, state, detail, sid):
        if state == "REFUSED":
            raise SystemExit("crash before the refusal was recorded")
        return real(rid, state, detail, sid)
    monkeypatch.setattr(q, "append_event", append)


def test_review_20970af_p3_d_signing_failed_then_crash_is_refused_before_signature(s, monkeypatch):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-R9004")
    monkeypatch.setattr(s.signer, "_produce_signature", lambda msg: (_ for _ in ()).throw(RuntimeError("hsm")))
    _crash_on_refusal_event(q, monkeypatch)
    with pytest.raises(SystemExit):
        service(s, q).process_once()
    monkeypatch.undo()
    o = s.ledger.get("order-key-R9004")
    assert o.state == od.REJECTED and not o.signature_released and q.states(p["request_id"]) == ["CLAIMED"]
    (res,) = service(s, q).recover()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "REFUSED_BEFORE_SIGNATURE"
    assert [r for r in s.sim.writes() if r.method == "POST"] == []


def test_review_20970af_p3_d_abandon_then_crash_then_restart_is_refused_before_signature(s, monkeypatch):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-R9005")
    monkeypatch.setattr(s.signer, "_produce_signature", lambda msg: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        service(s, q).process_once()
    monkeypatch.undo()
    _crash_on_refusal_event(q, monkeypatch)                     # recover abandons, then dies before the event
    with pytest.raises(SystemExit):
        service(s, q).recover()
    monkeypatch.undo()
    assert s.ledger.get("order-key-R9005").state == od.REJECTED
    (res,) = service(s, q).recover()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "REFUSED_BEFORE_SIGNATURE"
    assert q.states(p["request_id"]) == ["CLAIMED", "REFUSED"]


def test_review_20970af_p3_d_rejected_after_the_signature_left_is_never_refused(s, monkeypatch):
    q = MemoryQueue()
    p = enqueue_place(s, q, "order-key-R9006")
    monkeypatch.setattr(s.gw, "submit", lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        service(s, q).process_once()
    monkeypatch.undo()
    s.ledger.transition("order-key-R9006", od.REJECTED, reason="TEST_REJECTED_AFTER_SIGNATURE")
    o = s.ledger.get("order-key-R9006")
    assert o.state == od.REJECTED and o.signature_released
    (res,) = service(s, q).recover()
    assert res["event"] == "UNKNOWN" and res["detail"].get("code") != "REFUSED_BEFORE_SIGNATURE"
    assert q.states(p["request_id"]) == ["CLAIMED", "SIGNED", "UNKNOWN"]
