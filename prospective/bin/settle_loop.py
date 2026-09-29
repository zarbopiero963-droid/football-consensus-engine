#!/usr/bin/env python3
"""Separate permanent settlement process. Every 30 minutes. No end date.

Does not collect odds or predictions. Does not hold the collector lock.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import settle  # noqa: E402

VAR = os.path.join(ROOT, "var")
LOG = os.path.join(ROOT, "logs", "settlement.log")
PIDFILE = os.path.join(VAR, "settle.pid")
LOCK = os.path.join(VAR, "settle.lock")
INTERVAL = 30 * 60
FAIL_SLEEP = 10 * 60

def iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{iso()} {msg}\n"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)
    sys.stdout.write(line)
    sys.stdout.flush()

def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

def running_pid():
    if not os.path.exists(PIDFILE):
        return None
    try:
        pid = int(open(PIDFILE, encoding="utf-8").read().strip())
    except ValueError:
        return None
    return pid if pid_alive(pid) else None

def serve():
    os.makedirs(VAR, exist_ok=True)
    lockf = open(LOCK, "a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"settle loop already running pid={running_pid()}; this process exits")
        return
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    log(f"settle loop start pid={os.getpid()} interval_seconds={INTERVAL} expires=never")
    while True:
        try:
            payload = settle.run_cycle()
            log(
                "cycle "
                f"open={payload['open_matches']} settled={payload['settled_matches']} "
                f"due={payload['due']} appended={payload['appended']} "
                f"skipped={payload['skipped_identical']} corrections={payload['corrections']} "
                f"hashes_unchanged={payload['prematch_hashes_unchanged']} "
                f"errors={len(payload['errors'])} class={payload['classification']}"
            )
            wait = INTERVAL
        except Exception as e:
            log(f"cycle exception {type(e).__name__}: {e}; retry_seconds={FAIL_SLEEP}")
            wait = FAIL_SLEEP
        end = time.time() + wait
        while time.time() < end:
            time.sleep(min(30, max(0.5, end - time.time())))
            if lockf.closed:
                return

def ensure():
    existing = running_pid()
    if existing:
        print(f"already_running pid={existing}")
        return
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    out = open(os.path.join(ROOT, "logs", "settlement.out"), "a")
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve"],
        cwd=ROOT,
        stdout=out,
        stderr=out,
        start_new_session=True,
    )
    for _ in range(50):
        time.sleep(0.1)
        pid = running_pid()
        if pid:
            print(f"started pid={pid}")
            return
    print("spawned pidfile_not_ready")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--serve", action="store_true")
    p.add_argument("--ensure", action="store_true")
    args = p.parse_args()
    if args.ensure:
        ensure()
    else:
        serve()

if __name__ == "__main__":
    main()
