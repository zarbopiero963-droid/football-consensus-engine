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
    assert svc.kinds == ("CANCEL",) and mon.calls == 1 and "recover" in svc.calls
    assert r["stages"]["process_once"] == {"ok": True, "skipped": "KILL_SWITCH_ENGAGED", "only_kinds": ["CANCEL"]}


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
    assert r["stages"]["process_once"]["skipped"] == "KILL_SWITCH_ENGAGED" and q.states(p2["request_id"]) == []
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
    assert q.states(p_place["request_id"]) == []                # pending, unclaimed
    assert len([x for x in s.sim.writes() if x.method == "POST"]) == posts_before
    assert len([x for x in s.sim.writes() if x.method == "DELETE"]) == 1
