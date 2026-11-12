"""Owner gate P2-A (issue #187): the kill switch survives a restart even when the ledger could not record it.

Offline, SIMULATED exchange, throwaway keys. A marker OUTSIDE the ledger is written before every ENGAGE line
and checked on every open/create; an engagement the ledger does not hold keeps the signer blocked until it is
recorded and then released by the owner-signed release. If neither marker nor ledger can be written, the
ledger file is quarantined so a restart finds no ledger. The signer never resumes by itself.
"""
import json
import os
import pathlib

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import killswitch as ks  # noqa: E402
from signer import orders as od  # noqa: E402
from test_sxbet_killswitch import ks_audit, ledger_rows, make_ks, refused, release  # noqa: E402
from sxbet_e2e_kit import Stack  # noqa: E402


@pytest.fixture
def s(tmp_path):
    return Stack(tmp_path)


def marker(s):
    return pathlib.Path(s.ledger.kill_marker_path)


def _ledger_write_fails(monkeypatch, s):
    def boom(**kw):
        raise od.LedgerError("LEDGER_ANCHOR_WRITE_FAILED")
    monkeypatch.setattr(s.ledger, "engage_kill_switch", boom)


def test_engage_writes_the_marker_before_the_engage_line_and_restart_is_consistent(s):
    k = make_ks(s)
    rep = k.engage("OWNER_MANUAL_STOP")
    assert rep["persisted"] and rep["marker_written"] and rep["survives_restart"]
    doc = json.loads(marker(s).read_text())
    assert doc["schema"] == od.KILL_MARKER_SCHEMA and doc["ledger_id"] == s.ledger.ledger_id
    engage_seq = s.ledger.kill_switch_state()["engage_seq"]
    assert doc["ledger_seq"] < engage_seq
    s.restart()
    assert s.ledger.kill_marker_status == "CONSISTENT" and s.ledger.kill_switch_engaged


def test_unrecorded_engagement_blocks_after_restart_without_any_killswitch_object(s, monkeypatch):
    """RED on main: the volatile engagement was lost on restart and the signer signed again."""
    k = make_ks(s)
    _ledger_write_fails(monkeypatch, s)
    rep = k.engage("OWNER_MANUAL_STOP")
    assert rep["persisted"] is False and rep["marker_written"] is True and rep["survives_restart"] is True
    monkeypatch.undo()
    s.restart()
    assert s.ledger.kill_marker_status == "UNRECORDED"
    assert s.ledger.kill_switch_engaged and not s.ledger.kill_switch_state()["engaged"]  # memory, not ledger yet
    assert refused(lambda: s.sign(s.intent("order-key-P2A01"))) == "KILL_SWITCH_ENGAGED"


def test_killswitch_records_the_lost_engagement_then_only_the_owner_release_resumes(s, monkeypatch):
    k = make_ks(s)
    _ledger_write_fails(monkeypatch, s)
    k.engage("OWNER_MANUAL_STOP")
    monkeypatch.undo()
    s.restart()
    k2 = make_ks(s)
    assert k2.recovery["persisted"] is True and k2.recovery["trigger"] == od.KILL_MARKER_UNRECORDED
    assert s.ledger.kill_switch_state()["engaged"]
    assert ("KILL_SWITCH", "ENGAGE") in [(r["kind"], r.get("action")) for r in ledger_rows(s)]
    assert "KILL_SWITCH_MARKER_UNRECORDED" in [r["decision"] for r in ks_audit(s)]
    # Restarting again never resumes by itself.
    s.restart()
    assert s.ledger.kill_switch_engaged
    assert refused(lambda: s.sign(s.intent("order-key-P2A02"))) == "KILL_SWITCH_ENGAGED"
    k3 = make_ks(s)
    out = k3.release(release(s, k3))
    assert out["engaged"] is False and out["marker_removed"] is True and not marker(s).exists()
    s.restart()
    assert s.ledger.kill_marker_status is None and not s.ledger.kill_switch_engaged
    s.sign(s.intent("order-key-P2A03"))  # resumes only after the owner-signed release


def test_marker_never_removed_without_a_release_of_a_later_engagement(s, monkeypatch):
    k = make_ks(s)
    k.engage("OWNER_MANUAL_STOP")
    with pytest.raises(od.OrderStateError):
        s.ledger.remove_kill_marker_after_release()
    assert marker(s).exists()


def test_stale_marker_after_release_is_removed_on_next_open(s, monkeypatch):
    k = make_ks(s)
    k.engage("OWNER_MANUAL_STOP")
    monkeypatch.setattr(s.ledger, "remove_kill_marker_after_release", lambda: False)
    out = k.release(release(s, k))
    assert out["marker_removed"] is False and marker(s).exists()
    monkeypatch.undo()
    s.restart()
    assert s.ledger.kill_marker_status == "STALE_REMOVED" and not marker(s).exists()
    assert not s.ledger.kill_switch_engaged


@pytest.mark.parametrize("content", [b"", b"{not json", json.dumps({"schema": "other"}).encode(),
                                     json.dumps({"schema": od.KILL_MARKER_SCHEMA, "ledger_id": "x:0x" + "00" * 20,
                                                 "ledger_seq": 1}).encode(),
                                     b"\xff\xfe"])
def test_invalid_or_foreign_marker_fails_closed(s, content):
    marker(s).write_bytes(content)
    s.restart()
    assert s.ledger.kill_marker_status == "INVALID" and s.ledger.kill_switch_engaged
    assert refused(lambda: s.sign(s.intent("order-key-P2A04"))) == "KILL_SWITCH_ENGAGED"
    k = make_ks(s)
    assert k.recovery["persisted"] and s.ledger.kill_switch_state()["trigger"] == od.KILL_MARKER_INVALID


def test_a_recreated_ledger_does_not_escape_a_pending_marker(s, monkeypatch, tmp_path):
    k = make_ks(s)
    _ledger_write_fails(monkeypatch, s)
    k.engage("OWNER_MANUAL_STOP")
    monkeypatch.undo()
    path, mpath = s.ledger._path, s.ledger.kill_marker_path
    s.ledger.close()
    os.rename(path, str(tmp_path / "old-ledger.jsonl"))
    fresh = od.OrderLedger.create(path, signer_id=s.ledger.signer_id, signer_address=s.ledger.signer_address,
                                  clock=s.clock)
    try:
        assert fresh.kill_marker_path == mpath and fresh.kill_marker_status == "UNRECORDED"
        assert fresh.kill_switch_engaged
    finally:
        fresh.close()


def test_marker_and_ledger_both_unwritable_quarantine_the_ledger(s, monkeypatch):
    k = make_ks(s)
    _ledger_write_fails(monkeypatch, s)
    monkeypatch.setattr(s.ledger, "write_kill_marker", lambda **kw: False)
    path = s.ledger._path
    rep = k.engage("OWNER_MANUAL_STOP")
    assert rep["persisted"] is False and rep["marker_written"] is False
    assert rep["ledger_quarantined"] is True and rep["survives_restart"] is True
    assert not os.path.exists(path) and any(p.name.startswith(pathlib.Path(path).name + od.KILL_QUARANTINE_SUFFIX)
                                            for p in pathlib.Path(path).parent.iterdir())
    assert "KILL_SWITCH_LEDGER_QUARANTINED" in [r["decision"] for r in ks_audit(s)]
    assert s.ledger.poisoned and s.ledger.kill_switch_engaged
    monkeypatch.undo()
    s.ledger.close()
    with pytest.raises(od.LedgerError) as e:  # the production restart path opens, it never creates
        od.OrderLedger.open(path, signer_id=s.ledger.signer_id, signer_address=s.ledger.signer_address,
                            clock=s.clock)
    assert e.value.code == "LEDGER_MISSING"  # nothing resumes without the owner
    # A NEW ledger is a new identity: never live without a new owner-signed LIVE flag (review #196 P2-5).


def test_quarantine_never_modifies_the_ledger_content(s, monkeypatch):
    before = pathlib.Path(s.ledger._path).read_bytes()
    k = make_ks(s)
    _ledger_write_fails(monkeypatch, s)
    monkeypatch.setattr(s.ledger, "write_kill_marker", lambda **kw: False)
    k.engage("OWNER_MANUAL_STOP")
    (q,) = [p for p in pathlib.Path(s.ledger._path).parent.iterdir() if od.KILL_QUARANTINE_SUFFIX in p.name]
    assert q.read_bytes() == before


def test_marker_never_holds_secrets_or_command_text(s):
    k = make_ks(s)
    k.engage("OWNER_MANUAL_STOP")
    doc = json.loads(marker(s).read_text())
    assert set(doc) == {"schema", "ledger_id", "ledger_seq", "trigger", "source", "marked_at", "marker_id"}
    assert oct(os.stat(marker(s)).st_mode & 0o777) == "0o600"


def test_automatic_engage_on_poisoned_ledger_now_survives_restart(s):
    """Owner gate P2-A on the existing poisoned-ledger scenario (review #196 P2-a)."""
    k = make_ks(s)
    s.ledger._poisoned = True
    rep = k.evaluate_automatic(s.signer._risk.limits)
    assert rep["persisted"] is False and rep["marker_written"] is True
    s.ledger._poisoned = False
    s.restart()
    assert s.ledger.kill_switch_engaged and s.ledger.kill_marker_status == "UNRECORDED"
    assert make_ks(s).recovery["trigger"] == ks.LEDGER_ANCHOR_FORK or s.ledger.kill_switch_state()["engaged"]
