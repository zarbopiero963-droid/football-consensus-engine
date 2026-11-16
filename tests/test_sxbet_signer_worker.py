"""S9 (issue #187): signer worker / scheduler. Offline: fakes and the simulated exchange; nothing reaches SX Bet."""
import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import killswitch as ks  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.monitor import Monitor, MonitorState  # noqa: E402
from signer.service import QueueClaimUnavailable, SignerService  # noqa: E402
from signer.worker import SignerWorker, WorkerError  # noqa: E402
from sxbet_e2e_kit import SIGNER_ID, Stack  # noqa: E402
from sxbet_queue_kit import MemoryQueue, place_request  # noqa: E402


class FakeService:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def _do(self, name, value):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError("detail with secret-dsn")
        return value

    def recover(self):
        return self._do("recover", [])

    def sync_outcomes(self):
        return self._do("sync_outcomes", [])

    def process_once(self, limit, kinds=None):
        self.kinds = kinds
        return self._do("process_once", [{"event": "SUBMITTED"}])

    def refuse_pending_places(self, limit):
        return self._do("refuse_pending_places", [{"event": "REFUSED"}])


class FakeMonitor:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def tick(self):
        self.calls += 1
        if self.fail:
            raise ValueError("x")
        return {"reconciled": [1], "alerts": [], "network_writes": 0}


class FakeKill:
    def __init__(self, engaged=False):
        self.engaged, self.engages = engaged, []

    def engage(self, trigger, *, source):
        self.engages.append((trigger, source))
        self.engaged = True


def worker(tmp_path, service=None, monitor=None, kill=None, **kw):
    return SignerWorker(service or FakeService(), monitor or FakeMonitor(), kill or FakeKill(),
                        lock_path=str(tmp_path / "worker.lock"), sleep=lambda s: None, **kw)


def test_cycle_order_and_counts(tmp_path):
    svc = FakeService()
    w = worker(tmp_path, svc)
    r = w.run_cycle()
    assert svc.calls == ["recover", "sync_outcomes", "process_once"]
    assert all(st["ok"] for st in r["stages"].values()) and r["counts"]["processed"] == 1
    assert r["counts"]["monitor_network_writes"] == 0 and r["consecutive_failures"] == 0


def test_kill_switch_engaged_skips_new_orders_but_keeps_reconciling(tmp_path):
    # Declared change (owner decision 16-11-2026): while ENGAGED the worker still processes CANCEL requests only.
    svc, mon = FakeService(), FakeMonitor()
    r = worker(tmp_path, svc, mon, FakeKill(engaged=True)).run_cycle()
    # Declared (review a046ad9 P2-2/P3-2): stage label is a mode, and pending PLACE requests are refused for good.
    assert svc.kinds == ("CANCEL",) and mon.calls == 1 and "recover" in svc.calls
    assert r["stages"]["process_once"] == {"ok": True, "mode": "CANCEL_ONLY_KILL_SWITCH_ENGAGED",
                                           "only_kinds": ["CANCEL"]}
    assert "refuse_pending_places" in svc.calls and r["counts_refused_placements"] == 1


def test_unknown_kill_switch_state_fails_closed(tmp_path):
    class Broken:
        @property
        def engaged(self):
            raise OSError("ledger")
    svc = FakeService()
    r = worker(tmp_path, svc, kill=Broken()).run_cycle()
    assert "process_once" not in svc.calls
    # review 44583a5 P3-1 (declared change): an unreadable switch is UNKNOWN and a failed stage, not "engaged"
    assert r["kill_switch_engaged"] == "UNKNOWN"
    assert r["stages"]["kill_switch_read"] == {"ok": False, "error_type": "OSError"}
    assert r["consecutive_failures"] == 1


def test_a_failing_stage_is_isolated_and_reported_by_type_only(tmp_path):
    svc = FakeService(fail={"recover"})
    r = worker(tmp_path, svc).run_cycle()
    assert r["stages"]["recover"] == {"ok": False, "error_type": "RuntimeError"}
    assert "process_once" in svc.calls and "secret" not in str(r)


def test_repeated_failures_engage_the_persistent_kill_switch(tmp_path):
    kill = FakeKill()
    w = worker(tmp_path, monitor=FakeMonitor(fail=True), kill=kill, max_consecutive_failures=3)
    for _ in range(2):
        assert "kill_switch_engaged_now" not in w.run_cycle()
    r = w.run_cycle()
    assert r["kill_switch_engaged_now"] == "WORKER_REPEATED_FAILURES"
    assert kill.engages == [("WORKER_REPEATED_FAILURES", "AUTOMATIC")]


def test_a_successful_cycle_resets_the_failure_counter(tmp_path):
    mon = FakeMonitor(fail=True)
    w = worker(tmp_path, monitor=mon)
    w.run_cycle()
    mon.fail = False
    assert w.run_cycle()["consecutive_failures"] == 0


def test_claim_unavailable_counts_as_a_failure(tmp_path):
    class Svc(FakeService):
        def process_once(self, limit, kinds=None):
            raise QueueClaimUnavailable()
    r = worker(tmp_path, Svc()).run_cycle()
    assert r["stages"]["process_once"] == {"ok": False, "error_type": "QueueClaimUnavailable"}


def test_single_instance(tmp_path):
    w = worker(tmp_path)
    with pytest.raises(WorkerError) as e:
        worker(tmp_path)
    assert e.value.code == "WORKER_ALREADY_RUNNING"
    w.close()
    worker(tmp_path).close()                                  # free again after close


def test_scheduler_runs_bounded_cycles_and_stops(tmp_path):
    sleeps = []
    w = SignerWorker(FakeService(), FakeMonitor(), FakeKill(), lock_path=str(tmp_path / "l"),
                     sleep=sleeps.append)
    assert w.run(interval_seconds=5, max_cycles=3) == 3 and sleeps == [5, 5]
    seen = []
    w2 = SignerWorker(FakeService(), FakeMonitor(), FakeKill(), lock_path=str(tmp_path / "l2"), sleep=lambda s: None)
    w2.run(interval_seconds=1, on_cycle=lambda r: (seen.append(r), w2.stop()))
    assert len(seen) == 1
    with pytest.raises(WorkerError):
        w2.run(interval_seconds=0)


@pytest.mark.parametrize("kw", [{"max_consecutive_failures": 0}, {"batch": 0}, {"batch": "10"}])
def test_bad_settings_refused(tmp_path, kw):
    with pytest.raises(WorkerError):
        worker(tmp_path, **kw)


# ---------------------------------------------------------------- end to end with the real parts
def test_end_to_end_worker_places_reconciles_and_stops_on_kill_switch(tmp_path):
    s = Stack(tmp_path)
    q = MemoryQueue()
    svc = SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)
    mon = Monitor(s.ledger, s.adapter, MonitorState.create(str(tmp_path / "mon.json"), ledger_id=s.ledger.ledger_id),
                  clock=s.clock)
    kill = ks.KillSwitch(s.ledger, AuditLog(str(tmp_path / "ks.jsonl")), owner_public_key=s.owner.public_key(),
                         network="mainnet", clock=s.clock)
    w = SignerWorker(svc, mon, kill, lock_path=str(tmp_path / "w.lock"), sleep=lambda x: None)
    i = s.intent("order-key-W001")
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    p = place_request(i, s.command(i, ctx), created_at=int(s.clock()))
    q.insert(p)
    r = w.run_cycle()
    assert r["counts"]["processed"] == 1 and q.states(p["request_id"])[-1] == "SUBMITTED"
    oid = q.events_of(p["request_id"])[-1]["detail"]["order_id"]
    s.sim.fill_maker_order(oid, 5_000_000)
    w.run_cycle()                                              # monitor reconciles the fill, sync mirrors it
    assert s.state("order-key-W001") == "FILLED" and q.states(p["request_id"])[-1] == "DONE"
    kill.engage("OWNER_TEST", source="MANUAL")
    i2 = s.intent("order-key-W002")
    ctx2 = s.context(i2)
    s.signer.config.market_data.world = ctx2
    p2 = place_request(i2, s.command(i2, ctx2), created_at=int(s.clock()))
    q.insert(p2)
    r = w.run_cycle()
    # Declared (review a046ad9 P2-2): the PLACE queued while ENGAGED is closed for good, never signed or sent.
    assert r["stages"]["process_once"]["mode"] == "CANCEL_ONLY_KILL_SWITCH_ENGAGED"
    assert q.states(p2["request_id"]) == ["CLAIMED", "REFUSED"]
    assert q.events_of(p2["request_id"])[-1]["detail"]["code"] == "KILL_SWITCH_ENGAGED"
    posts = [x for x in s.sim.writes() if x.method == "POST"]
    assert len(posts) == 1                                     # only the first order ever left
    w.close()


# ---------------------------------------------------------------- review 44583a5 P3 fixes
@pytest.mark.parametrize("kw", [{"interval_seconds": True}, {"interval_seconds": 5, "max_cycles": 0},
                                {"interval_seconds": 5, "max_cycles": -1}, {"interval_seconds": 5, "max_cycles": True}])
def test_scheduler_settings_are_exact(tmp_path, kw):
    with worker(tmp_path) as w, pytest.raises(WorkerError):
        w.run(**kw)


def test_unopenable_lock_path_is_a_fixed_code_without_the_path(tmp_path):
    with pytest.raises(WorkerError) as e:
        SignerWorker(FakeService(), FakeMonitor(), FakeKill(), lock_path=str(tmp_path / "missing" / "w.lock"))
    assert e.value.code == "WORKER_LOCK_UNAVAILABLE" and "missing" not in str(e.value)


def test_context_manager_releases_the_lock(tmp_path):
    with worker(tmp_path):
        pass
    worker(tmp_path).close()


def test_non_bool_kill_state_fails_closed(tmp_path):
    svc = FakeService()
    r = worker(tmp_path, svc, kill=FakeKill(engaged="no")).run_cycle()
    assert "process_once" not in svc.calls and r["kill_switch_engaged"] == "UNKNOWN"


def test_kill_switch_on_another_ledger_is_refused(tmp_path):
    class L:
        def __init__(self, i):
            self.ledger_id = i

    class K(FakeKill):
        _ledger = L("a")

    class S(FakeService):
        class _signer:
            _ledger = L("b")

    class M(FakeMonitor):
        _ledger = L("a")
    with pytest.raises(WorkerError) as e:
        worker(tmp_path, S(), M(), K())
    assert e.value.code == "WORKER_LEDGER_MISMATCH"
    S._signer._ledger = L("a")
    worker(tmp_path, S(), M(), K()).close()


def test_end_to_end_kill_switch_engaged_processes_owner_cancels_of_open_orders_only(tmp_path):
    """Owner decision 16-11-2026: with the kill switch ENGAGED, a cancel of an order already open is processed (owner
    command verified, idempotency, audit, reconciliation by the monitor); a new PLACE stays pending, never sent."""
    from signer import cancellation as cx
    from sxbet_queue_kit import cancel_request
    s = Stack(tmp_path)
    q = MemoryQueue()
    svc = SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)
    mon = Monitor(s.ledger, s.adapter, MonitorState.create(str(tmp_path / "mon.json"), ledger_id=s.ledger.ledger_id),
                  clock=s.clock)
    kill = ks.KillSwitch(s.ledger, AuditLog(str(tmp_path / "ks.jsonl")), owner_public_key=s.owner.public_key(),
                         network="mainnet", clock=s.clock)
    _, sg, _ = s.place("order-key-W010")                       # an order already open on the exchange
    s.gw.reconcile("order-key-W010")
    kill.engage("OWNER_TEST", source="MANUAL")
    i2 = s.intent("order-key-W011")
    ctx2 = s.context(i2)
    s.signer.config.market_data.world = ctx2
    p_place = place_request(i2, s.command(i2, ctx2), created_at=int(s.clock()))
    q.insert(p_place)
    auth = s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[sg["order_id"]])
    p_cancel = cancel_request(auth, scope="ORDER_IDS", order_ids=[sg["order_id"]], created_at=int(s.clock()))
    q.insert(p_cancel)
    posts_before = len([x for x in s.sim.writes() if x.method == "POST"])
    with SignerWorker(svc, mon, kill, lock_path=str(tmp_path / "w.lock"), sleep=lambda x: None) as w:
        r = w.run_cycle()
    assert r["stages"]["process_once"]["only_kinds"] == ["CANCEL"] and r["counts"]["processed"] == 1
    assert q.states(p_cancel["request_id"])[-1] == "DONE"
    assert q.states(p_place["request_id"]) == ["CLAIMED", "REFUSED"]   # closed for good, never signed (P2-2)
    assert len([x for x in s.sim.writes() if x.method == "POST"]) == posts_before
    assert len([x for x in s.sim.writes() if x.method == "DELETE"]) == 1


def test_a_placement_queued_while_engaged_can_never_run_after_a_release(tmp_path):
    """Review a046ad9 P2-2: refused terminally while ENGAGED; once released, the refused request is never retried."""
    s = Stack(tmp_path)
    q = MemoryQueue()
    svc = SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)
    i = s.intent("order-key-W020")
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    p = place_request(i, s.command(i, ctx), created_at=int(s.clock()))
    q.insert(p)
    s.ledger.engage_volatile_kill("TEST")                          # declared: refusal now needs an engaged switch
    assert [r["event"] for r in svc.refuse_pending_places(10)] == ["REFUSED"]
    s.ledger.clear_volatile_kill()
    assert svc.process_once(10) == []                              # after a release nothing is left to run
    assert [x for x in s.sim.writes() if x.method == "POST"] == []


def test_row_kind_must_match_the_payload_kind(tmp_path):
    """Review a046ad9 P3-1: a CANCEL row carrying a PLACE payload is refused, never routed to placement."""
    s = Stack(tmp_path)
    q = MemoryQueue()
    svc = SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)
    i = s.intent("order-key-W021")
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    p = place_request(i, s.command(i, ctx), created_at=int(s.clock()))
    q.insert(p)
    rid = p["request_id"]
    q.requests = [(r[0], "CANCEL", *r[2:]) if r[0] == rid else r for r in q.requests]
    (res,) = svc.process_once(10, kinds=("CANCEL",))
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "REQUEST_KIND_MISMATCH"
    assert [x for x in s.sim.writes() if x.method == "POST"] == []


class _FlakyAppendQueue(MemoryQueue):
    """The REFUSED append fails once after the claim committed (DB error between the two writes)."""
    def __init__(self):
        super().__init__()
        self.fail_refused = True

    def append_event(self, request_id, state, detail, signer_id):
        if state == "REFUSED" and self.fail_refused:
            self.fail_refused = False
            raise ConnectionError("db")
        return super().append_event(request_id, state, detail, signer_id)


def test_kill_refusal_drains_the_whole_backlog(tmp_path):
    s = Stack(tmp_path)
    q = MemoryQueue()
    svc = SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)
    for n in range(5):
        i = s.intent(f"order-key-W04{n}")
        ctx = s.context(i)
        s.signer.config.market_data.world = ctx
        q.insert(place_request(i, s.command(i, ctx), created_at=int(s.clock())))
    s.ledger.engage_volatile_kill("TEST")                          # declared: refusal now needs an engaged switch
    assert len(svc.refuse_pending_places(2)) == 5 and q.pending("mainnet", 10) == []
    s.ledger.clear_volatile_kill()
    assert svc.refuse_pending_places(2) == []                      # not engaged: nothing refused (P3-1)


class _FlakyClaimQueue(MemoryQueue):
    """The claim fails once WITHOUT committing (e.g. a lock timeout): the request stays pending."""
    def __init__(self):
        super().__init__()
        self.fail_claim = True

    def claim(self, request_id, signer_id):
        if self.fail_claim:
            self.fail_claim = False
            raise TimeoutError("lock")
        return super().claim(request_id, signer_id)


# ---------------------------------------------------------------- owner decision 16-11-2026: persistent refusal
# A placement that existed while the kill switch was engaged stays refused after the owner's signed release,
# whatever happened in between. These tests replace the in-process-set versions (declared: they now use the real
# ledger-backed kill switch and an owner-signed release, and also cover restart and concurrency).
def _kill_stack(tmp_path, queue=None):
    s = Stack(tmp_path)
    q = queue if queue is not None else MemoryQueue()
    kill = ks.KillSwitch(s.ledger, AuditLog(str(tmp_path / "ks.jsonl")), owner_public_key=s.owner.public_key(),
                         network="mainnet", clock=s.clock, queue_watermark=q.release_watermark)
    return s, q, kill


def _svc(s, q):
    return SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)


def _owner_release(s, kill):
    now, seq = int(s.clock()), kill.state()["engage_seq"]
    rid = f"release-{seq:08d}-{now}"
    msg = ks.release_message(ledger_id=s.ledger.ledger_id, engage_seq=seq, release_id=rid, issued_at=now,
                             expires_at=now + 60)
    return kill.release(ks.KillSwitchRelease(s.ledger.ledger_id, seq, rid, now, now + 60, s.owner.sign(msg)))


def _queue_place(s, q, key):
    i = s.intent(key)
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    p = place_request(i, s.command(i, ctx), created_at=int(s.clock()))
    q.insert(p)
    return p


def _posts(s):
    return [x for x in s.sim.writes() if x.method == "POST"]


def test_placement_never_refused_in_time_is_refused_after_release(tmp_path):
    """Worst case: the refusal never ran at all (crash right after the engagement), then the owner releases."""
    s, q, kill = _kill_stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p = _queue_place(s, q, "order-key-W060")
    _owner_release(s, kill)
    (res,) = _svc(s, q).process_once(10)
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE"
    assert _posts(s) == []


def test_refusal_survives_a_restart_after_release(tmp_path):
    """The release instant is durable: replayed from the ledger after a process restart (no in-memory state)."""
    s, q, kill = _kill_stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    p = _queue_place(s, q, "order-key-W061")
    _owner_release(s, kill)
    s.restart()                                                   # ledger replayed from disk
    assert s.ledger.last_kill_release_at() is not None
    (res,) = _svc(s, q).process_once(10)
    assert res["detail"]["code"] == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE" and _posts(s) == []


def test_claim_committed_refusal_lost_then_restart_and_release(tmp_path):
    """Claim committed, REFUSED write lost, process restart, owner release: recover() refuses, never signs."""
    s, q, kill = _kill_stack(tmp_path, _FlakyAppendQueue())
    kill.engage("OWNER_TEST", source="MANUAL")
    p = _queue_place(s, q, "order-key-W062")
    with pytest.raises(ConnectionError):
        _svc(s, q).refuse_pending_places(10)
    assert q.states(p["request_id"]) == ["CLAIMED"]
    _owner_release(s, kill)
    s.restart()
    (res,) = _svc(s, q).recover()
    assert res["event"] == "REFUSED" and res["detail"]["code"] == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE"
    assert _posts(s) == []


def test_claim_failed_uncommitted_then_restart_and_release(tmp_path):
    """Claim failed without committing (still pending), restart, release: process_once refuses, never signs."""
    s, q, kill = _kill_stack(tmp_path, _FlakyClaimQueue())
    kill.engage("OWNER_TEST", source="MANUAL")
    p = _queue_place(s, q, "order-key-W063")
    assert [r["event"] for r in _svc(s, q).refuse_pending_places(10)] == ["CLAIM_FAILED"]
    _owner_release(s, kill)
    s.restart()
    (res,) = _svc(s, q).process_once(10)
    assert res["detail"]["code"] == "PLACE_QUEUED_BEFORE_KILL_SWITCH_RELEASE" and _posts(s) == []


def test_a_new_command_after_the_release_is_processed_normally(tmp_path):
    """Not a permanent block: a placement created after the release (new owner command) is signed and sent."""
    s, q, kill = _kill_stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    _owner_release(s, kill)
    s.clock.t += 10                                               # past the 5 s clock tolerance
    p = _queue_place(s, q, "order-key-W064")
    (res,) = _svc(s, q).process_once(10)
    assert res["event"] == "SUBMITTED" and len(_posts(s)) == 1


def test_release_waits_for_a_request_being_processed(tmp_path):
    """Concurrency: the release cannot land between the check and the signature (it takes the service lock)."""
    import threading
    s, q, kill = _kill_stack(tmp_path)
    kill.engage("OWNER_TEST", source="MANUAL")
    released = threading.Event()
    with s.ledger.service_lock:                                   # a request is being processed
        t = threading.Thread(target=lambda: (_owner_release(s, kill), released.set()))
        t.start()
        assert not released.wait(0.3)                             # the release is blocked meanwhile
        assert s.ledger.kill_switch_engaged
    t.join(5)
    assert released.is_set() and not s.ledger.kill_switch_engaged


def test_worker_cycle_engaged_then_released_never_sends_the_old_placement(tmp_path):
    """Through the worker: queued while ENGAGED, refused, released, next cycles: never sent."""
    s, q, kill = _kill_stack(tmp_path)
    svc = _svc(s, q)
    mon = Monitor(s.ledger, s.adapter, MonitorState.create(str(tmp_path / "m.json"), ledger_id=s.ledger.ledger_id),
                  clock=s.clock)
    kill.engage("OWNER_TEST", source="MANUAL")
    p = _queue_place(s, q, "order-key-W065")
    with SignerWorker(svc, mon, kill, lock_path=str(tmp_path / "w.lock"), sleep=lambda x: None) as w:
        w.run_cycle()
        _owner_release(s, kill)
        w.run_cycle()
        w.run_cycle()
    assert q.states(p["request_id"]) == ["CLAIMED", "REFUSED"] and _posts(s) == []


def test_places_blocked_by_an_unresolved_request_is_a_visible_stage_failure(tmp_path):
    """Review 9e7bfca P3-2: never a silent block: the stage fails, last_cycle_ok goes false, repeated cycles engage."""
    from signer.service import PlacesBlockedByUnresolvedRequest

    class Svc(FakeService):
        def process_once(self, limit, kinds=None):
            raise PlacesBlockedByUnresolvedRequest()
    kill = FakeKill()
    w = worker(tmp_path, Svc(), kill=kill, max_consecutive_failures=2)
    r = w.run_cycle()
    assert r["stages"]["process_once"] == {"ok": False, "error_type": "PlacesBlockedByUnresolvedRequest"}
    w.run_cycle()
    assert kill.engages == [("WORKER_REPEATED_FAILURES", "AUTOMATIC")]
