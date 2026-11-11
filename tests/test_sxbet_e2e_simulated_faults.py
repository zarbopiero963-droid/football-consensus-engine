"""SIMULATED faults, crash points and concurrency on the full signer -> gateway -> SimExchange path (§2.6).

Timeouts, HTTP 500/429, delayed visibility and caller crashes are injected into tests/sxbet_sim_exchange.py.
Invariants checked with numbers: an ambiguous send is UNKNOWN (never "not placed"), reconciliation finds what
the exchange accepted, a retry never produces a second exchange order, a restart after a crash at every
write-ahead point replays to a consistent state with no lost reservation. SIMULATED, not native evidence.
"""
import multiprocessing
import threading

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import gateway as gw  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.core import Signer, SignerRefused  # noqa: E402
from signer.orders import LedgerError  # noqa: E402
from sxbet_e2e_kit import USDC, Stack  # noqa: E402
from sxbet_sim_exchange import SimCrash  # noqa: E402

KEY = "order-key-F001"


def signed(s, key=KEY, **kw):
    i = s.intent(key, **kw)
    return i, s.sign(i)


def code(fn, exc=gw.GatewayRefused):
    with pytest.raises(exc) as e:
        fn()
    return e.value.code


# ---------------------------------------------------------------- ambiguous replies
@pytest.mark.parametrize("fault", ["timeout_after", "http500_after"])
def test_simulated_timeout_or_500_after_accept_is_unknown_then_found_no_double_order(tmp_path, fault):
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.sim.inject("POST", "/orders-v3", fault)
    assert s.gw.submit(sg, i)["state"] == od.UNKNOWN
    assert s.exposure().total == 5 * USDC                                   # UNKNOWN never releases
    assert code(lambda: s.gw.submit(sg, i)) == "STATE_UNKNOWN_RECONCILE_FIRST"
    assert s.gw.reconcile(KEY)["state"] == od.OPEN                           # the exchange had accepted it
    assert s.gw.submit(sg, i)["sent"] is False
    assert s.sim.accepted_count(KEY) == 1 and len(s.sim.writes()) == 1


@pytest.mark.parametrize("fault", ["timeout_before", "http500_before", "http429"])
def test_simulated_not_accepted_reconciled_absent_then_same_order_resent_once(tmp_path, fault):
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.sim.inject("POST", "/orders-v3", fault)
    assert s.gw.submit(sg, i)["state"] == od.UNKNOWN
    s.sim.advance(10)
    assert s.gw.reconcile(KEY)["state"] == od.UNKNOWN                        # 404 within the settle window: nothing
    s.sim.advance(od.ABSENT_SETTLE_SECONDS)
    assert s.gw.reconcile(KEY)["state"] == od.SIGNED                         # reconciled absent: re-send allowed
    assert s.exposure().total == 5 * USDC
    assert s.gw.submit(sg, i)["state"] == od.SUBMITTED                       # the SAME signed order, same id
    posts = [r for r in s.sim.writes() if r.method == "POST"]
    assert len(posts) == 2 and posts[0].body == posts[1].body
    assert s.sim.accepted_count(KEY) == 1


def test_simulated_delayed_visibility_beyond_settle_window_still_one_order(tmp_path):
    """Worst case: accepted, reply lost, invisible to reads for 90 s (> the 60 s settle window). The re-send is
    refused by the exchange (clientOrderId collision), the ledger holds the stake, and the order is found later."""
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.sim.inject("POST", "/orders-v3", "timeout_after", visibility_delay=90)
    assert s.gw.submit(sg, i)["state"] == od.UNKNOWN
    s.sim.advance(od.ABSENT_SETTLE_SECONDS + 1)
    assert s.gw.reconcile(KEY)["state"] == od.SIGNED                         # 404 by id and clientOrderId
    assert s.gw.submit(sg, i)["state"] == od.REJECTED                        # 400 DUPLICATE_CLIENT_ORDER_ID
    assert s.exposure().total == 5 * USDC                                    # signature out: still reserved
    s.sim.advance(30)
    assert s.gw.reconcile(KEY)["state"] == od.OPEN                           # H3: found live after all
    assert s.sim.accepted_count(KEY) == 1 and s.exposure().total == 5 * USDC


def test_simulated_read_errors_learn_nothing(tmp_path):
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.sim.inject("POST", "/orders-v3", "timeout_after")
    s.gw.submit(sg, i)
    s.sim.advance(120)
    s.sim.inject("GET", "/orders-v3/", "get_error")
    assert s.gw.reconcile(KEY)["state"] == od.UNKNOWN and s.exposure().total == 5 * USDC
    assert s.gw.reconcile(KEY)["state"] == od.OPEN


# ---------------------------------------------------------------- crash at every write-ahead point, then restart
def _restart_consistent(s, key, state, reserved):
    s.restart()
    o = s.ledger.get(key)
    assert o.state == state and s.exposure().total == reserved
    return o


def test_simulated_crash_after_authorize_before_signature(tmp_path, monkeypatch):
    s = Stack(tmp_path)
    i = s.intent(KEY)
    ctx = s.context(i)
    approval = s.command(i, ctx)

    def die(self, msg):
        raise SimCrash("signer process killed while signing")

    monkeypatch.setattr(Signer, "_produce_signature", die)
    with pytest.raises(SimCrash):
        s.signer.sign_order(i, approval, ctx)
    monkeypatch.undo()
    _restart_consistent(s, KEY, od.AUTHORIZED, 5 * USDC)                    # reservation not lost
    assert s.ledger.command_consumed(approval.command_id)
    with pytest.raises(SignerRefused) as e:
        s.signer.sign_order(i, approval, s.context(i))
    assert e.value.code == "IDEMPOTENCY_KEY_SIGNED_BEFORE_RESTART_RECONCILE_FIRST"
    assert s.signer.abandon_unsigned(KEY) == od.REJECTED and s.exposure().total == 0
    assert s.sim.writes() == []


def test_simulated_crash_after_signed_line_before_signature_returned(tmp_path, monkeypatch):
    s = Stack(tmp_path)
    i = s.intent(KEY)
    real = Signer._log

    def log(self, decision, *a, **kw):
        if decision == "SIGNED":
            raise SimCrash("killed after the SIGNED line, before returning the signature")
        return real(self, decision, *a, **kw)

    monkeypatch.setattr(Signer, "_log", log)
    with pytest.raises(SimCrash):
        s.sign(i)
    monkeypatch.undo()
    _restart_consistent(s, KEY, od.SIGNED, 5 * USDC)
    with pytest.raises(SignerRefused):
        s.signer.abandon_unsigned(KEY)            # SIGNED may exist outside: never released by a local decision
    # Only exchange evidence releases it: 404 + no fills after expiry -> EXPIRED_UNCONFIRMED (still reserved),
    # re-observed after the confirmation window -> EXPIRED.
    s.sim.advance(3600 + od.EXPIRY_GRACE_SECONDS + 1)
    assert s.gw.reconcile(KEY)["state"] == od.EXPIRED_UNCONFIRMED and s.exposure().total == 5 * USDC
    s.sim.advance(od.EXPIRY_CONFIRM_SECONDS)
    assert s.gw.reconcile(KEY)["state"] == od.EXPIRED and s.exposure().total == 0
    assert s.sim.writes() == []


def test_simulated_crash_after_write_ahead_before_send(tmp_path):
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.sim.inject("POST", "/orders-v3", "crash_before")
    with pytest.raises(SimCrash):
        s.gw.submit(sg, i)
    _restart_consistent(s, KEY, od.UNKNOWN, 5 * USDC)
    s.sim.advance(od.ABSENT_SETTLE_SECONDS + 1)
    assert s.gw.reconcile(KEY)["state"] == od.SIGNED
    assert s.gw.submit(sg, i)["state"] == od.SUBMITTED and s.sim.accepted_count(KEY) == 1


def test_simulated_crash_after_exchange_accepted_before_reply_recorded(tmp_path):
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.sim.inject("POST", "/orders-v3", "crash_after")
    with pytest.raises(SimCrash):
        s.gw.submit(sg, i)
    _restart_consistent(s, KEY, od.UNKNOWN, 5 * USDC)
    assert code(lambda: s.gw.submit(sg, i)) == "STATE_UNKNOWN_RECONCILE_FIRST"
    assert s.gw.reconcile(KEY)["state"] == od.OPEN
    assert s.gw.submit(sg, i)["sent"] is False and s.sim.accepted_count(KEY) == 1


def test_simulated_crash_after_reply_recorded(tmp_path, monkeypatch):
    s = Stack(tmp_path)
    i, sg = signed(s)
    real = gw.SubmissionGateway._log

    def log(self, decision, *a, **kw):
        if decision == od.SUBMITTED:
            raise SimCrash("killed after the SUBMITTED line")
        return real(self, decision, *a, **kw)

    monkeypatch.setattr(gw.SubmissionGateway, "_log", log)
    with pytest.raises(SimCrash):
        s.gw.submit(sg, i)
    monkeypatch.undo()
    _restart_consistent(s, KEY, od.SUBMITTED, 5 * USDC)
    assert s.gw.submit(sg, i)["sent"] is False and s.sim.accepted_count(KEY) == 1


def test_simulated_crash_mid_cancel_after_command_consumed(tmp_path):
    from signer import cancellation as cx
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.gw.submit(sg, i)
    oid = sg["order_id"]
    a = s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid])
    s.sim.inject("DELETE", "/orders-v3", "crash_before")
    with pytest.raises(SimCrash):
        s.cx.cancel_orders([oid], a)
    _restart_consistent(s, KEY, od.OPEN, 5 * USDC)                          # nothing cancelled, still reserved
    assert s.ledger.command_consumed(a.command_id)
    with pytest.raises(cx.CancelRefused) as e:
        s.cx.cancel_orders([oid], a)
    assert e.value.code == "COMMAND_ID_ALREADY_CONSUMED"                      # a new owner command is needed
    rep = s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["orders"][oid]["state"] == od.CANCELLED and s.exposure().total == 0


# ---------------------------------------------------------------- concurrency
def test_simulated_concurrent_duplicate_submissions_threads_one_exchange_order(tmp_path):
    s = Stack(tmp_path)
    i, sg = signed(s)
    barrier = threading.Barrier(8)
    out = []

    def go():
        barrier.wait()
        try:
            out.append(s.gw.submit(sg, i)["sent"])
        except gw.GatewayRefused as exc:
            out.append(exc.code)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert out.count(True) == 1 and len(out) == 8
    assert s.sim.accepted_count(KEY) == 1 and len([r for r in s.sim.writes() if r.method == "POST"]) == 1


def test_simulated_concurrent_distinct_orders_respect_open_order_limit(tmp_path):
    s = Stack(tmp_path)
    keys = [f"order-key-C{n:03d}" for n in range(8)]
    pre = []
    for k in keys:
        i = s.intent(k, stake=2 * USDC)
        ctx = s.context(i)
        pre.append((i, s.command(i, ctx), ctx))
    barrier = threading.Barrier(len(pre))
    results = []

    def go(i, a, ctx):
        barrier.wait()
        try:
            sg = s.signer.sign_order(i, a, ctx)
            results.append(s.gw.submit(sg, i)["state"])
        except SignerRefused as exc:
            results.append(exc.code)

    ts = [threading.Thread(target=go, args=p) for p in pre]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert results.count(od.SUBMITTED) == 5 and results.count("OPEN_ORDERS_ABOVE_LIMIT") == 3
    assert s.exposure().total == 10 * USDC and s.sim.accepted_count() == 5


def _child(handoff, signed_order, intent, barrier, out_path):
    # Module-level and fed only picklable data: runs in a "spawn" child (fresh interpreter, no inherited threads).
    try:
        stack = Stack.from_handoff(handoff)      # same throwaway keys, clock, simulated world and ledger path
        barrier.wait(30)
        try:
            stack._build()                       # this process starts a signer on the shared ledger file
        except LedgerError as exc:
            barrier.wait(30)
            out_path.write_text(exc.code)
            return
        barrier.wait(30)                         # the other process has tried (and failed) to open it
        r = stack.gw.submit(signed_order, intent)
        out_path.write_text(f"{r['state']}:{stack.sim.accepted_count()}")
    except BaseException as exc:  # pragma: no cover - diagnostic only
        out_path.write_text("ERROR:" + type(exc).__name__)


def test_simulated_concurrent_duplicate_submissions_processes_one_send(tmp_path):
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.ledger.close()                             # the parent signer stops; two processes race for the ledger
    # "spawn", not "fork": forking a multi-threaded pytest process can deadlock the child (DeprecationWarning
    # since Python 3.12) and is Linux-only. Each child is a fresh interpreter, like a real second signer process.
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    outs = [tmp_path / f"child-{n}.txt" for n in range(2)]
    handoff = s.handoff()
    procs = [ctx.Process(target=_child, args=(handoff, sg, i, barrier, outs[n])) for n in range(2)]
    [p.start() for p in procs]
    [p.join(120) for p in procs]
    assert [p.exitcode for p in procs] == [0, 0]
    results = sorted(o.read_text() for o in outs)
    assert results == ["LEDGER_LOCKED", "SUBMITTED:1"], results
    s._build()                                   # replay what the winning process wrote
    assert s.state(KEY) == od.SUBMITTED
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert sum('"reason":"SEND_ATTEMPT"' in ln for ln in lines) == 1  # exactly one send attempt, ever


def test_simulated_two_lost_sends_then_third_send_accepted_once(tmp_path):
    """Regression of the absence-evidence dedupe (tests/test_sxbet_orders_absence_evidence.py) on the full path."""
    s = Stack(tmp_path)
    i, sg = signed(s)
    for _ in range(2):
        s.sim.inject("POST", "/orders-v3", "timeout_before")
        assert s.gw.submit(sg, i)["state"] == od.UNKNOWN
        s.sim.advance(od.ABSENT_SETTLE_SECONDS + 1)
        assert s.gw.reconcile(KEY)["state"] == od.SIGNED
    assert s.gw.submit(sg, i)["state"] == od.SUBMITTED
    assert s.sim.accepted_count(KEY) == 1 and s.exposure().total == 5 * USDC


def test_simulated_signed_never_sent_is_released_only_after_expiry_confirmation(tmp_path):
    s = Stack(tmp_path)
    i, sg = signed(s)
    s.sim.inject("POST", "/orders-v3", "http429")
    s.gw.submit(sg, i)
    s.sim.advance(od.ABSENT_SETTLE_SECONDS + 1)
    assert s.gw.reconcile(KEY)["state"] == od.SIGNED                # the owner's process decides not to re-send
    s.sim.advance(3600)
    assert s.gw.reconcile(KEY)["state"] == od.EXPIRED_UNCONFIRMED and s.exposure().total == 5 * USDC
    s.sim.advance(od.EXPIRY_CONFIRM_SECONDS)
    assert s.gw.reconcile(KEY)["state"] == od.EXPIRED and s.exposure().total == 0
