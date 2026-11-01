"""FIX 1: order lifecycle state machine, exposure release from exchange evidence, ledger rebuild.

Exchange inputs use the OpenAPI field names (OrderV3 / FillV3, https://docs.sx.bet/openapi.json).
Offline and deterministic.
"""
import json

import pytest

pytest.importorskip("cryptography")

from signer import orders as od  # noqa: E402
from signer.orders import LedgerError, OrderLedger, OrderStateError, evidence_from_exchange  # noqa: E402

SIGNER_ID = "signer-test-1"
ADDR = "0x" + "aa" * 20
OID = "0x" + "77" * 32
MKT = "0x" + "ab" * 32
NOW = 1_800_000_000
STAKE = 5_000_000


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def led(tmp_path, clock):
    return OrderLedger.create(str(tmp_path / "ledger.jsonl"), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock)


def reopen(tmp_path, clock):
    return OrderLedger.open(str(tmp_path / "ledger.jsonl"), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock)


def authorize(led, key="order-key-0001", oid=OID, stake=STAKE, nonce="nonce-0000000000000001", event="L1",
              market=MKT, expiry=NOW + 3600):
    return led.authorize(client_order_id=key, intent_hash="sha256:" + "1" * 64, order_id=oid, market_hash=market,
                         event_id=event, stake=stake, expiry=expiry, approval_nonce=nonce)


def submitted(led, key="order-key-0001"):
    led.transition(key, od.SIGNED, reason="SIGNED")
    led.transition(key, od.UNKNOWN, reason="SEND_ATTEMPT")
    return led.transition(key, od.SUBMITTED, reason="EXCHANGE_SUBMITTED")


def row(status, remaining, reason=None, oid=OID, total=STAKE):
    return {"kind": "FOUND", "order": {"id": oid, "marketHash": MKT, "totalBetSize": str(total),
                                       "remainingSize": str(remaining), "status": status, "inactiveReason": reason}}


def fills(*items, complete=True):
    return {"kind": "OK", "fills": list(items), "complete": complete}


def fill(amount, status="SETTLED", ret=None, oid=OID):
    return {"orderId": oid, "fillAmount": str(amount), "status": status,
            "settlement": None if ret is None else {"outcome": 1, "settleReturnAmount": str(ret)}}


def reconcile(led, key, clock, lookup, f=None, client=None):
    o = led.get(key)
    ev = evidence_from_exchange(o, lookup=lookup, client_lookup=client, fills=f, now=int(clock()))
    return led.apply_evidence(key, ev)


# ---------------------------------------------------------------- exposure lifecycle
def test_reserved_at_full_stake_from_authorized_to_open(led, clock):
    authorize(led)
    assert led.exposure().total == STAKE and led.exposure().reserved_unmatched == STAKE
    submitted(led)
    o = reconcile(led, "order-key-0001", clock, row("ACTIVE", STAKE))
    assert o.state == od.OPEN and led.exposure().total == STAKE and led.exposure().open_orders == 1


def test_cancel_confirmed_by_exchange_releases_everything(led, clock):
    authorize(led)
    submitted(led)
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", STAKE, "USER_REQUESTED"))
    assert o.state == od.CANCELLED and o.filled == 0
    v = led.exposure()
    assert v.total == 0 and v.open_orders == 0 and v.by_market == {}


def test_partial_fill_then_cancel(led, clock):
    authorize(led)
    submitted(led)
    o = reconcile(led, "order-key-0001", clock, row("ACTIVE", 3_000_000))
    assert o.state == od.PARTIALLY_FILLED
    v = led.exposure()
    assert (v.matched, v.reserved_unmatched, v.total) == (2_000_000, 3_000_000, STAKE)
    # A further fill keeps PARTIALLY_FILLED with a larger matched part.
    o = reconcile(led, "order-key-0001", clock, row("ACTIVE", 1_000_000))
    assert o.state == od.PARTIALLY_FILLED and o.filled == 4_000_000
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", 1_000_000, "USER_REQUESTED"))
    assert o.state == od.CANCELLED
    v = led.exposure()
    assert (v.matched, v.reserved_unmatched, v.total) == (4_000_000, 0, 4_000_000)  # matched stays until SETTLED
    # Settlement from complete, settled fills: matched exposure released, P&L recorded.
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", 1_000_000, "USER_REQUESTED"),
                  fills(fill(1_500_000, ret=0), fill(2_500_000, ret=0)))
    assert o.state == od.SETTLED and o.realized_pnl == -4_000_000 and led.exposure().total == 0
    assert led.realized_pnl_since(NOW) == -4_000_000


def test_full_fill_then_settle(led, clock):
    authorize(led)
    submitted(led)
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", 0, "FILLED"), fills(fill(STAKE, status="LOCKED")))
    assert o.state == od.FILLED and led.exposure().matched == STAKE and led.exposure().reserved_unmatched == 0
    clock.t += 7200
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", 0, "FILLED"), fills(fill(STAKE, ret=12_500_000)))
    assert o.state == od.SETTLED and o.realized_pnl == 7_500_000 and led.exposure().total == 0


@pytest.mark.parametrize("bad_fills", [
    fills(fill(STAKE, status="FAILED", ret=0)),             # FAILED fill: not settled
    fills(fill(STAKE, ret=None)),                           # settlement missing
    fills(fill(STAKE - 1, ret=0)),                          # sum(fillAmount) != matched (DOCS_UNVERIFIED rule)
    fills(fill(STAKE, ret=0), complete=False),              # more pages: not proven
    fills(fill(STAKE, ret=0, oid="0x" + "99" * 32)),        # another order's fill
    {"kind": "ERROR"},
])
def test_settlement_requires_complete_consistent_evidence(led, clock, bad_fills):
    authorize(led)
    submitted(led)
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", 0, "FILLED"), bad_fills)
    assert o.state == od.FILLED and led.exposure().matched == STAKE


def test_expiry_from_exchange(led, clock):
    authorize(led)
    submitted(led)
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", STAKE, "EXPIRED"))
    assert o.state == od.EXPIRED and led.exposure().total == 0


def test_not_found_after_expiry_with_no_fills_does_not_release(led, clock):
    """Intentional change (review M2): a 404 after expiry no longer releases the stake by itself."""
    authorize(led, expiry=NOW + 100)
    submitted(led)
    o = reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills())
    assert o.state == od.SUBMITTED  # before expiry a 404 proves nothing
    clock.t = NOW + 100 + od.EXPIRY_GRACE_SECONDS + 1
    assert reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills(complete=False)).state == od.SUBMITTED
    o = reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills())
    assert o.state == od.EXPIRED_UNCONFIRMED and led.exposure().total == STAKE


@pytest.mark.parametrize("inactive_reason", ["NO_LIQUIDITY", "INSUFFICIENT_BALANCE", "EVENT_LIFECYCLE",
                                             "MARKET_HALTED", "HEARTBEAT_TIMEOUT", "POST_ONLY_WOULD_CROSS", "SYSTEM"])
def test_other_inactive_reasons_are_cancellations(led, clock, inactive_reason):
    authorize(led)
    submitted(led)
    assert reconcile(led, "order-key-0001", clock, row("INACTIVE", STAKE, inactive_reason)).state == od.CANCELLED


def test_unknown_inactive_reason_or_status_learns_nothing(led, clock):
    authorize(led)
    submitted(led)
    assert reconcile(led, "order-key-0001", clock, row("INACTIVE", STAKE, "SOMETHING_NEW")).state == od.SUBMITTED
    assert reconcile(led, "order-key-0001", clock, row("WEIRD", STAKE)).state == od.SUBMITTED
    assert reconcile(led, "order-key-0001", clock, row("ACTIVE", STAKE, oid="0x" + "55" * 32)).state == od.SUBMITTED
    assert reconcile(led, "order-key-0001", clock, row("ACTIVE", STAKE, total=STAKE + 1)).state == od.SUBMITTED
    assert led.exposure().total == STAKE


# ---------------------------------------------------------------- UNKNOWN (timeout) reconciliation, both ways
def test_timeout_unknown_then_exchange_shows_order_live(led, clock):
    authorize(led)
    led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    led.transition("order-key-0001", od.UNKNOWN, reason="SEND_ATTEMPT")
    assert led.exposure().total == STAKE  # UNKNOWN never releases
    for lookup in ({"kind": "ERROR"}, {"kind": "NOT_FOUND"}):
        assert reconcile(led, "order-key-0001", clock, lookup, fills()).state == od.UNKNOWN
    o = reconcile(led, "order-key-0001", clock, row("ACTIVE", 4_000_000))
    assert o.state == od.PARTIALLY_FILLED and led.exposure().total == STAKE


def test_timeout_unknown_then_reconciled_absent_reopens_same_order_only(led, clock):
    authorize(led)
    led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    led.transition("order-key-0001", od.UNKNOWN, reason="SEND_ATTEMPT")
    nf = {"kind": "NOT_FOUND"}
    # Too soon after the send: still UNKNOWN.
    clock.t = NOW + od.ABSENT_SETTLE_SECONDS - 1
    assert reconcile(led, "order-key-0001", clock, nf, fills(), client=nf).state == od.UNKNOWN
    # Found by clientOrderId lookup is not "absent".
    clock.t = NOW + od.ABSENT_SETTLE_SECONDS + 1
    assert reconcile(led, "order-key-0001", clock, nf, fills(), client={"kind": "ERROR"}).state == od.UNKNOWN
    o = reconcile(led, "order-key-0001", clock, nf, fills(), client=nf)
    assert o.state == od.SIGNED and led.exposure().total == STAKE  # re-sendable, still fully reserved


def test_unknown_with_fills_never_reconciled_absent(led, clock):
    authorize(led)
    led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    led.transition("order-key-0001", od.UNKNOWN, reason="SEND_ATTEMPT")
    clock.t = NOW + 3600 * 5
    o = reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills(fill(1, status="MATCHED")),
                  client={"kind": "NOT_FOUND"})
    assert o.state == od.UNKNOWN and led.exposure().total == STAKE


# ---------------------------------------------------------------- idempotency, illegal transitions
def test_duplicate_evidence_is_idempotent(tmp_path, led, clock):
    authorize(led)
    submitted(led)
    before = len((tmp_path / "ledger.jsonl").read_text().splitlines())
    for _ in range(3):
        reconcile(led, "order-key-0001", clock, row("ACTIVE", 3_000_000))
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == before + 1
    # Different evidence that changes nothing writes nothing either.
    reconcile(led, "order-key-0001", clock, {"kind": "FOUND", "order": {**row("ACTIVE", 3_000_000)["order"],
                                                                         "updatedAt": "x"}})
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == before + 1


@pytest.mark.parametrize("path,to", [
    ([], od.SUBMITTED),                                          # AUTHORIZED -> SUBMITTED (skips SIGNED)
    ([], od.FILLED),
    ([od.SIGNED, od.SUBMITTED, od.CANCELLED], od.OPEN),          # terminal cancel cannot reopen
    ([od.SIGNED, od.REJECTED], od.SIGNED),                       # REJECTED is terminal
    ([od.SIGNED, od.SUBMITTED], od.SETTLED),                     # settle requires inactive + matched
    ([od.SIGNED, od.SUBMITTED, od.OPEN], od.SUBMITTED),          # no going back
    ([od.SIGNED, od.SUBMITTED, od.EXPIRED], od.SETTLED),         # nothing matched => nothing to settle
])
def test_illegal_transitions_refused_and_not_written(tmp_path, led, path, to):
    authorize(led)
    for st in path:
        led.transition("order-key-0001", st, reason="TEST", filled=0)
    size = (tmp_path / "ledger.jsonl").stat().st_size
    with pytest.raises(OrderStateError):
        led.transition("order-key-0001", to, reason="TEST", realized_pnl=0 if to == od.SETTLED else None)
    assert (tmp_path / "ledger.jsonl").stat().st_size == size


def test_filled_never_decreases_and_never_exceeds_stake(led, clock):
    authorize(led)
    submitted(led)
    led.transition("order-key-0001", od.PARTIALLY_FILLED, reason="T", filled=3_000_000)
    with pytest.raises(OrderStateError):
        led.transition("order-key-0001", od.PARTIALLY_FILLED, reason="T", filled=2_000_000)
    with pytest.raises(OrderStateError):
        led.transition("order-key-0001", od.FILLED, reason="T", filled=STAKE + 1)
    # Exchange evidence going backwards is refused too (state unchanged).
    with pytest.raises(OrderStateError):
        reconcile(led, "order-key-0001", clock, row("ACTIVE", STAKE))
    assert led.get("order-key-0001").filled == 3_000_000


def test_duplicate_authorize_refused(led):
    authorize(led)
    with pytest.raises(OrderStateError, match="IDEMPOTENCY_KEY_ALREADY_AUTHORIZED"):
        authorize(led, nonce="nonce-0000000000000002", oid="0x" + "78" * 32)
    with pytest.raises(OrderStateError, match="APPROVAL_NONCE_REPLAYED"):
        authorize(led, key="order-key-0002", oid="0x" + "78" * 32)
    with pytest.raises(OrderStateError, match="ORDER_ID_ALREADY_AUTHORIZED"):
        authorize(led, key="order-key-0002", nonce="nonce-0000000000000002")


# ---------------------------------------------------------------- per market / event aggregation
def test_exposure_by_market_event_and_open_orders(led):
    authorize(led)
    authorize(led, key="order-key-0002", oid="0x" + "78" * 32, nonce="nonce-0000000000000002", stake=1_000_000,
              market="0x" + "cd" * 32, event="L1")
    authorize(led, key="order-key-0003", oid="0x" + "79" * 32, nonce="nonce-0000000000000003", stake=2_000_000,
              market="0x" + "cd" * 32, event="L2")
    v = led.exposure()
    assert v.total == 8_000_000 and v.open_orders == 3
    assert v.by_market == {MKT: STAKE, "0x" + "cd" * 32: 3_000_000}
    assert v.by_event == {"L1": 6_000_000, "L2": 2_000_000}


# ---------------------------------------------------------------- crash/restart rebuild + tamper
def test_restart_rebuilds_state_exposure_nonces_and_pnl(tmp_path, led, clock):
    authorize(led)
    submitted(led)
    reconcile(led, "order-key-0001", clock, row("ACTIVE", 2_000_000))
    authorize(led, key="order-key-0002", oid="0x" + "78" * 32, nonce="nonce-0000000000000002")
    led.transition("order-key-0002", od.SIGNED, reason="SIGNED")
    led.transition("order-key-0002", od.UNKNOWN, reason="SEND_ATTEMPT")
    before = (led.exposure().total, led.exposure().matched, [(o.client_order_id, o.state, o.filled)
                                                             for o in led.orders()])
    led.close()  # the previous process is gone (it held the exclusive file lock)
    r = reopen(tmp_path, clock)
    after = (r.exposure().total, r.exposure().matched, [(o.client_order_id, o.state, o.filled) for o in r.orders()])
    assert before == after and after[0] == 2 * STAKE
    assert r.nonce_consumed("nonce-0000000000000002")
    assert r.get("order-key-0002").last_send_at == NOW
    # Applying the same evidence after restart is still a no-op.
    size = (tmp_path / "ledger.jsonl").stat().st_size
    reconcile(r, "order-key-0001", clock, row("ACTIVE", 2_000_000))
    assert (tmp_path / "ledger.jsonl").stat().st_size == size


@pytest.mark.parametrize("tamper", ["edit", "delete_middle", "reorder", "garbage", "blank"])
def test_tampered_ledger_refused(tmp_path, led, clock, tamper):
    authorize(led)
    submitted(led)
    reconcile(led, "order-key-0001", clock, row("INACTIVE", STAKE, "USER_REQUESTED"))
    led.close()
    p = tmp_path / "ledger.jsonl"
    lines = p.read_text().splitlines()
    if tamper == "edit":
        lines[1] = lines[1].replace('"stake":"5000000"', '"stake":"1"')
    elif tamper == "delete_middle":
        del lines[2]
    elif tamper == "reorder":
        lines[2], lines[3] = lines[3], lines[2]
    elif tamper == "garbage":
        lines.append("{not json")
    else:
        lines.insert(2, "")
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerError):
        reopen(tmp_path, clock)


def test_rechained_illegal_history_still_refused(tmp_path, led, clock):
    """Even a correctly re-hashed ledger is replayed through the state machine."""
    authorize(led)
    led.close()
    p = tmp_path / "ledger.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    rows.append({"kind": "TRANSITION", "client_order_id": "order-key-0001", "from": "AUTHORIZED", "to": "SETTLED",
                 "filled": 0, "reason": "FORGED", "realized_pnl": 0, "ts": NOW})
    prev, out = od.GENESIS_PREV, []
    for r in rows:
        r["prev_sha256"] = prev
        line = od._canon(r)
        out.append(line)
        prev = od._line_hash(line)
    p.write_text("\n".join(out) + "\n")
    with pytest.raises(LedgerError, match="LEDGER_CORRUPT"):
        reopen(tmp_path, clock)


def test_states_and_transition_table_are_closed():
    assert set(od.TRANSITIONS) == od.STATES == {
        "PREPARED", "AUTHORIZED", "SIGNED", "SUBMITTED", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELLED",
        "EXPIRED", "EXPIRED_UNCONFIRMED", "SETTLED", "REJECTED", "UNKNOWN"}
    assert all(t <= od.STATES for t in od.TRANSITIONS.values())
    assert od.TRANSITIONS[od.SETTLED] == od.TRANSITIONS[od.REJECTED] == frozenset()
    assert od.UNKNOWN in od.RESERVING  # UNKNOWN never releases capital
    assert od.EXPIRED_UNCONFIRMED in od.RESERVING  # a 404 after expiry never releases capital by itself


# ================================================================ review fixes (M1, M2, M3, L3)
NONCE_X = "nonce-shared-000000X1"


def _ledger_path(tmp_path):
    return str(tmp_path / "ledger.jsonl")


# ---------------------------------------------------------------- M1: one writer per ledger file
def test_m1_second_instance_on_same_file_is_refused(tmp_path, led, clock):
    with pytest.raises(LedgerError) as e:
        reopen(tmp_path, clock)
    assert e.value.code == "LEDGER_LOCKED"


def test_m1_two_instances_never_both_accept_the_same_nonce(tmp_path, led, clock):
    accepted = 0
    try:
        other = reopen(tmp_path, clock)
    except LedgerError as e:
        assert e.code == "LEDGER_LOCKED"
        other = None
    authorize(led, nonce=NONCE_X)
    accepted += 1
    if other is not None:
        try:
            authorize(other, key="order-key-0002", oid="0x" + "78" * 32, nonce=NONCE_X)
            accepted += 1
        except (LedgerError, OrderStateError):
            pass
    assert accepted == 1
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert sum(1 for r in rows if r.get("approval_nonce") == NONCE_X) == 1


_CHILD = r"""
import sys
from signer.orders import LedgerError, OrderLedger
mode, path, addr = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    led = OrderLedger.open(path, signer_id="signer-test-1", signer_address=addr, clock=lambda: 1800000000)
except LedgerError as e:
    print(e.code, flush=True)
    sys.exit(0)
print("OPENED", flush=True)
if mode == "hold":
    sys.stdin.readline()
"""


def _child(mode, path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[1])
    env = {k: v for k, v in os.environ.items() if k not in ("RENDER", "DATABASE_URL")}
    env.update(SXBET_SIGNER_PROCESS="1", PYTHONPATH=root)
    return subprocess.Popen([sys.executable, "-c", _CHILD, mode, path, ADDR], cwd=root, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def test_m1_other_process_cannot_open_a_held_ledger(tmp_path, led):
    p = _child("probe", _ledger_path(tmp_path))
    out, _ = p.communicate(timeout=60)
    assert out.strip() == "LEDGER_LOCKED"


def test_m1_ledger_held_by_other_process_refused_here(tmp_path, led, clock):
    led.close()
    p = _child("hold", _ledger_path(tmp_path))
    try:
        assert p.stdout.readline().strip() == "OPENED"
        with pytest.raises(LedgerError) as e:
            reopen(tmp_path, clock)
        assert e.value.code == "LEDGER_LOCKED"
    finally:
        p.communicate("\n", timeout=60)
    reopen(tmp_path, clock).close()  # released when the other process exits


def test_m1_external_writer_detected_before_append(tmp_path, led, clock):
    """A writer that ignores the lock appends a valid, chained AUTHORIZE: the next append fails closed."""
    authorize(led)
    p = tmp_path / "ledger.jsonl"
    last = p.read_text().splitlines()[-1]
    forged = {"kind": "AUTHORIZE", "client_order_id": "order-key-EXT1", "intent_hash": "sha256:" + "2" * 64,
              "order_id": "0x" + "78" * 32, "market_hash": MKT, "event_id": "L1", "stake": "1",
              "expiry": NOW + 3600, "approval_nonce": NONCE_X, "ts": NOW, "prev_sha256": od._line_hash(last)}
    with open(p, "a", encoding="ascii") as fh:
        fh.write(od._canon(forged) + "\n")
    with pytest.raises(LedgerError) as e:
        authorize(led, key="order-key-0002", oid="0x" + "79" * 32, nonce=NONCE_X)
    assert e.value.code == "LEDGER_HEAD_MISMATCH"
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert sum(1 for r in rows if r.get("approval_nonce") == NONCE_X) == 1
    # Poisoned: refuses every further write until restart + replay.
    with pytest.raises(LedgerError) as e:
        led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    assert e.value.code == "LEDGER_POISONED"


def test_m1_replaced_file_detected(tmp_path, led, clock):
    authorize(led)
    p = tmp_path / "ledger.jsonl"
    text = p.read_text()
    p.unlink()
    p.write_text(text)  # same bytes, different inode: our lock no longer covers the path
    with pytest.raises(LedgerError) as e:
        led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    assert e.value.code == "LEDGER_HEAD_MISMATCH"


def test_m1_closed_ledger_refuses_writes_and_releases_lock(tmp_path, led, clock):
    led.close()
    with pytest.raises(LedgerError) as e:
        authorize(led)
    assert e.value.code == "LEDGER_CLOSED"
    reopen(tmp_path, clock).close()


# ---------------------------------------------------------------- M2: 404 after expiry is not proof
def test_m2_not_found_after_expiry_keeps_reservation(led, clock):
    authorize(led, expiry=NOW + 100)
    submitted(led)
    clock.t = NOW + 100 + od.EXPIRY_GRACE_SECONDS + 1
    o = reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills())
    assert o.state == od.EXPIRED_UNCONFIRMED
    assert led.exposure().total == STAKE and led.exposure().reserved_unmatched == STAKE


def test_m2_late_fill_after_404_heuristic_is_recorded_and_counted(led, clock):
    authorize(led, expiry=NOW + 100)
    submitted(led)
    clock.t = NOW + 100 + od.EXPIRY_GRACE_SECONDS + 1
    reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills())
    # The exchange later shows the order expired with 2_000_000 matched.
    clock.t += 600
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", STAKE - 2_000_000, "EXPIRED"))
    assert o.state == od.EXPIRED and o.filled == 2_000_000
    v = led.exposure()
    assert (v.matched, v.reserved_unmatched, v.total) == (2_000_000, 0, 2_000_000)


def test_m2_late_fill_seen_only_in_fills_while_order_404(led, clock):
    authorize(led, expiry=NOW + 100)
    submitted(led)
    clock.t = NOW + 100 + od.EXPIRY_GRACE_SECONDS + 1
    reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills())
    clock.t += 60
    o = reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills(fill(2_000_000, status="MATCHED")))
    assert o.state == od.EXPIRED_UNCONFIRMED and o.filled == 2_000_000
    v = led.exposure()
    assert (v.matched, v.reserved_unmatched, v.total) == (2_000_000, 3_000_000, STAKE)
    # Fills that disagree (fewer, foreign, unknown status, incomplete) learn nothing.
    for bad in (fills(fill(1_000_000, status="MATCHED")), fills(fill(2_000_000, oid="0x" + "99" * 32)),
                fills(fill(2_000_000, status="WEIRD")), fills(fill(3_000_000, status="MATCHED"), complete=False)):
        assert reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, bad).filled == 2_000_000


def test_m2_release_only_after_confirmation_window_and_late_fill_still_counted(led, clock):
    authorize(led, expiry=NOW + 100)
    submitted(led)
    first = NOW + 100 + od.EXPIRY_GRACE_SECONDS + 1
    clock.t = first
    reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills())
    clock.t = first + od.EXPIRY_CONFIRM_SECONDS - 1
    assert reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills()).state == od.EXPIRED_UNCONFIRMED
    assert led.exposure().total == STAKE
    clock.t = first + od.EXPIRY_CONFIRM_SECONDS
    assert reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills(complete=False)).state \
        == od.EXPIRED_UNCONFIRMED
    o = reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills())
    assert o.state == od.EXPIRED and led.exposure().total == 0
    # Even after the release, a later fill is recorded (not dropped as a conflict) and counted again.
    o = reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills(fill(1_000_000, status="LOCKED")))
    assert o.state == od.EXPIRED and o.filled == 1_000_000 and led.exposure().matched == 1_000_000
    o = reconcile(led, "order-key-0001", clock, row("INACTIVE", STAKE - 1_500_000, "EXPIRED"))
    assert o.filled == 1_500_000 and led.exposure().total == 1_500_000


def test_m2_unconfirmed_state_survives_restart(tmp_path, led, clock):
    authorize(led, expiry=NOW + 100)
    submitted(led)
    clock.t = NOW + 100 + od.EXPIRY_GRACE_SECONDS + 1
    reconcile(led, "order-key-0001", clock, {"kind": "NOT_FOUND"}, fills())
    led.close()
    r = reopen(tmp_path, clock)
    o = r.get("order-key-0001")
    assert o.state == od.EXPIRED_UNCONFIRMED and o.unconfirmed_since == clock.t and r.exposure().total == STAKE


# ---------------------------------------------------------------- M3: a released signature keeps its stake
def test_m3_rejected_after_signed_keeps_reservation_until_expiry(led, clock):
    authorize(led, expiry=NOW + 600)
    led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    led.transition("order-key-0001", od.REJECTED, reason="MANUAL")
    v = led.exposure()
    assert v.total == STAKE and v.reserved_unmatched == STAKE and v.open_orders == 1
    assert v.by_market == {MKT: STAKE}
    clock.t = NOW + 600 + od.EXPIRY_GRACE_SECONDS
    assert led.exposure().total == STAKE
    clock.t += 1
    assert led.exposure().total == 0 and led.exposure().open_orders == 0


def test_m3_rejected_after_unknown_keeps_reservation(tmp_path, led, clock):
    authorize(led, expiry=NOW + 600)
    led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    led.transition("order-key-0001", od.UNKNOWN, reason="SEND_ATTEMPT")
    led.transition("order-key-0001", od.REJECTED, reason="EXCHANGE_REJECTED_400")
    assert led.exposure().total == STAKE
    led.close()
    assert reopen(tmp_path, clock).exposure().total == STAKE  # rebuilt from the ledger


def test_m3_never_signed_rejection_releases_at_once(led):
    authorize(led)
    led.transition("order-key-0001", od.REJECTED, reason="SIGNING_FAILED")
    assert led.exposure().total == 0


# ---------------------------------------------------------------- L3: a failed append poisons the instance
def test_l3_fsync_failure_poisons_until_restart(tmp_path, led, clock, monkeypatch):
    authorize(led)

    def broken(fd):
        raise OSError("disk")

    monkeypatch.setattr(od.os, "fsync", broken)
    with pytest.raises(LedgerError, match="LEDGER_WRITE_FAILED"):
        led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    monkeypatch.undo()
    with pytest.raises(LedgerError) as e:
        authorize(led, key="order-key-0002", oid="0x" + "78" * 32, nonce="nonce-0000000000000002")
    assert e.value.code == "LEDGER_POISONED"
    with pytest.raises(LedgerError):
        led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    led.close()
    r = reopen(tmp_path, clock)  # restart + replay: the fully written line is part of the history
    assert r.get("order-key-0001").state == od.SIGNED
    authorize(r, key="order-key-0002", oid="0x" + "78" * 32, nonce="nonce-0000000000000002")


def test_l3_torn_write_poisons_and_restart_fails_closed(tmp_path, led, clock, monkeypatch):
    authorize(led)
    real = od.os.write

    def torn(fd, data):
        real(fd, data[: len(data) // 2])
        raise OSError("disk full")

    monkeypatch.setattr(od.os, "write", torn)
    with pytest.raises(LedgerError, match="LEDGER_WRITE_FAILED"):
        led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    monkeypatch.undo()
    with pytest.raises(LedgerError) as e:
        authorize(led, key="order-key-0002", oid="0x" + "78" * 32, nonce="nonce-0000000000000002")
    assert e.value.code == "LEDGER_POISONED"
    led.close()
    with pytest.raises(LedgerError) as e:
        reopen(tmp_path, clock)
    assert e.value.code == "LEDGER_CORRUPT"
