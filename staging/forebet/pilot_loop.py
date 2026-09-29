#!/usr/bin/env python3
"""Forebet 48h pilot loop. Own lock. 16 planned slots. Retries are not new slots."""
from __future__ import annotations

import fcntl
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pilot

PIDFILE = os.path.join(pilot.VAR, "pilot.pid")
LOCK = os.path.join(pilot.VAR, "pilot.lock")
LOG = os.path.join(pilot.PILOT, "pilot.log")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{pilot.iso()} {msg}\n"
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line)
    sys.stdout.write(line)
    sys.stdout.flush()


def serve():
    os.makedirs(pilot.VAR, exist_ok=True)
    lockf = open(LOCK, "a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("pilot loop already running; this process exits")
        return
    with open(PIDFILE, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    meta = pilot.ensure_started()
    log(
        f"pilot_id={meta['pilot_id']} pid={os.getpid()} planned={pilot.PLANNED} "
        f"deadline_utc={meta['deadline_utc']} ledger=read-only"
    )
    while True:
        meta = pilot.ensure_started()
        action = pilot.next_action(meta)
        if action["action"] == "finish":
            log("window closed; writing final report")
            final = pilot.write_final(meta)
            log(f"finished snapshot_ratio={final['snapshot_ratio']} classification={final['classification']}")
            return
        if action["action"] == "sleep":
            wait = int(action["sleep_seconds"])
            log(f"sleep_seconds={wait} next_slot={action.get('slot')} reason={action.get('reason')}")
            end = time.time() + wait
            while time.time() < end:
                if pilot.utcnow() >= pilot.parse_iso(meta["deadline_utc"]) and pilot.next_action(meta)["action"] == "finish":
                    break
                time.sleep(min(30, max(0.5, end - time.time())))
            continue
        try:
            result = pilot.run_due_slot(meta)
        except Exception as exc:
            log(f"slot exception {type(exc).__name__}: {exc}")
            time.sleep(120)
            continue
        info = pilot.counter(meta)
        log(
            "slot "
            + __import__("json").dumps(
                {
                    "result": result,
                    "snapshot_ratio": info["snapshot_ratio"],
                    "executed_ratio": info["executed_ratio"],
                    "retry_attempts": info["retry_attempts"],
                    "open_slot": info["open_slot"],
                },
                default=str,
            )
        )


if __name__ == "__main__":
    serve()
