"""Owner decision 16-11-2026 (issue #187): a placement refused because of the kill switch stays refused after a
restart and a later release, independently of the MCP clock. Offline: simulated exchange, in-memory queue with the
database sequence semantics (queue_seq assigned at insert), throwaway keys. Nothing reaches SX Bet."""
import threading

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import killswitch as ks  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.service import SignerService  # noqa: E402
from sxbet_e2e_kit import SIGNER_ID, Stack  # noqa: E402
from sxbet_queue_kit import MemoryQueue, place_request  # noqa: E402

SKEWS = (10, 60, 300)


def _stack(tmp_path):
    s = Stack(tmp_path)
    q = MemoryQueue()
    return s, q, _kill(s, q, tmp_path)


def _kill(s, q, tmp_path):
    return ks.KillSwitch(s.ledger, AuditLog(str(tmp_path / "ks.jsonl")), owner_public_key=s.owner.public_key(),
                         network="mainnet", clock=s.clock, queue_watermark=q.release_watermark)


def _svc(s, q):
    return SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)


def _release(s, kill):
    now, seq = int(s.clock()), kill.state()["engage_seq"]
    rid = f"release-{seq:08d}-{now}"
    msg = ks.release_message(ledger_id=s.ledger.ledger_id, engage_seq=seq, release_id=rid, issued_at=now,
                             expires_at=now + 60)
    return kill.release(ks.KillSwitchRelease(s.ledger.ledger_id, seq, rid, now, now + 60, s.owner.sign(msg)))


def _place(s, q, key, *, mcp_skew=0, stake=None):
    """A PLACE request as the MCP queues it; `mcp_skew` shifts the MCP clock (created_at) vs the signer clock."""
    i = s.intent(key) if stake is None else s.intent(key, stake=stake)
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    p = place_request(i, s.command(i, ctx), created_at=int(s.clock()) + mcp_skew)
    q.insert(p)
    return p, i, ctx


def _posts(s):
    return [x for x in s.sim.writes() if x.method == "POST"]


def _code(res):
    return res["detail"].get("code")


# ---------------------------------------------------------------- MCP clock ahead / behind
@pytest.mark.parametrize("skew", [*SKEWS, *(-x for x in SKEWS)])
def test_mcp_clock_skew_never_revives_an_old_placement(tmp_path, skew):
    """Queued while ENGAGED with the MCP clock off by +/-10/60/300 s, refusal never ran, owner releases: refused by
    the DATABASE ordering (or earlier by freshness), never sent."""
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, _, _ = _place(s, q, f"order-key-K{skew + 1000:04d}", mcp_skew=skew)
    _release(s, kill)
    (res,) = _svc(s, q).process_once(10)
    # Review dccb44a P2-2/P3-2: decided by the DATABASE ordering (checked before freshness), and voided.
    assert res["event"] == "REFUSED" and _code(res) == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE"
    assert s.ledger.command_text_voided(p["command"]["command_text_sha256"]) and _posts(s) == []


@pytest.mark.parametrize("skew", [*SKEWS, *(-x for x in SKEWS)])
def test_mcp_clock_skew_never_bypasses_an_engaged_kill_switch(tmp_path, skew):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, _, _ = _place(s, q, f"order-key-B{skew + 1000:04d}", mcp_skew=skew)
    svc = _svc(s, q)
    (r,) = svc.process_once(10)                                       # even the all-kinds path refuses (P1-1)
    assert _code(r) == "KILL_SWITCH_ENGAGED" and _posts(s) == []
    assert s.ledger.command_text_voided(p["command"]["command_text_sha256"])


# ---------------------------------------------------------------- concurrency: release vs processing
def test_release_during_processing_is_deterministic_refused_by_the_engaged_switch(tmp_path, monkeypatch):
    """A PLACE is inside _handle while the owner releases: the release waits for the service lock; the PLACE sees
    the switch ENGAGED and is refused. Never signed, never sent."""
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, _, _ = _place(s, q, "order-key-C001")
    svc = _svc(s, q)
    inside, go = threading.Event(), threading.Event()
    from signer import service as service_mod
    real = service_mod.validate_request

    def slow_validate(payload):                                   # first step of _handle, under the service lock
        inside.set()
        go.wait(5)
        return real(payload)
    monkeypatch.setattr(service_mod, "validate_request", slow_validate)
    out = {}
    t1 = threading.Thread(target=lambda: out.setdefault("res", svc.process_once(10)))
    t1.start()
    assert inside.wait(5)
    t2 = threading.Thread(target=lambda: out.setdefault("rel", _release(s, kill)))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive() and s.ledger.kill_switch_engaged             # release blocked by the service lock
    go.set()
    t1.join(5)
    t2.join(5)
    (res,) = out["res"]
    assert res["event"] == "REFUSED" and _code(res) == "KILL_SWITCH_ENGAGED"
    assert out["rel"]["engaged"] is False and _posts(s) == []
    assert s.ledger.command_text_voided(p["command"]["command_text_sha256"])     # voided before the release


def test_release_first_then_processing_is_refused_by_the_watermark(tmp_path):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    _place(s, q, "order-key-C002")
    _release(s, kill)
    (res,) = _svc(s, q).process_once(10)
    assert _code(res) == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE" and _posts(s) == []


# ---------------------------------------------------------------- crash / restart
def test_crash_before_release_then_restart_and_release(tmp_path):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    _place(s, q, "order-key-R001")
    s.restart()                                                       # crash before the release
    kill = _kill(s, q, tmp_path)
    assert kill.engaged
    _release(s, kill)
    (res,) = _svc(s, q).process_once(10)
    assert _code(res) == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE" and _posts(s) == []


def test_crash_after_release_then_restart(tmp_path):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    _place(s, q, "order-key-R002")
    _release(s, kill)
    wm = s.ledger.queue_watermark()
    s.restart()                                                       # watermark replayed from the ledger
    assert s.ledger.queue_watermark() == wm
    (res,) = _svc(s, q).process_once(10)
    assert _code(res) == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE" and _posts(s) == []


def test_refusal_is_terminal_and_the_command_is_voided_after_restart(tmp_path):
    """The refused request is never reprocessed; its owner command text cannot authorise a NEW request either."""
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, i, ctx = _place(s, q, "order-key-R003")
    svc = _svc(s, q)
    assert [r["event"] for r in svc.refuse_pending_places(10)] == ["REFUSED"]
    _release(s, kill)
    s.restart()
    s.signer.config.market_data.world = ctx
    assert _svc(s, q).process_once(10) == [] and _svc(s, q).recover() == []
    i2 = s.intent("order-key-R004")                                   # SAME owner text, new key, after the release
    p2 = place_request(i2, s.command(i2, ctx, text=p["command"]["command_text"]), created_at=int(s.clock()))
    q.insert(p2)
    (res,) = _svc(s, q).process_once(10)
    assert res["event"] == "REFUSED" and _code(res) == "COMMAND_VOIDED_BY_KILL_SWITCH" and _posts(s) == []


def test_unrecorded_engagement_is_restored_after_restart(tmp_path, monkeypatch):
    """Review 48a22c6 P2-2: the ledger could not record the engagement (volatile); after a restart the audit
    restores it durably, so nothing queued before is processed without an owner release."""
    from signer import orders as od
    s, q, kill = _stack(tmp_path)

    def unwritable(**kw):
        raise od.LedgerError("LEDGER_WRITE_FAILED")
    monkeypatch.setattr(s.ledger, "engage_kill_switch", unwritable)
    assert kill.engage("OWNER_TEST", source="MANUAL")["persisted"] is False
    _place(s, q, "order-key-V001")
    monkeypatch.undo()
    s.restart()
    kill = _kill(s, q, tmp_path)                                      # startup: audit says engaged, unreleased
    assert s.ledger.kill_switch_engaged and kill.state()["trigger"] == "RESTORED_UNRECORDED_ENGAGEMENT"
    svc = _svc(s, q)
    svc.refuse_pending_places(10)
    _release(s, kill)
    assert svc.process_once(10) == [] and _posts(s) == []


def test_a_broken_audit_chain_stops_the_start(tmp_path):
    s, q, kill = _stack(tmp_path)
    kill._log("KILL_SWITCH_TEST", reason="X")
    with open(tmp_path / "ks.jsonl", "a", encoding="ascii") as fh:
        fh.write('{"prev_sha256":"sha256:broken"}\n')
    with pytest.raises(ValueError):
        _kill(s, q, tmp_path)                                         # a broken audit chain stops the start


# ---------------------------------------------------------------- after the release
def test_a_new_command_after_the_release_is_accepted_when_every_gate_holds(tmp_path):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    _release(s, kill)
    _place(s, q, "order-key-N001", stake=4_000_000)                   # new command text, queued after the release
    (res,) = _svc(s, q).process_once(10)
    assert res["event"] == "SUBMITTED" and len(_posts(s)) == 1


@pytest.mark.parametrize("skew", [10, 60, 300])
def test_a_new_command_with_the_mcp_clock_ahead_is_refused_by_freshness(tmp_path, skew):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    _release(s, kill)
    _place(s, q, f"order-key-N{skew:03d}", stake=4_000_000, mcp_skew=skew)
    (res,) = _svc(s, q).process_once(10)
    assert res["event"] == "REFUSED" and _code(res) == "REQUEST_EXPIRED" and _posts(s) == []


def test_several_engage_release_cycles_keep_every_older_placement_refused(tmp_path):
    s, q, kill = _stack(tmp_path)
    old = []
    for n in range(3):
        kill.engage("OWNER_TEST", source="MANUAL")
        old.append(_place(s, q, f"order-key-M00{n}")[0])
        _release(s, kill)
    res = _svc(s, q).process_once(10)
    assert {_code(r) for r in res} == {"PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE"} and len(res) == 3
    assert _posts(s) == []


def test_release_without_a_queue_watermark_source_is_refused(tmp_path):
    s, q, _ = _stack(tmp_path)
    k = ks.KillSwitch(s.ledger, AuditLog(str(tmp_path / "k2.jsonl")), owner_public_key=s.owner.public_key(),
                      network="mainnet", clock=s.clock)
    k.engage("OWNER_TEST", source="MANUAL")
    with pytest.raises(ks.KillSwitchError) as e:
        _release(s, k)
    assert e.value.code == "KILL_SWITCH_RELEASE_NEEDS_QUEUE_WATERMARK" and s.ledger.kill_switch_engaged


def test_unreachable_queue_keeps_the_switch_engaged(tmp_path):
    s, q, _ = _stack(tmp_path)

    def down():
        raise ConnectionError("db")
    k = ks.KillSwitch(s.ledger, AuditLog(str(tmp_path / "k3.jsonl")), owner_public_key=s.owner.public_key(),
                      network="mainnet", clock=s.clock, queue_watermark=down)
    k.engage("OWNER_TEST", source="MANUAL")
    with pytest.raises(ks.KillSwitchError) as e:
        _release(s, k)
    assert e.value.code == "KILL_SWITCH_RELEASE_QUEUE_WATERMARK_UNAVAILABLE" and s.ledger.kill_switch_engaged


# ---------------------------------------------------------------- review dccb44a: every refusal voids the operation
def _resend(s, q, p, ctx, key, text=None, same_id=False):
    i2 = s.intent(key)
    auth = s.command(i2, ctx, text=text or p["command"]["command_text"],
                     command_id=p["command"]["command_id"] if same_id else None)
    p2 = place_request(i2, auth, created_at=int(s.clock()))
    q.insert(p2)
    return p2


def test_p1_recover_while_engaged_voids_and_a_resend_after_release_is_refused(tmp_path):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, _, ctx = _place(s, q, "order-key-PA01")
    assert q.claim(p["request_id"], SIGNER_ID)                        # claim committed, the call "failed"
    svc = _svc(s, q)
    (r,) = svc.recover()                                              # worker stage 1, still engaged
    assert _code(r) == "KILL_SWITCH_ENGAGED"
    _release(s, kill)
    s.signer.config.market_data.world = ctx
    for n, same_id in enumerate((True, False)):
        _resend(s, q, p, ctx, f"order-key-PA1{n}", same_id=same_id)
        (res,) = svc.process_once(10)
        assert res["event"] == "REFUSED" and _code(res) in ("COMMAND_VOIDED_BY_KILL_SWITCH",
                                                            "COMMAND_ID_ALREADY_CONSUMED")
    assert _posts(s) == []


def test_p2_engagement_during_processing_voids_and_a_resend_after_release_is_refused(tmp_path, monkeypatch):
    s, q, kill = _stack(tmp_path)
    p, _, ctx = _place(s, q, "order-key-PB01")
    svc = _svc(s, q)
    real = svc._place

    def place_then_engage(*a, **k):
        kill.engage("OWNER_TEST", source="MANUAL")                   # engagement lands inside processing
        return real(*a, **k)
    monkeypatch.setattr(svc, "_place", place_then_engage)
    (r,) = svc.process_once(10)
    assert _code(r) == "KILL_SWITCH_ENGAGED"
    monkeypatch.undo()
    _release(s, kill)
    s.signer.config.market_data.world = ctx
    _resend(s, q, p, ctx, "order-key-PB02")                           # new command id, identical owner text
    (res,) = svc.process_once(10)
    assert res["event"] == "REFUSED" and _code(res) == "COMMAND_VOIDED_BY_KILL_SWITCH" and _posts(s) == []


@pytest.mark.parametrize("variant", [lambda t: t + " ", lambda t: t + ".", lambda t: t.replace(" ", "  ", 1),
                                     lambda t: t.upper()], ids=["space", "dot", "double-space", "upper"])
def test_p2_any_spelling_of_a_voided_operation_is_refused(tmp_path, variant):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, _, ctx = _place(s, q, "order-key-PC01")
    svc = _svc(s, q)
    svc.refuse_pending_places(10)
    _release(s, kill)
    s.signer.config.market_data.world = ctx
    _resend(s, q, p, ctx, "order-key-PC02", text=variant(p["command"]["command_text"]))
    (res,) = svc.process_once(10)
    assert res["event"] == "REFUSED" and _code(res) == "COMMAND_VOIDED_BY_KILL_SWITCH" and _posts(s) == []


@pytest.mark.parametrize("skew", [10, 60, -300])
def test_p2_a_skewed_request_is_voided_by_the_ordering_not_expired(tmp_path, skew):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, _, ctx = _place(s, q, "order-key-PD01", mcp_skew=skew)
    _release(s, kill)
    svc = _svc(s, q)
    (r,) = svc.process_once(10)
    assert _code(r) == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE"
    s.signer.config.market_data.world = ctx
    _resend(s, q, p, ctx, "order-key-PD02", same_id=True)             # MCP clock corrected, same command
    (res,) = svc.process_once(10)
    assert res["event"] == "REFUSED" and _code(res) in ("COMMAND_VOIDED_BY_KILL_SWITCH",
                                                        "COMMAND_ID_ALREADY_CONSUMED") and _posts(s) == []


def test_a_deliberate_new_reference_is_a_new_operation(tmp_path):
    """After the release the owner may repeat the operation on purpose with a new reference "(rif. N)"."""
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, i, ctx = _place(s, q, "order-key-PE01")
    _svc(s, q).refuse_pending_places(10)
    _release(s, kill)
    s.signer.config.market_data.world = ctx
    text = p["command"]["command_text"]
    import re
    new_text = re.sub(r"\(rif\. ?\d+\)", "(rif. 99999)", text)
    assert new_text != text
    _resend(s, q, p, ctx, "order-key-PE02", text=new_text)
    (res,) = _svc(s, q).process_once(10)
    assert res["event"] == "SUBMITTED" and len(_posts(s)) == 1


# ---------------------------------------------------------------- review 0bcbae6: the ECONOMIC order is voided
VARIANTS = {
    "explicit_default_tif": lambda t: t.replace(" (rif.", " GTC (rif."),
    "accented_teams": lambda t: t.replace("Home FC", "Hóme FC"),
    "event_by_id": lambda t: t.replace("in Home FC–Away FC", "in evento L12003787"),
    "market_type": lambda t: t.replace(" (rif.", " (tipo mercato 52) (rif."),
    "teams_vs": lambda t: t.replace("Home FC–Away FC", "Home FC vs Away FC"),
    "decimal_trailing_zero": lambda t: t.replace("quota 2,5", "quota 2,50").replace("per 5 USDC", "per 5,00 USDC"),
}


@pytest.mark.parametrize("name", list(VARIANTS))
def test_p2_equivalent_spellings_of_a_voided_order_are_refused(tmp_path, name):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p, _, ctx = _place(s, q, "order-key-Z001")
    svc = _svc(s, q)
    (r,) = svc.refuse_pending_places(10)
    assert _code(r) == "KILL_SWITCH_ENGAGED"
    _release(s, kill)
    s.signer.config.market_data.world = ctx
    t = p["command"]["command_text"]
    _resend(s, q, p, ctx, "order-key-Z002", text=VARIANTS[name](t))
    (res,) = svc.process_once(10)
    assert res["event"] == "REFUSED" and _code(res) == "COMMAND_VOIDED_BY_KILL_SWITCH" and _posts(s) == []


def test_p2_engagement_during_the_signer_reads_voids_the_order(tmp_path, monkeypatch):
    s, q, kill = _stack(tmp_path)
    p, _, ctx = _place(s, q, "order-key-Y001")
    svc = _svc(s, q)
    real = s.signer._reads

    def reads_then_engage(*a, **k):
        r = real(*a, **k)
        kill.engage("OWNER_TEST", source="MANUAL")                   # engagement during the provider reads
        return r
    monkeypatch.setattr(s.signer, "_reads", reads_then_engage)
    (r,) = svc.process_once(10)
    assert _code(r) == "KILL_SWITCH_ENGAGED"
    assert s.ledger.command_text_voided(p["command"]["command_text_sha256"])
    monkeypatch.undo()
    _release(s, kill)
    s.signer.config.market_data.world = ctx
    _resend(s, q, p, ctx, "order-key-Y002")
    (res,) = svc.process_once(10)
    assert res["event"] == "REFUSED" and _code(res) == "COMMAND_VOIDED_BY_KILL_SWITCH" and _posts(s) == []


def test_p2_malformed_command_id_never_wedges_the_service(tmp_path):
    s, q, kill = _stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    i = s.intent("order-key-U001")
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    p = place_request(i, s.command(i, ctx, command_id="bad id with spaces"), created_at=int(s.clock()))
    q.insert(p)
    svc = _svc(s, q)
    (r,) = svc.refuse_pending_places(10)
    assert r["event"] == "REFUSED" and _code(r) == "REQUEST_COMMAND_ID_INVALID"
    assert svc.recover() == [] and q.states(p["request_id"])[-1] == "REFUSED"


# ---------------------------------------------------------------- review 9e7bfca P3
def test_p3_1_void_failure_inside_sign_order_keeps_the_request_open(tmp_path, monkeypatch):
    """Engagement during processing + a transient void write failure: the request is NOT closed without its void;
    recover() retries, the void lands, and a re-send after the release is refused."""
    from signer import orders as od
    s, q, kill = _stack(tmp_path)
    p, _, ctx = _place(s, q, "order-key-T001")
    svc = _svc(s, q)
    real_reads, real_void = s.signer._reads, s.ledger.void_command
    left = {"n": 2}

    def reads_then_engage(*a, **k):
        r = real_reads(*a, **k)
        kill.engage("OWNER_TEST", source="MANUAL")
        return r

    def flaky_void(**kw):
        if left["n"]:
            left["n"] -= 1
            raise od.LedgerError("LEDGER_WRITE_FAILED")
        return real_void(**kw)
    monkeypatch.setattr(s.signer, "_reads", reads_then_engage)
    monkeypatch.setattr(s.ledger, "void_command", flaky_void)
    with pytest.raises(od.LedgerError):
        svc.process_once(10)
    assert q.states(p["request_id"]) == ["CLAIMED"]                   # open, never closed without its void
    monkeypatch.setattr(s.signer, "_reads", real_reads)
    (r,) = svc.recover()                                              # retried: void written, then refused
    assert r["event"] == "REFUSED" and s.ledger.command_text_voided(p["command"]["command_text_sha256"])
    monkeypatch.undo()
    _release(s, kill)
    s.signer.config.market_data.world = ctx
    _resend(s, q, p, ctx, "order-key-T002")
    (res,) = svc.process_once(10)
    assert _code(res) == "COMMAND_VOIDED_BY_KILL_SWITCH" and _posts(s) == []


def test_p3_3_a_false_claim_also_keeps_queue_order(tmp_path, monkeypatch):
    s, q, kill = _stack(tmp_path)
    a, _, _ = _place(s, q, "order-key-T010")
    b, _, _ = _place(s, q, "order-key-T011")
    real = q.claim
    monkeypatch.setattr(q, "claim", lambda rid, sid: False if rid == a["request_id"] else real(rid, sid))
    assert _svc(s, q).process_once(10) == [] and _posts(s) == []      # B waits for A
