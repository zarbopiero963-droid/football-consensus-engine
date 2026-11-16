"""Signer worker and scheduler (issue #187, S9; governance §2.6). Runs ONLY in the signer process.

One `SignerWorker.run_cycle()` composes the existing, already-reviewed parts in a fixed, fail-closed order:
1. `SignerService.recover()`: requests interrupted by a crash or restart are resolved from the ledger, never
   re-sent blindly;
2. `Monitor.tick()`: read-only reconciliation of every order that may still change (UNKNOWN, open, partial,
   expired-unconfirmed, late fills until settlement), expiry and keep-alive renewal alerts, owner rules. It can
   only READ the exchange (by construction);
3. `SignerService.sync_outcomes()`: final states learnt by step 2 are mirrored to the request queue;
4. `SignerService.process_once()`: new owner requests. While the persistent kill switch is ENGAGED only CANCEL
   requests are processed (owner decision 16-11-2026: cancels of orders already open, with the owner command,
   idempotency, audit and reconciliation of the signer); every pending PLACE request is REFUSED for good
   (KILL_SWITCH_ENGAGED), never signed, so nothing queued around an engagement can run after a release. While its state is
   UNKNOWN nothing is processed (fail closed). Steps 1-3 keep running so open exposure is still reconciled.

Note on step 1: `recover()` may re-run a PLACE request that was claimed but never reached the ledger. That path
still goes through `Signer.sign_order` and the gateway, which re-check the kill switch under their own locks:
while it is engaged such a request is refused (KILL_SWITCH_ENGAGED), never sent. The worker's own check is
defence in depth, not the only barrier.

Each step is isolated: an exception in one is recorded with its type only and the others still run. After
`max_consecutive_failures` cycles in a row with a failing step, the worker ENGAGES the persistent kill switch
(source AUTOMATIC, trigger WORKER_REPEATED_FAILURES): no new order until the owner's signed release.

Single instance: an exclusive, non-blocking lock on `lock_path` (one worker per ledger); a second worker
refuses to start (WORKER_ALREADY_RUNNING). Concurrency inside the process is already serialized by the
ledger's service lock (signer/service.py) and send lock (gateway, cancellation, monitor).

There is no keep-alive SENDER here: the monitor only reports when a renewal is due (policy). Renewing it
is a write to the exchange and needs a separate, owner-reviewed decision.
With a `status_sink` (signer/status.py) the worker publishes, after every cycle, the status the MCP checks before
queueing a trading request (P5). Nothing here enables LIVE trading: sending still needs the owner-signed LIVE flag in the gateway and, on
Toronto, a sender built with writes enabled (signer/toronto_sender.py).
"""
from __future__ import annotations

import fcntl
import os
import time
from typing import Any, Callable

DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
STAGES = ("recover", "monitor_tick", "sync_outcomes", "process_once")


class WorkerError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SignerWorker:
    def __init__(self, service: Any, monitor: Any, kill_switch: Any, *, lock_path: str,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES, batch: int = 10,
                 status_sink: Callable[[dict[str, Any]], None] | None = None):
        if type(max_consecutive_failures) is not int or not 1 <= max_consecutive_failures <= 100:
            raise WorkerError("WORKER_MAX_FAILURES_INVALID")
        if type(batch) is not int or not 1 <= batch <= 100:
            raise WorkerError("WORKER_BATCH_INVALID")
        self._service = service
        self._monitor = monitor
        self._kill = kill_switch
        self._clock = clock
        self._sleep = sleep
        self._max_failures = max_consecutive_failures
        self._batch = batch
        self._failures = 0
        self._stop = False
        self._status_sink = status_sink
        _same_ledger(service, monitor, kill_switch)
        try:
            self._lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise WorkerError("WORKER_LOCK_UNAVAILABLE") from None          # fixed code, no path (§2.1)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            raise WorkerError("WORKER_ALREADY_RUNNING") from None

    def __enter__(self) -> "SignerWorker":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def stop(self) -> None:
        self._stop = True

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def _stage(self, report: dict[str, Any], name: str, fn: Callable[[], Any]) -> Any:
        try:
            out = fn()
        except Exception as exc:  # type only: never a message, DSN or key (§2.1)
            report["stages"][name] = {"ok": False, "error_type": type(exc).__name__}
            return None
        report["stages"][name] = {"ok": True}
        return out

    def run_cycle(self) -> dict[str, Any]:
        report: dict[str, Any] = {"at": int(self._clock()), "stages": {}, "kill_switch_engaged": None}
        rec = self._stage(report, "recover", self._service.recover)
        tick = self._stage(report, "monitor_tick", self._monitor.tick)
        synced = self._stage(report, "sync_outcomes", self._service.sync_outcomes)
        try:
            engaged = self._kill.engaged
            if type(engaged) is not bool:
                raise TypeError("kill switch state is not a bool")
            report["kill_switch_engaged"] = engaged
            report["stages"]["kill_switch_read"] = {"ok": True}
        except Exception as exc:
            engaged = True                    # unknown kill-switch state: fail closed, no new order
            report["kill_switch_engaged"] = "UNKNOWN"
            report["stages"]["kill_switch_read"] = {"ok": False, "error_type": type(exc).__name__}
        if engaged is True and report["kill_switch_engaged"] is True:
            # Known ENGAGED: cancels only (they reduce risk); new placements stay pending.
            processed = self._stage(report, "process_once",
                                    lambda: self._service.process_once(self._batch, kinds=("CANCEL",)))
            report["stages"]["process_once"].update(mode="CANCEL_ONLY_KILL_SWITCH_ENGAGED", only_kinds=["CANCEL"])
            refused = self._stage(report, "refuse_new_placements",
                                  lambda: self._service.refuse_pending_places(self._batch))
            report["counts_refused_placements"] = len(refused or [])
        elif engaged:
            report["stages"]["process_once"] = {"ok": True, "skipped": "KILL_SWITCH_STATE_UNKNOWN"}
            processed = []
        else:
            processed = self._stage(report, "process_once", lambda: self._service.process_once(self._batch))
        report["counts"] = {
            "recovered": len(rec or []), "synced": len(synced or []), "processed": len(processed or []),
            "reconciled": len((tick or {}).get("reconciled", [])), "alerts": len((tick or {}).get("alerts", [])),
            "monitor_network_writes": (tick or {}).get("network_writes")}
        if any(not st["ok"] for st in report["stages"].values()):
            self._failures += 1
        else:
            self._failures = 0
        report["consecutive_failures"] = self._failures
        if self._failures >= self._max_failures and not engaged:
            try:
                self._kill.engage("WORKER_REPEATED_FAILURES", source="AUTOMATIC")
                report["kill_switch_engaged_now"] = "WORKER_REPEATED_FAILURES"
            except Exception as exc:
                report["kill_switch_engage_error"] = type(exc).__name__
        if self._status_sink is not None:
            # P5: publish the signer status for the MCP. A failure here only makes the status go stale, which the
            # MCP treats as "signer unreachable" (fail closed); it is reported but not counted as a stage failure.
            try:
                self._status_sink(report)
                report["status_published"] = True
            except Exception as exc:
                report["status_published"] = {"ok": False, "error_type": type(exc).__name__}
        return report

    def run(self, *, interval_seconds: float, max_cycles: int | None = None,
            on_cycle: Callable[[dict[str, Any]], None] | None = None) -> int:
        """Scheduler: one cycle every `interval_seconds` until stop() or `max_cycles`. Returns cycles run."""
        if type(interval_seconds) not in (int, float) or not 1 <= interval_seconds <= 3600:
            raise WorkerError("WORKER_INTERVAL_INVALID")
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise WorkerError("WORKER_MAX_CYCLES_INVALID")
        cycles = 0
        while not self._stop and (max_cycles is None or cycles < max_cycles):
            report = self.run_cycle()
            cycles += 1
            if on_cycle is not None:
                on_cycle(report)
            if self._stop or (max_cycles is not None and cycles >= max_cycles):
                break
            self._sleep(interval_seconds)
        return cycles


def _same_ledger(service: Any, monitor: Any, kill_switch: Any) -> None:
    """The kill switch the worker engages must be the one the signer and the monitor enforce (review P3-5)."""
    kill_ledger = getattr(kill_switch, "_ledger", None)
    if kill_ledger is None:
        return                                        # test doubles without a ledger
    others = (getattr(getattr(service, "_signer", None), "_ledger", None), getattr(monitor, "_ledger", None))
    ids = {getattr(x, "ledger_id", None) for x in (kill_ledger, *others)}
    if None in ids or len(ids) != 1:
        raise WorkerError("WORKER_LEDGER_MISMATCH")


__all__ = ["DEFAULT_MAX_CONSECUTIVE_FAILURES", "STAGES", "SignerWorker", "WorkerError"]
