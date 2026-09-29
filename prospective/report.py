#!/usr/bin/env python3
"""Score only pre-registered strategies. Never retunes a threshold. N<100 is insufficient."""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(ROOT, "prospective_ledger.csv")
RESULTS = os.path.join(ROOT, "normalized", "results.csv")

def main():
    now = datetime.now(timezone.utc)
    n_ledger = 0
    n_eligible = 0
    if os.path.exists(LEDGER):
        with open(LEDGER, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        n_ledger = len(rows)
        n_eligible = sum(r.get("analysis_eligible") == "1" for r in rows)
    n_results = 0
    if os.path.exists(RESULTS):
        with open(RESULTS, newline="", encoding="utf-8") as f:
            n_results = sum(1 for _ in csv.DictReader(f))
    status = "INSUFFICIENT FOR CONCLUSIONS" if n_results < 100 else "READY_FOR_PREREGISTERED_REPORT"
    out = {
        "at_utc": now.isoformat(),
        "status": status,
        "settled_matches": n_results,
        "ledger_rows": n_ledger,
        "eligible_prematch_rows": n_eligible,
        "strategies": "S1-S9 frozen in strategies_preregistered.json",
        "classification": None if n_results < 100 else "compute_only_from_settled_future_rows",
        "note": "No threshold was changed. Historical Foresportia and the old 312-match set are not inputs.",
    }
    path = os.path.join(ROOT, "reports", f"status_{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        raise FileExistsError(path)
    with open(path, "x", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))

if __name__ == "__main__":
    main()
