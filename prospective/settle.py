#!/usr/bin/env python3
"""Settle finished matches into normalized/results.csv only.

Reads the pre-match ledger. Never writes snapshots, raw files, manifests,
strategies, or the ledger. Identical scores are not appended. A changed
official score is a new version plus an audit line.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(ROOT))
from collect import canon, name_score  # noqa: E402
from app.persist import guarded, set_runtime_state, upsert_result  # noqa: E402
from app.strategy_sync import synchronize_strategy_simulations  # noqa: E402

LEDGER = os.path.join(ROOT, "prospective_ledger.csv")
RESULTS = os.path.join(ROOT, "normalized", "results.csv")
AUDIT = os.path.join(ROOT, "normalized", "result_audit.jsonl")
REPORT = os.path.join(ROOT, "reports", "settlement_status.json")
HISTORY = os.path.join(ROOT, "reports", "settlement_history.jsonl")
UA = "Mozilla/5.0 (compatible; prospective-ledger/1.0; results-only)"
FIFA = "https://api.fifa.com/api/v3/calendar/matches"
MIN_AFTER_KICKOFF = 120
NAME_MIN = 2  # exact canon or containment; token-only matches are refused

ALIASES = {
    "czechia": "czech republic",
    "fyr macedonia": "north macedonia",
    "macedonia": "north macedonia",
    "cape verde islands": "cabo verde",
    "cape verde": "cabo verde",
    "ivory coast": "cote divoire",
    "cote d ivoire": "cote divoire",
    "south korea": "korea republic",
    "korea republic": "korea republic",
    "ir iran": "iran",
    "usa": "united states",
    "united states": "united states",
    "saint kitts and nevis": "st kitts and nevis",
    "st kitts and nevis": "st kitts and nevis",
    "saint lucia": "st lucia",
    "st lucia": "st lucia",
    "saint vincent and the grenadines": "st vincent and the grenadines",
    "dr congo": "congo dr",
    "democratic republic of the congo": "congo dr",
    "turkey": "turkiye",
    "turkiye": "turkiye",
}

STRATS = [f"S{i}" for i in range(1, 10)]
BASES = ["B1", "B2", "B3"]

def utcnow():
    return datetime.now(timezone.utc)

def parse_iso(s):
    dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

def watched_files():
    paths = [
        LEDGER,
        os.path.join(ROOT, "strategies_preregistered.json"),
        os.path.join(ROOT, "manifests", "manifest_20260929T075426Z.json"),
        os.path.join(ROOT, "manifests", "manifest_20260929T080927Z.json"),
        os.path.join(ROOT, "manifests", "manifest_20260929T082036Z.json"),
    ]
    raw_root = os.path.join(ROOT, "raw", "footinsights", "2026-09-29", "20260929T075426Z")
    if os.path.isdir(raw_root):
        names = sorted(os.listdir(raw_root))
        if names:
            paths.append(os.path.join(raw_root, names[0]))
    snap = os.path.join(ROOT, "snapshots", "2026-09-29", "20260929T075426Z", "prospective_ledger_slice.csv")
    if os.path.exists(snap):
        paths.append(snap)
    return [p for p in paths if os.path.exists(p)]

def hash_set(paths=None):
    return {os.path.relpath(p, ROOT): sha256_file(p) for p in (paths or watched_files())}

def alias(name):
    c = canon(name)
    return ALIASES.get(c, c)

def strong(a, b):
    return name_score(alias(a), alias(b)) >= NAME_MIN

def num(v):
    if v is None or v == "":
        return None
    return float(v)

def result_1x2(hg, ag):
    if hg > ag:
        return "1"
    if hg == ag:
        return "X"
    return "2"

def profit(odds, won):
    if odds is None:
        return ""
    return f"{(odds - 1.0) if won else -1.0:.4f}"

def strategies_of(row):
    p = num(row.get("model_pick_probability"))
    edge = num(row.get("picked_edge"))
    same = str(row.get("same_as_favorite")) == "1"
    odds = num(row.get("picked_odds"))
    has_odds = str(row.get("analysis_eligible")) == "1" and odds is not None
    active = {s: 0 for s in STRATS}
    if p is None:
        return active, has_odds, odds, edge
    if p >= 0.50:
        active["S1"] = 1
    if p >= 0.60:
        active["S2"] = 1
    if has_odds and same and p >= 0.50:
        active["S3"] = 1
    if has_odds and same and p >= 0.60:
        active["S4"] = 1
    if has_odds and edge is not None and p >= 0.50 and edge > 0:
        active["S5"] = 1
    if has_odds and edge is not None and p >= 0.60 and edge > 0:
        active["S6"] = 1
    if has_odds and edge is not None and edge >= 0.03:
        active["S7"] = 1
    if has_odds and edge is not None and edge >= 0.05:
        active["S8"] = 1
    if has_odds and edge is not None and edge >= 0.08:
        active["S9"] = 1
    return active, has_odds, odds, edge

def fifa_name(side):
    if not isinstance(side, dict):
        return ""
    v = side.get("TeamName")
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0].get("Description") or ""
    return ""

def fifa_finished(m):
    if not isinstance(m, dict):
        return False
    if m.get("HomeTeamScore") is None or m.get("AwayTeamScore") is None:
        return False
    return m.get("MatchStatus") == 0 and m.get("OfficialityStatus") == 2 and m.get("ResultType") == 1

def fetch_fifa(start, end):
    start = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    url = (
        f"{FIFA}?from={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&to={end.strftime('%Y-%m-%dT%H:%M:%SZ')}&language=en&count=200"
    )
    last_err = None
    for delay in (0, 20, 60):
        if delay:
            time.sleep(delay)
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=40) as r:
                body = r.read()
            if not body or body.strip() in (b"null", b""):
                last_err = "empty response"
                continue
            data = json.loads(body)
            if not isinstance(data, dict):
                last_err = f"unexpected payload {type(data).__name__}"
                continue
            return data.get("Results") or [], None
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
    return [], last_err

def match_fifa(row, matches):
    kick = parse_iso(row["kickoff_utc"])
    hits = []
    for m in matches:
        try:
            when = parse_iso(m.get("Date") or "")
        except ValueError:
            continue
        if abs((when - kick).total_seconds()) > 6 * 3600:
            continue
        home, away = fifa_name(m.get("Home")), fifa_name(m.get("Away"))
        if strong(row["home_team"], home) and strong(row["away_team"], away):
            hits.append(m)
    if len(hits) != 1:
        return None, ("ambiguous" if len(hits) > 1 else "no_unique_source_match")
    return hits[0], None

def grade(row, hg, ag):
    res = result_1x2(hg, ag)
    active, has_odds, odds, edge = strategies_of(row)
    pick = row.get("model_pick") or ""
    won_model = pick == res
    out = {
        "model_pick": pick,
        "model_pick_probability": row.get("model_pick_probability") or "",
        "picked_odds": row.get("picked_odds") or "",
        "picked_edge": row.get("picked_edge") or "",
        "pin_favorite": row.get("pin_favorite") or "",
        "pin_favorite_odds": row.get("pin_favorite_odds") or "",
        "profit_flat_1": profit(odds, won_model) if has_odds else "",
    }
    for s, on in active.items():
        out[f"{s}_active"] = on
        if not on:
            out[f"{s}_correct"] = ""
            out[f"{s}_profit"] = ""
            continue
        correct = int(pick == res)
        out[f"{s}_correct"] = correct
        out[f"{s}_profit"] = profit(odds, bool(correct)) if has_odds else ""
    fav = row.get("pin_favorite") or ""
    fav_odds = num(row.get("pin_favorite_odds"))
    priced = str(row.get("analysis_eligible")) == "1" and fav in ("1", "X", "2") and fav_odds is not None
    fav_won = fav == res
    out["B1_active"] = int(priced)
    out["B1_correct"] = int(fav_won) if priced else ""
    out["B1_profit"] = profit(fav_odds, fav_won) if priced else ""
    out["B2_active"] = int(priced and active["S1"])
    out["B2_correct"] = int(fav_won) if priced and active["S1"] else ""
    out["B2_profit"] = profit(fav_odds, fav_won) if priced and active["S1"] else ""
    out["B3_active"] = int(priced and active["S2"])
    out["B3_correct"] = int(fav_won) if priced and active["S2"] else ""
    out["B3_profit"] = profit(fav_odds, fav_won) if priced and active["S2"] else ""
    return out

def result_fields():
    fields = [
        "match_id", "version", "status", "snapshot_used_utc", "kickoff_utc",
        "home_team", "away_team", "final_home_goals", "final_away_goals", "result_1x2",
        "result_source", "result_source_id", "result_timestamp_utc",
        "fifa_home", "fifa_away", "fifa_kickoff_utc",
        "model_pick", "model_pick_probability", "picked_odds", "picked_edge",
        "pin_favorite", "pin_favorite_odds", "profit_flat_1",
    ]
    for s in STRATS + BASES:
        fields += [f"{s}_active", f"{s}_correct", f"{s}_profit"]
    return fields

def read_results(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def latest_by_match(rows):
    latest = {}
    for r in rows:
        mid = r["match_id"]
        ver = int(r.get("version") or 1)
        if mid not in latest or ver >= int(latest[mid].get("version") or 1):
            latest[mid] = r
    return latest

def commit_rows(existing, new_row):
    """Return (action, version). Does not touch disk."""
    latest = latest_by_match(existing).get(new_row["match_id"])
    if latest is None:
        new_row["version"] = 1
        return "append", new_row
    same = (
        str(latest["final_home_goals"]) == str(new_row["final_home_goals"])
        and str(latest["final_away_goals"]) == str(new_row["final_away_goals"])
        and latest["result_1x2"] == new_row["result_1x2"]
    )
    if same:
        return "skip_identical", latest
    new_row["version"] = int(latest["version"]) + 1
    return "correction", new_row

def append_result(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = result_fields()
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)

def append_audit(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")

def choose_snapshot(rows):
    good = []
    for r in rows:
        if str(r.get("valid_prematch")) != "1":
            continue
        try:
            snap = parse_iso(r["snapshot_timestamp_utc"])
            kick = parse_iso(r["kickoff_utc"])
        except ValueError:
            continue
        if snap < kick:
            good.append(r)
    if not good:
        return None
    good.sort(key=lambda r: parse_iso(r["snapshot_timestamp_utc"]))
    return good[-1]

def group_ledger(rows):
    grouped = {}
    for r in rows:
        grouped.setdefault(r["match_id"], []).append(r)
    return grouped

def summarize(result_rows):
    latest = list(latest_by_match(result_rows).values())
    settled = [r for r in latest if r.get("status") == "settled"]

    def block(prefix):
        active = [r for r in settled if str(r.get(f"{prefix}_active")) == "1"]
        graded = [r for r in active if r.get(f"{prefix}_correct") != ""]
        profits = [float(r[f"{prefix}_profit"]) for r in active if r.get(f"{prefix}_profit") not in ("", None)]
        hits = sum(int(r[f"{prefix}_correct"]) for r in graded)
        n = len(graded)
        stake_n = len(profits)
        pnl = sum(profits) if profits else None
        return {
            "bets": n,
            "priced_bets": stake_n,
            "wins": hits,
            "losses": n - hits,
            "hit_rate": (hits / n) if n else None,
            "profit": None if pnl is None else round(pnl, 4),
            "roi": None if not stake_n else round(pnl / stake_n, 4),
        }

    strategies = {s: block(s) for s in STRATS}
    baselines = {b: block(b) for b in BASES}

    def incr(model, base):
        a, b = strategies[model]["roi"], baselines[base]["roi"]
        if a is None or b is None:
            return None
        return round(a - b, 4)

    return {
        "settled_matches": len(settled),
        "strategies": strategies,
        "baselines": baselines,
        "incremental_roi": {
            "S1_minus_B2": incr("S1", "B2"),
            "S2_minus_B3": incr("S2", "B3"),
        },
        "classification": "INSUFFICIENT SAMPLE" if len(settled) < 100 else "READY_FOR_PREREGISTERED_REPORT",
    }

def write_report(payload):
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    tmp = REPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, REPORT)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at_utc": payload["at_utc"], "settled_matches": payload["settled_matches"], "open_matches": payload["open_matches"], "errors": payload["errors"]}) + "\n")

def run_cycle(now=None, results_path=RESULTS, audit_path=AUDIT, ledger_path=LEDGER, fifa_matches=None, check_hashes=True):
    now = now or utcnow()
    before = hash_set() if check_hashes else {}
    with open(ledger_path, newline="", encoding="utf-8") as f:
        ledger = list(csv.DictReader(f))
    grouped = group_ledger(ledger)
    existing = read_results(results_path)
    latest = latest_by_match(existing)
    errors = []
    pending = []
    due = []
    for mid, rows in grouped.items():
        snap = choose_snapshot(rows)
        if snap is None:
            continue
        kick = parse_iso(snap["kickoff_utc"])
        if now < kick + timedelta(minutes=MIN_AFTER_KICKOFF):
            pending.append({"match_id": mid, "reason": "not_finished", "kickoff_utc": snap["kickoff_utc"]})
            continue
        due.append(snap)
    source = {
        "name": "fifa-calendar-v3",
        "finished_rule": "MatchStatus=0 AND OfficialityStatus=2 AND ResultType=1 AND both scores present",
        "retry_seconds": [0, 20, 60],
        "fetched": False,
        "finished_on_source": 0,
        "not_final_on_source": 0,
        "finished_without_prematch_snapshot": 0,
    }
    fifa_err = None
    matches = fifa_matches
    if matches is None:
        start = now - timedelta(hours=36)
        end = now + timedelta(hours=6)
        if due:
            start = min(start, min(parse_iso(r["kickoff_utc"]) for r in due) - timedelta(hours=12))
            end = max(end, max(parse_iso(r["kickoff_utc"]) for r in due) + timedelta(hours=12))
        matches, fifa_err = fetch_fifa(start, end)
        source["fetched"] = fifa_err is None
        if fifa_err:
            errors.append({"where": "fifa", "error": fifa_err, "retry": "next_cycle_in_600s"})
            matches = []
    else:
        source["fetched"] = True
    matches = matches or []
    eligible_snaps = []
    for rows in grouped.values():
        snap = choose_snapshot(rows)
        if snap is not None:
            eligible_snaps.append(snap)
    for m in matches:
        if fifa_finished(m):
            source["finished_on_source"] += 1
            paired = False
            for snap in eligible_snaps:
                found, _why = match_fifa(snap, [m])
                if found is not None:
                    paired = True
                    break
            if not paired:
                source["finished_without_prematch_snapshot"] += 1
        else:
            source["not_final_on_source"] += 1
    appended = 0
    corrections = 0
    skipped = 0
    if not fifa_err:
        for snap in due:
            found, why = match_fifa(snap, matches)
            if found is None:
                errors.append({"match_id": snap["match_id"], "error": why})
                pending.append({"match_id": snap["match_id"], "reason": why, "kickoff_utc": snap["kickoff_utc"]})
                continue
            if not fifa_finished(found):
                pending.append({"match_id": snap["match_id"], "reason": "source_not_final", "kickoff_utc": snap["kickoff_utc"]})
                continue
            hg = int(found["HomeTeamScore"])
            ag = int(found["AwayTeamScore"])
            graded = grade(snap, hg, ag)
            row = {
                "match_id": snap["match_id"],
                "status": "settled",
                "snapshot_used_utc": snap["snapshot_timestamp_utc"],
                "kickoff_utc": snap["kickoff_utc"],
                "home_team": snap["home_team"],
                "away_team": snap["away_team"],
                "final_home_goals": hg,
                "final_away_goals": ag,
                "result_1x2": result_1x2(hg, ag),
                "result_source": "fifa-calendar-v3",
                "result_source_id": found.get("IdMatch") or "",
                "result_timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fifa_home": fifa_name(found.get("Home")),
                "fifa_away": fifa_name(found.get("Away")),
                "fifa_kickoff_utc": found.get("Date") or "",
            }
            row.update(graded)
            action, stored = commit_rows(existing, row)
            if action == "skip_identical":
                skipped += 1
                continue
            if action == "correction":
                prev = latest[snap["match_id"]]
                append_audit(audit_path, {
                    "at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "match_id": snap["match_id"],
                    "old_version": int(prev["version"]),
                    "new_version": int(stored["version"]),
                    "old_score": f"{prev['final_home_goals']}-{prev['final_away_goals']}",
                    "new_score": f"{hg}-{ag}",
                    "old_result_1x2": prev["result_1x2"],
                    "new_result_1x2": stored["result_1x2"],
                    "old_result_source": prev.get("result_source"),
                    "new_result_source": stored["result_source"],
                })
                corrections += 1
            after_try = hash_set() if check_hashes else before
            if check_hashes and after_try != before:
                errors.append({"where": "hash_guard", "error": "prematch files changed during settlement; result not written"})
                break
            if results_path == RESULTS:
                db_result = guarded(upsert_result, stored)
                if not db_result["ok"]:
                    errors.append({"where": "database_result", "error": db_result["error"]})
            append_result(results_path, stored)
            existing.append({k: str(v) for k, v in stored.items()})
            latest[snap["match_id"]] = existing[-1]
            appended += 1
    after = hash_set() if check_hashes else before
    unchanged = (before == after) if check_hashes else True
    if check_hashes and not unchanged:
        errors.append({"where": "hash_guard", "error": "prematch hash mismatch after cycle"})
    summary = summarize(read_results(results_path)) if os.path.exists(results_path) else summarize([])
    if results_path == RESULTS and (not os.path.exists(RESULTS) or os.path.getsize(RESULTS) == 0):
        os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
        with open(RESULTS, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=result_fields()).writeheader()
        summary = summarize([])
    open_ids = []
    for mid, rows in grouped.items():
        if choose_snapshot(rows) is None:
            continue
        last = latest_by_match(read_results(results_path)).get(mid)
        if not last or last.get("status") != "settled":
            open_ids.append(mid)
    payload = {
        "at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "job": "settle_loop",
        "interval_seconds": 1800,
        "open_matches": len(open_ids),
        "pending": len(open_ids),
        "due": len(due),
        "appended": appended,
        "corrections": corrections,
        "skipped_identical": skipped,
        "errors": errors,
        "result_source": source,
        "pending_reason": "not_finished" if due == [] and not fifa_err else "",
        "prematch_hashes_unchanged": unchanged,
        "hashes_before": before,
        "hashes_after": after,
        "results_path": os.path.relpath(results_path, ROOT),
        **summary,
    }
    if results_path == RESULTS:
        db_state = guarded(set_runtime_state, "settlement", {
            "at_utc": payload["at_utc"],
            "open_matches": payload["open_matches"],
            "settled_matches": payload["settled_matches"],
            "due": payload["due"],
            "appended": payload["appended"],
            "corrections": payload["corrections"],
            "classification": payload["classification"],
        })
        if not db_state["ok"]:
            payload["errors"].append({"where": "database_state", "error": db_state["error"]})
        db_strategy = guarded(synchronize_strategy_simulations)
        if not db_strategy["ok"]:
            payload["errors"].append({"where": "strategy_sync", "error": db_strategy["error"]})
        else:
            payload["strategy_sync"] = db_strategy["result"]
        write_report(payload)
    return payload

def self_test():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="settle-selftest-")
    results = os.path.join(tmp, "results.csv")
    audit = os.path.join(tmp, "audit.jsonl")
    row = {
        "match_id": "fi:test",
        "valid_prematch": "1",
        "snapshot_timestamp_utc": "2026-09-01T10:00:00+00:00",
        "kickoff_utc": "2026-09-01T12:00:00+00:00",
        "home_team": "Czechia",
        "away_team": "England",
        "model_pick": "2",
        "model_pick_probability": "0.70",
        "picked_odds": "1.40",
        "picked_edge": "-0.02",
        "analysis_eligible": "1",
        "same_as_favorite": "1",
        "pin_favorite": "2",
        "pin_favorite_odds": "1.40",
    }
    fifa = [{
        "IdMatch": "abc",
        "Date": "2026-09-01T12:00:00Z",
        "HomeTeamScore": 0,
        "AwayTeamScore": 2,
        "MatchStatus": 0,
        "OfficialityStatus": 2,
        "ResultType": 1,
        "Home": {"TeamName": [{"Description": "Czech Republic"}]},
        "Away": {"TeamName": [{"Description": "England"}]},
    }]
    ledger = os.path.join(tmp, "ledger.csv")
    with open(ledger, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        w.writeheader()
        w.writerow(row)
    now = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    # in-progress gate
    early = run_cycle(now=datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc), results_path=results, audit_path=audit, ledger_path=ledger, fifa_matches=fifa, check_hashes=False)
    assert early["appended"] == 0 and early["settled_matches"] == 0, early
    print("PASS not_in_progress")
    first = run_cycle(now=now, results_path=results, audit_path=audit, ledger_path=ledger, fifa_matches=fifa, check_hashes=False)
    assert first["appended"] == 1 and first["settled_matches"] == 1, first
    saved = read_results(results)
    assert saved[0]["result_1x2"] == "2"
    assert saved[0]["S2_correct"] == "1"
    assert saved[0]["profit_flat_1"] == "0.4000"
    assert saved[0]["B1_correct"] == "1"
    print("PASS settle", saved[0]["home_team"], saved[0]["final_home_goals"], saved[0]["final_away_goals"])
    second = run_cycle(now=now, results_path=results, audit_path=audit, ledger_path=ledger, fifa_matches=fifa, check_hashes=False)
    assert second["appended"] == 0 and second["skipped_identical"] == 1
    assert len(read_results(results)) == 1
    print("PASS no_duplicate")
    fifa[0]["HomeTeamScore"] = 1
    fifa[0]["AwayTeamScore"] = 1
    third = run_cycle(now=now + timedelta(hours=1), results_path=results, audit_path=audit, ledger_path=ledger, fifa_matches=fifa, check_hashes=False)
    assert third["corrections"] == 1 and third["settled_matches"] == 1
    lines = read_results(results)
    assert len(lines) == 2 and lines[-1]["version"] == "2" and lines[-1]["result_1x2"] == "X"
    audit_rows = [json.loads(x) for x in open(audit, encoding="utf-8")]
    assert audit_rows[0]["old_score"] == "0-2" and audit_rows[0]["new_score"] == "1-1"
    print("PASS audit", audit_rows[0]["old_score"], "->", audit_rows[0]["new_score"])
    # live-looking source must not settle
    fifa[0]["MatchStatus"] = 1
    fifa[0]["OfficialityStatus"] = 0
    fifa[0]["HomeTeamScore"] = 0
    fifa[0]["AwayTeamScore"] = 1
    fourth = run_cycle(now=now + timedelta(hours=2), results_path=results, audit_path=audit, ledger_path=ledger, fifa_matches=fifa, check_hashes=False)
    assert fourth["appended"] == 0 and fourth["corrections"] == 0
    print("PASS source_not_final")
    print("SELFTEST_OK", tmp)

def main():
    if "--self-test" in sys.argv:
        self_test()
        return
    payload = run_cycle()
    print(json.dumps({k: payload[k] for k in ("at_utc", "open_matches", "settled_matches", "due", "appended", "skipped_identical", "corrections", "errors", "classification", "prematch_hashes_unchanged")}, indent=2))

if __name__ == "__main__":
    main()
