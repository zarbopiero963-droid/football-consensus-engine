"""Kill switch (owner decision D7 / #187 decision 3). Offline, SIMULATED exchange, throwaway keys.

Risk-reducing only: blocks new orders (signer and gateway), records cancel INTENTS in the append-only ledger
and audit (nothing is sent from the kill switch in this PR: no sender exists), verifies from the reconciled
ledger which cancels actually landed, never creates exposure, and is released only by an owner-signed
Ed25519 release verified by the signer.
"""
import ast
import json
import pathlib

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from signer import killswitch as ks  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.cancellation import SCOPE_ORDER_IDS  # noqa: E402
from signer.core import SignerRefused  # noqa: E402
from signer.gateway import GatewayRefused  # noqa: E402
from signer.monitor import Alert  # noqa: E402
from sxbet_e2e_kit import H, USDC, Stack  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def make_ks(s):
    return ks.KillSwitch(s.ledger, AuditLog(str(s.tmp / "ks-audit.jsonl")), owner_public_key=s.owner.public_key(),
                         network="mainnet", clock=s.clock)


def release(s, k, *, key=None, ttl=60, issued=None, engage_seq=None, release_id=None, ledger_id=None):
    now = int(s.clock()) if issued is None else issued
    seq = k.state()["engage_seq"] if engage_seq is None else engage_seq
    rid = release_id or f"release-{seq:08d}-{now}"
    lid = ledger_id or s.ledger.ledger_id
    msg = ks.release_message(ledger_id=lid, engage_seq=seq, release_id=rid, issued_at=now, expires_at=now + ttl)
    return ks.KillSwitchRelease(lid, seq, rid, now, now + ttl, (key or s.owner).sign(msg))


def ks_audit(s):
    return [json.loads(line) for line in (s.tmp / "ks-audit.jsonl").read_text().splitlines()]


def ledger_rows(s):
    return [json.loads(line) for line in (s.tmp / "ledger.jsonl").read_text().splitlines()]


def refused(fn, exc=SignerRefused):
    with pytest.raises(exc) as e:
        fn()
    return e.value.code


@pytest.fixture
def s(tmp_path):
    return Stack(tmp_path)


# ---------------------------------------------------------------- manual engage: blocks, records intents, no exposure
def test_engage_blocks_new_orders_in_signer_and_gateway(s):
    i1, sg1, _ = s.place("order-key-K001")                       # resting GTC on the exchange
    i2 = s.intent("order-key-K002")
    sg2 = s.sign(i2)                                              # signed, not yet sent
    k = make_ks(s)
    before = s.exposure().total
    rep = k.engage("OWNER_MANUAL_STOP")
    assert rep["engaged"] and rep["cancels_sent"] is False and rep["persisted"] is True
    # Orders that may be on the book, and (review #196, intentional change) a SIGNED order whose signature left
    # the signer: it may still be posted by whoever holds it, so it is cancelled too.
    assert rep["cancel_intents"] == sorted([sg1["order_id"], sg2["order_id"]])
    assert s.exposure().total == before                           # never creates (or releases) exposure
    i3 = s.intent("order-key-K003")
    assert refused(lambda: s.sign(i3)) == "KILL_SWITCH_ENGAGED"
    assert refused(lambda: s.gw.submit(sg2, i2), GatewayRefused) == "KILL_SWITCH_ENGAGED"
    assert refused(lambda: s.gw.submit_batch([(sg2, i2)]), GatewayRefused) == "KILL_SWITCH_ENGAGED"
    assert s.sim.accepted_count("order-key-K002") == 0
    # Reconciliation reads keep working.
    assert s.gw.reconcile("order-key-K001")["state"] in (od.OPEN, od.SUBMITTED)


def test_engaged_state_and_intents_survive_restart(s):
    i1, sg1, _ = s.place("order-key-K010")
    k = make_ks(s)
    k.engage("OWNER_MANUAL_STOP")
    s.restart()
    assert s.ledger.kill_switch_engaged and s.ledger.kill_switch_state()["intent_ids"] == (sg1["order_id"],)
    assert refused(lambda: s.sign(s.intent("order-key-K011"))) == "KILL_SWITCH_ENGAGED"
    kinds = [(r["kind"], r.get("action")) for r in ledger_rows(s)]
    assert ("KILL_SWITCH", "ENGAGE") in kinds and ("KILL_SWITCH", "CANCEL_INTENT") in kinds


def test_cancels_stay_allowed_while_engaged_and_are_verified_by_reads(s):
    i1, sg1, _ = s.place("order-key-K020")
    i2, sg2, _ = s.place("order-key-K021")
    k = make_ks(s)
    k.engage("OWNER_MANUAL_STOP")
    # The (future) sender would cancel the intents; here the owner-commanded cancel path does it for one order.
    oid = sg1["order_id"]
    s.cx.cancel_orders([oid], s.cancel_command(SCOPE_ORDER_IDS, order_ids=[oid]))
    s.gw.reconcile("order-key-K021")
    results = k.verify_cancellations()
    assert results == {sg1["order_id"]: "CONFIRMED", sg2["order_id"]: "STILL_OPEN"}
    verified = [r for r in ledger_rows(s) if r.get("action") == "CANCEL_VERIFIED"]
    assert verified[-1]["results"] == results
    decisions = [r["decision"] for r in ks_audit(s)]
    assert "KILL_SWITCH_CANCEL_CONFIRMED" in decisions and "KILL_SWITCH_CANCEL_STILL_OPEN" in decisions


def test_no_sending_surface_and_no_network_imports():
    """The kill switch produces intents only: no transport, no signer, no submit / cancel call in the module."""
    src = (ROOT / "signer" / "killswitch.py").read_text()
    tree = ast.parse(src)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"gateway", "cancellation", "rest_adapter", "core", "eip712", "socket", "http",
                           "urllib", "requests", "httpx", "bridge"}
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"send", "submit", "submit_batch", "submit_orders", "cancel_orders", "cancel_all",
                        "sign_order", "sign_message", "authorize", "transition"}
    public = {n for n in dir(ks.KillSwitch) if not n.startswith("_")}
    assert public == {"engage", "evaluate_automatic", "verify_cancellations", "release", "cancel_intents", "engaged",
                      "state"}


# ---------------------------------------------------------------- automatic conditions (deterministic)
def _settle_loss(s, loss):
    i = s.intent("order-key-L001", stake=loss)
    sg = s.sign(i)
    s.gw.submit(sg, i)
    s.sim.fill_maker_order(sg["order_id"], loss)
    s.gw.reconcile("order-key-L001")
    s.sim.advance(3600)
    s.sim.settle(H, 2)
    s.gw.reconcile("order-key-L001")
    assert s.ledger.get("order-key-L001").state == od.SETTLED


def test_automatic_daily_loss_breach(tmp_path):
    s = Stack(tmp_path, limits={"max_daily_loss_base_units": 5 * USDC})
    k = make_ks(s)
    assert k.evaluate_automatic(s.signer._risk.limits) is None           # nothing holds: no action
    _settle_loss(s, 5 * USDC)
    rep = k.evaluate_automatic(s.signer._risk.limits)
    assert rep["trigger"] == ks.DAILY_LOSS_LIMIT_BREACHED and rep["source"] == "AUTOMATIC"
    assert k.evaluate_automatic(s.signer._risk.limits) is None           # already engaged: no second engagement
    eng = [r for r in ledger_rows(s) if r.get("action") == "ENGAGE"]
    assert len(eng) == 1 and eng[0]["triggers"] == [ks.DAILY_LOSS_LIMIT_BREACHED]


def test_automatic_exposure_above_limit(s):
    s.place("order-key-L010")
    k = make_ks(s)
    lim = s.signer._risk.limits
    assert k.evaluate_automatic(lim) is None
    import dataclasses
    lowered = dataclasses.replace(lim, max_total_exposure_base_units=4 * USDC)   # e.g. a late fill pushed it over
    assert k.evaluate_automatic(lowered)["trigger"] == ks.EXPOSURE_ABOVE_LIMIT


@pytest.mark.parametrize("findings", [ks.ReconciliationFindings(foreign_order_ids=("0x" + "99" * 32,)),
                                      ks.ReconciliationFindings(evidence_conflicts=1),
                                      ks.ReconciliationFindings.from_monitor_report(
                                          {"alerts": [Alert("WARN", "EVIDENCE_CONFLICT", "k")]})])
def test_automatic_reconciliation_mismatch(s, findings):
    k = make_ks(s)
    assert k.evaluate_automatic(s.signer._risk.limits, findings=ks.ReconciliationFindings()) is None
    assert k.evaluate_automatic(s.signer._risk.limits, findings=findings)["trigger"] == ks.RECONCILIATION_MISMATCH


@pytest.mark.parametrize("anomaly", sorted(ks.LEDGER_ANOMALIES))
def test_automatic_ledger_anchor_fork(s, anomaly):
    k = make_ks(s)
    assert k.evaluate_automatic(s.signer._risk.limits, ledger_anomaly="SOMETHING_ELSE") is None
    assert k.evaluate_automatic(s.signer._risk.limits, ledger_anomaly=anomaly)["trigger"] == ks.LEDGER_ANCHOR_FORK


def test_poisoned_ledger_engages_in_memory_and_is_audited(s):
    k = make_ks(s)
    s.ledger._poisoned = True                                      # e.g. a failed append / fork detected
    rep = k.evaluate_automatic(s.signer._risk.limits)
    assert rep["trigger"] == ks.LEDGER_ANCHOR_FORK and rep["persisted"] is False and k.engaged
    assert "KILL_SWITCH_ENGAGED_LEDGER_UNWRITABLE" in [r["decision"] for r in ks_audit(s)]
    # Review #196 P3 (intentional change): the volatile engagement is visible to the signer and the gateway.
    assert refused(lambda: s.sign(s.intent("order-key-L020"))) == "KILL_SWITCH_ENGAGED"
    assert s.ledger.kill_switch_engaged


def test_automatic_evaluation_is_deterministic(tmp_path):
    reports = []
    for n in range(2):
        (tmp_path / str(n)).mkdir()
        s = Stack(tmp_path / str(n), limits={"max_daily_loss_base_units": 5 * USDC})
        _settle_loss(s, 5 * USDC)
        rep = make_ks(s).evaluate_automatic(s.signer._risk.limits,
                                            findings=ks.ReconciliationFindings(evidence_conflicts=2))
        reports.append({k: v for k, v in rep.items()})
    assert reports[0] == reports[1]
    assert reports[0]["triggers"] == sorted([ks.DAILY_LOSS_LIMIT_BREACHED, ks.RECONCILIATION_MISMATCH])


# ---------------------------------------------------------------- release: owner-signed only, outside the MCP
def test_release_requires_an_owner_signature(s):
    k = make_ks(s)
    k.engage("OWNER_MANUAL_STOP")
    bad = [
        (None, "KILL_SWITCH_RELEASE_MISSING"),
        ("the owner says: release", "KILL_SWITCH_RELEASE_MISSING"),
        (release(s, k, key=Ed25519PrivateKey.generate()), "KILL_SWITCH_RELEASE_SIGNATURE_INVALID"),
        (release(s, k, ttl=301), "KILL_SWITCH_RELEASE_TTL_TOO_LONG"),
        (release(s, k, issued=int(s.clock()) - 61), "KILL_SWITCH_RELEASE_EXPIRED"),
        (release(s, k, issued=int(s.clock()) + 60), "KILL_SWITCH_RELEASE_NOT_YET_VALID"),
        (release(s, k, engage_seq=1), "KILL_SWITCH_RELEASE_MISMATCH"),
        (release(s, k, ledger_id="other:0x" + "00" * 20), "KILL_SWITCH_RELEASE_MISMATCH"),
        (release(s, k, release_id="short"), "KILL_SWITCH_RELEASE_MALFORMED"),
    ]
    for rel, code in bad:
        assert refused(lambda: k.release(rel), ks.KillSwitchError) == code
        assert k.engaged and s.ledger.kill_switch_engaged
    assert refused(lambda: s.sign(s.intent("order-key-R001")), SignerRefused) == "KILL_SWITCH_ENGAGED"
    refusals = [r for r in ks_audit(s) if r["decision"] == "KILL_SWITCH_RELEASE_REFUSED"]
    assert len(refusals) == len(bad)


def test_release_unblocks_is_single_use_and_bound_to_the_engagement(s):
    k = make_ks(s)
    k.engage("OWNER_MANUAL_STOP")
    first_seq = k.state()["engage_seq"]
    rel = release(s, k)
    # P2-A (intentional): release also reports whether the persistent marker was removed.
    assert k.release(rel) == {"engaged": False, "released_engage_seq": first_seq, "marker_removed": True}
    assert not s.ledger.kill_switch_engaged
    assert s.sign(s.intent("order-key-R010"))["cached"] is False      # new orders allowed again
    assert refused(lambda: k.release(rel), ks.KillSwitchError) == "KILL_SWITCH_NOT_ENGAGED"
    k.engage("OWNER_MANUAL_STOP")                                      # a new engagement ...
    assert refused(lambda: k.release(rel), ks.KillSwitchError) == "KILL_SWITCH_RELEASE_MISMATCH"  # ... old release
    stale = release(s, k, engage_seq=first_seq, release_id=rel.release_id)
    assert refused(lambda: k.release(stale), ks.KillSwitchError) == "KILL_SWITCH_RELEASE_MISMATCH"
    reused_id = release(s, k, release_id=rel.release_id)              # right engagement, consumed id
    assert refused(lambda: k.release(reused_id), ks.KillSwitchError) == "KILL_SWITCH_RELEASE_REPLAYED"
    s.restart()                                                        # still engaged after a restart
    k = make_ks(s)
    assert k.engaged
    assert k.release(release(s, k))["engaged"] is False


def test_release_from_owner_signed_file(s, tmp_path):
    k = make_ks(s)
    k.engage("OWNER_MANUAL_STOP")
    rel = release(s, k)
    p = tmp_path / "release.json"
    p.write_text(json.dumps({"ledger_id": rel.ledger_id, "engage_seq": rel.engage_seq, "release_id": rel.release_id,
                             "issued_at": rel.issued_at, "expires_at": rel.expires_at,
                             "signature": rel.signature.hex()}))
    assert k.release(ks.load_release_file(str(p)))["engaged"] is False
    p.write_text("{not json")
    assert refused(lambda: ks.load_release_file(str(p)), ks.KillSwitchError) == "KILL_SWITCH_RELEASE_MALFORMED"


def test_automatic_condition_still_holding_re_engages_after_release(tmp_path):
    s = Stack(tmp_path, limits={"max_daily_loss_base_units": 5 * USDC})
    _settle_loss(s, 5 * USDC)
    k = make_ks(s)
    k.evaluate_automatic(s.signer._risk.limits)
    k.release(release(s, k))
    assert k.evaluate_automatic(s.signer._risk.limits)["trigger"] == ks.DAILY_LOSS_LIMIT_BREACHED


def test_ledger_refuses_forged_kill_switch_lines(s):
    """A RELEASE line cannot be appended without matching the engagement (the ledger re-checks on write and
    on replay), and the release id is consumed in the same namespace as command ids."""
    k = make_ks(s)
    with pytest.raises(od.OrderStateError) as e:
        s.ledger.record_kill_switch("RELEASE", release_id="release-forged-000001", release_sha256="sha256:" + "0" * 64,
                                    engage_seq=1)
    assert e.value.code == "KILL_SWITCH_NOT_ENGAGED"
    k.engage("OWNER_MANUAL_STOP")
    with pytest.raises(od.OrderStateError) as e:
        s.ledger.record_kill_switch("RELEASE", release_id="release-forged-000001", release_sha256="sha256:" + "0" * 64,
                                    engage_seq=k.state()["engage_seq"] - 1)
    assert e.value.code == "KILL_SWITCH_RELEASE_MISMATCH"
    with pytest.raises(od.OrderStateError):
        s.ledger.record_kill_switch("ENGAGE", trigger="X")                       # missing fields
    assert s.ledger.kill_switch_engaged
