"""signer/monitor.py: periodic reconciliation, expiry handling, heartbeat POLICY, persisted trailing state.

The monitor proposes and alerts; it never submits, signs or cancels. Reads go through SimExchange (SIMULATED,
in memory) via OfficialRestAdapter; the tests assert that no write request ever leaves the monitor.
"""
import ast
import json
import pathlib

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import monitor as mn  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.monitor import HeartbeatPolicy, Monitor, MonitorError, MonitorState, evaluate_heartbeat  # noqa: E402
from sxbet_e2e_kit import H, NOW, USDC, Stack  # noqa: E402


def codes(rep):
    return sorted(a.code for a in rep["alerts"])


@pytest.fixture
def s(tmp_path):
    return Stack(tmp_path)


def make(s, tmp_path, name="monitor.json", **kw):
    st = MonitorState.create(str(tmp_path / name), ledger_id=s.ledger.ledger_id)
    return Monitor(s.ledger, s.adapter, st, clock=s.clock, lock=s.gw._lock, **kw)


# ---------------------------------------------------------------- reconciliation
def test_monitor_reconciles_open_and_partial_orders_read_only(s, tmp_path):
    i, sg, _ = s.place("order-key-W001")
    m = make(s, tmp_path)
    n = len(s.sim.writes())
    rep = m.tick()
    assert rep["reconciled"][0]["to"] == od.OPEN and s.state("order-key-W001") == od.OPEN
    s.sim.fill_maker_order(sg["order_id"], 2 * USDC)
    rep = m.tick()
    assert s.state("order-key-W001") == od.PARTIALLY_FILLED and "PARTIAL_FILL_RESIDUAL_RESERVED" in codes(rep)
    s.sim.fill_maker_order(sg["order_id"], 3 * USDC)
    m.tick()
    assert s.state("order-key-W001") == od.FILLED
    s.sim.advance(3600)
    s.sim.settle(H, 1)
    s.sim.advance(400)                                    # settlement polling interval
    m.tick()
    o = s.ledger.get("order-key-W001")
    assert o.state == od.SETTLED and o.realized_pnl == 7_500_000
    assert len(s.sim.writes()) == n and rep["orders_submitted"] == 0 and rep["network_writes"] == 0


def test_monitor_expiry_handling_and_unknown_alerts(s, tmp_path):
    i, sg, _ = s.place("order-key-W002", expiry=NOW + 600)
    i2 = s.intent("order-key-W003")
    sg2 = s.sign(i2)
    s.sim.inject("POST", "/orders-v3", "timeout_before")
    s.gw.submit(sg2, i2)
    m = make(s, tmp_path)
    rep = m.tick()
    assert "UNKNOWN_RECONCILIATION_PENDING" in codes(rep)
    s.sim.advance(od.ABSENT_SETTLE_SECONDS + 1)
    rep = m.tick()
    assert "RECONCILED_ABSENT_RESEND_IS_A_GATEWAY_DECISION" in codes(rep)
    assert s.state("order-key-W003") == od.SIGNED and s.sim.accepted_count("order-key-W003") == 0  # never resent
    s.sim.advance(600)
    m.tick()
    assert s.state("order-key-W002") == od.EXPIRED and s.ledger.get("order-key-W002").reserved_at(int(s.clock())) == 0
    s.sim.advance(3600)                                   # W003 (signed, never on the exchange) is past expiry
    rep = m.tick()
    assert s.state("order-key-W003") == od.EXPIRED_UNCONFIRMED and "EXPIRY_UNCONFIRMED_STAKE_HELD" in codes(rep)
    assert s.exposure().total == 5 * USDC
    s.sim.advance(od.EXPIRY_CONFIRM_SECONDS)
    m.tick()
    assert s.state("order-key-W003") == od.EXPIRED and s.exposure().total == 0


def test_monitor_alerts_authorized_never_signed(s, tmp_path, monkeypatch):
    from signer.core import Signer
    from sxbet_sim_exchange import SimCrash

    def die(self, msg):
        raise SimCrash()

    monkeypatch.setattr(Signer, "_produce_signature", die)
    with pytest.raises(SimCrash):
        s.sign(s.intent("order-key-W004"))
    monkeypatch.undo()
    assert "AUTHORIZED_NEVER_SIGNED_REVIEW_ABANDON_UNSIGNED" in codes(make(s, tmp_path).tick())
    assert s.state("order-key-W004") == od.AUTHORIZED                       # alert only, never acted upon


# ---------------------------------------------------------------- heartbeat policy (no sender)
def test_heartbeat_policy_evaluation():
    p = HeartbeatPolicy(timeout_seconds=60, renew_margin_seconds=15)
    assert evaluate_heartbeat(None, last_ack_at=None, open_orders=0, now=NOW) == []
    assert [a.code for a in evaluate_heartbeat(None, last_ack_at=None, open_orders=2, now=NOW)] == [
        "HEARTBEAT_NOT_CONFIGURED"]
    assert [a.code for a in evaluate_heartbeat(p, last_ack_at=None, open_orders=1, now=NOW)] == [
        "HEARTBEAT_NEVER_ACKNOWLEDGED"]
    assert evaluate_heartbeat(p, last_ack_at=NOW - 10, open_orders=1, now=NOW) == []
    assert [a.code for a in evaluate_heartbeat(p, last_ack_at=NOW - 45, open_orders=1, now=NOW)] == ["HEARTBEAT_RENEW_DUE"]
    (a,) = evaluate_heartbeat(p, last_ack_at=NOW - 61, open_orders=1, now=NOW)
    assert a.code == "HEARTBEAT_LAPSED_ORDERS_MAY_BE_CANCELLED" and a.severity == mn.CRITICAL
    for bad in ((0, 1), (3601, 1), (60, 0), (60, 60), ("60", 1)):
        with pytest.raises(MonitorError):
            HeartbeatPolicy(*bad)


def test_monitor_heartbeat_alert_then_lapse_reconciled(s, tmp_path):
    from signer.rest_adapter import RestRequest
    s.place("order-key-W005")
    m = make(s, tmp_path, heartbeat_policy=HeartbeatPolicy(timeout_seconds=30, renew_margin_seconds=10))
    s.sim.send(RestRequest("POST", "/heartbeat/v3", (), {"timeoutSeconds": 30}))   # the TEST is the heartbeat process
    m._state.record_heartbeat_ack(int(s.clock()))
    s.sim.advance(22)
    assert "HEARTBEAT_RENEW_DUE" in codes(m.tick())
    s.sim.advance(10)
    rep = m.tick()
    assert "HEARTBEAT_LAPSED_ORDERS_MAY_BE_CANCELLED" in codes(rep)
    assert s.state("order-key-W005") == od.CANCELLED and s.exposure().total == 0   # HEARTBEAT_TIMEOUT, by reads


# ---------------------------------------------------------------- persisted state + restart
def test_monitor_state_is_explicit_integrity_checked_and_bound(s, tmp_path):
    p = tmp_path / "m.json"
    with pytest.raises(MonitorError) as e:
        MonitorState.open(str(p), ledger_id=s.ledger.ledger_id)
    assert e.value.code == "MONITOR_STATE_MISSING"
    st = MonitorState.create(str(p), ledger_id=s.ledger.ledger_id)
    with pytest.raises(MonitorError):
        MonitorState.create(str(p), ledger_id=s.ledger.ledger_id)
    st.raise_peak(H, 10, NOW)
    st.raise_peak(H, 5, NOW)                               # monotonic
    st.save()
    assert MonitorState.open(str(p), ledger_id=s.ledger.ledger_id).peak(H) == 10
    with pytest.raises(MonitorError) as e:
        MonitorState.open(str(p), ledger_id="other:0x" + "00" * 20)
    assert e.value.code == "MONITOR_STATE_FOREIGN"
    doc = json.loads(p.read_text())
    doc["body"]["trailing"][H]["peak"] = 99                 # tampered: the hash no longer matches
    p.write_text(json.dumps(doc))
    with pytest.raises(MonitorError) as e:
        MonitorState.open(str(p), ledger_id=s.ledger.ledger_id)
    assert e.value.code == "MONITOR_STATE_CORRUPT"
    p.write_text("{not json")
    with pytest.raises(MonitorError) as e:
        MonitorState.open(str(p), ledger_id=s.ledger.ledger_id)
    assert e.value.code == "MONITOR_STATE_CORRUPT"
    with pytest.raises(MonitorError) as e:
        Monitor(s.ledger, s.adapter, MonitorState.create(str(tmp_path / "x.json"), ledger_id="other:0x" + "00" * 20))
    assert e.value.code == "MONITOR_STATE_FOREIGN"


def test_monitor_restart_recovers_from_ledger_and_state(s, tmp_path):
    i, sg, _ = s.place("order-key-W006")
    m = make(s, tmp_path)
    m.tick()
    m._state.raise_peak(H, 123, NOW)
    m.tick()
    ticks = m._state.ticks
    s.sim.fill_maker_order(sg["order_id"], 2 * USDC)        # happens while the monitor is down
    s.restart()
    m2 = Monitor(s.ledger, s.adapter, MonitorState.open(str(tmp_path / "monitor.json"), ledger_id=s.ledger.ledger_id),
                 clock=s.clock)
    assert m2._state.ticks == ticks and s.state("order-key-W006") == od.OPEN    # ledger replay: as before the fill
    m2.tick()
    assert s.state("order-key-W006") == od.PARTIALLY_FILLED and m2._state.peak(H) == 123


def test_monitor_proposal_claiming_executable_is_refused(s, tmp_path):
    i, sg, _ = s.place("order-key-W007")
    s.sim.fill_maker_order(sg["order_id"], 5 * USDC)
    s.gw.reconcile("order-key-W007")

    def evil(cfg, **kw):
        return {"results": [], "proposal": {"proposal_hash": "sha256:x", "executable": True, "signed": False,
                                            "submitted": False}}

    m = make(s, tmp_path, rules={}, evaluate_rules=evil, market_snapshot=lambda mh: {"market_position": {}})
    with pytest.raises(MonitorError) as e:
        m.tick()
    assert e.value.code == "MONITOR_PROPOSAL_CLAIMS_EXECUTABLE"
    with pytest.raises(MonitorError):
        Monitor(s.ledger, s.adapter, MonitorState.create(str(tmp_path / "y.json"), ledger_id=s.ledger.ledger_id),
                rules={})


def test_monitor_reaches_only_the_three_reads():
    ro = mn._ReadOnly(type("R", (), {"get_order": 1, "get_order_by_client_id": 2, "get_fills": 3, "send": 4,
                                     "submit_orders": 5})())
    assert not hasattr(ro, "send") and not hasattr(ro, "submit_orders") and not hasattr(ro, "__dict__")
    src = pathlib.Path(mn.__file__).read_text(encoding="utf-8")
    called = {n.func.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}
    assert not called & {"send", "submit", "submit_orders", "submit_batch", "build_create", "sign_order",
                         "cancel_orders", "cancel_market", "cancel_event", "cancel_all", "transition", "authorize",
                         "record_cancel_request", "abandon_unsigned"}
