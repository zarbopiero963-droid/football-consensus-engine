#!/usr/bin/env python3
"""Run the Football Charts staging pilot every 3 hours until 48 hours from start."""
from __future__ import annotations

import fcntl
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pilot

PIDFILE = os.path.join(pilot.PILOT, "var", "pilot.pid")
LOCK = os.path.join(pilot.PILOT, "var", "pilot.lock")
LOG = os.path.join(pilot.PILOT, "pilot.log")

def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{pilot.iso()} {msg}\n"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)
    sys.stdout.write(line)
    sys.stdout.flush()

def deadline():
    meta = pilot.ensure_started()
    return datetime.fromisoformat(meta["deadline_utc"].replace("Z", "+00:00"))

def serve():
    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
    lockf = open(LOCK, "a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("pilot loop already running; this process exits")
        return
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    end = deadline()
    log(f"pilot loop pid={os.getpid()} interval_seconds={pilot.INTERVAL} deadline_utc={pilot.iso(end)}")
    while True:
        now = pilot.utcnow()
        if now >= end:
            log("window closed; writing final report")
            pilot.write_final()
            log("pilot finished")
            return
        last = pilot.latest_run_dir()
        age = None
        if last:
            try:
                started = datetime.strptime(os.path.basename(last), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                age = (now - started).total_seconds()
            except ValueError:
                age = None
        if age is not None and age < pilot.INTERVAL - 30:
            wait = int(pilot.INTERVAL - age)
            log(f"sleep_seconds={wait} last_run_age_seconds={int(age)}")
        else:
            try:
                summary = pilot.run_cycle()
                log("cycle " + __import__("json").dumps(summary))
            except Exception as e:
                log(f"cycle exception {type(e).__name__}: {e}")
                wait = 600
                time.sleep(wait)
                continue
            wait = pilot.INTERVAL
        end_sleep = time.time() + wait
        while time.time() < end_sleep:
            if pilot.utcnow() >= deadline():
                break
            time.sleep(min(30, max(0.5, end_sleep - time.time())))

if __name__ == "__main__":
    serve()
