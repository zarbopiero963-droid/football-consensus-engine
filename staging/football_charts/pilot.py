#!/usr/bin/env python3
"""48-hour Football Charts pilot. Staging only. Never writes the main ledger."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
PILOT = os.path.join(ROOT, "pilot")
LEDGER = os.path.abspath(os.path.join(ROOT, "..", "..", "prospective", "prospective_ledger.csv"))
API = "https://footballcharts-backend.onrender.com/api/v1"
SITE = "https://www.football-charts.com/predictions/"
UA = "Mozilla/5.0 (compatible; fc-pilot/1.0; staging-only)"
INTERVAL = 3 * 3600
WINDOW = timedelta(hours=48)
API_GAP = 3.2
HTML_GAP = 0.4
FULL_SCAN_EVERY = timedelta(hours=24)

ROW_RE = re.compile(
    r'font-medium text-slate-800">([^<]+)</td>'
    r'<td class="py-2 pr-4 text-right font-semibold">([0-9.]+)%</td>'
    r'<td class="py-2 pr-4 text-right text-slate-500">([0-9.]+)%</td>'
)

def utcnow():
    return datetime.now(timezone.utc)

def iso(dt=None):
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def stamp(dt=None):
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def canon(s: str) -> str:
    s = strip_accents(s or "").lower().replace("&", " and ").replace("'", "")
    s = s.replace(".", " ")
    s = re.sub(r"\b(fc|cf|sc|ac|afc|ud|cd|sd|fk|sk|sv|rc|as|ss|us|osc)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def tokens(s: str):
    return {t for t in canon(s).split() if len(t) > 1}

def name_score(a, b) -> int:
    na, nb = canon(a), canon(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 3
    if na in nb or nb in na:
        return 2
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb or not (ta & tb):
        return 0
    j = len(ta & tb) / len(ta | tb)
    return 1 if j >= 0.6 else 0

def side_flags(s: str) -> str:
    n = canon(s)
    flags = []
    if re.search(r"\b(women|woman|ladies|femenino|femminile|frauen)\b", n):
        flags.append("women")
    if re.search(r"\b(u19|u20|u21|u23|u18|reserves|reserve|ii|b)\b", n):
        flags.append("youth_or_b")
    return ",".join(flags)

def parse_iso(s: str):
    dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def kickoff_utc(match_date: str, clock: str):
    """JSON date + time are naive UTC. Do not apply the HTML 'CET' label."""
    clock = (clock or "").strip()
    if len(clock) == 5:
        clock += ":00"
    if len(clock) != 8:
        return None
    return f"{match_date}T{clock}Z"

def fetch(url: str, accept: str, errors: list, pause: float):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    fetched = iso()
    try:
        with urlopen(req, timeout=50) as r:
            body = r.read()
            status = r.status
    except HTTPError as e:
        body = e.read()
        status = e.code
        errors.append({"url": url, "status": status, "at_utc": fetched, "body": body[:180].decode("utf-8", "replace")})
        if status == 429:
            time.sleep(65)
            fetched = iso()
            try:
                with urlopen(req, timeout=50) as r:
                    body = r.read()
                    status = r.status
                    errors.append({"url": url, "status": "retry_ok", "at_utc": fetched})
            except HTTPError as e2:
                body = e2.read()
                status = e2.code
                errors.append({"url": url, "status": status, "at_utc": iso(), "retry": True, "body": body[:180].decode("utf-8", "replace")})
                time.sleep(pause)
                return status, body, fetched
            except (URLError, TimeoutError, OSError) as e2:
                errors.append({"url": url, "error": f"{type(e2).__name__}: {e2}", "at_utc": iso(), "retry": True})
                time.sleep(pause)
                return 0, b"", fetched
    except (URLError, TimeoutError, OSError) as e:
        errors.append({"url": url, "error": f"{type(e).__name__}: {e}", "at_utc": fetched})
        time.sleep(pause)
        return 0, b"", fetched
    time.sleep(pause)
    return status, body, fetched

def explicit_triplet(html: str):
    rows = ROW_RE.findall(html)
    if len(rows) < 3:
        return None
    home, draw, away = rows[0], rows[1], rows[2]
    if draw[0] != "Draw":
        return None
    if not home[0].endswith(" win") or not away[0].endswith(" win"):
        return None
    try:
        p1 = float(home[1]) / 100.0
        px = float(draw[1]) / 100.0
        p2 = float(away[1]) / 100.0
    except ValueError:
        return None
    return {"p1": p1, "px": px, "p2": p2, "labels": [home[0], draw[0], away[0]]}

def read_ledger():
    import csv
    if not os.path.exists(LEDGER):
        return []
    with open(LEDGER, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    best = {}
    for r in rows:
        mid = r.get("match_id")
        if mid not in best or (r.get("snapshot_timestamp_utc") or "") > (best[mid].get("snapshot_timestamp_utc") or ""):
            best[mid] = r
    return list(best.values())

def flags_compatible(a: str, b: str) -> bool:
    return side_flags(a) == side_flags(b)

def match_ledger(home, away, kick, ledger):
    hit_fi = []
    hit_pin = []
    for r in ledger:
        if not flags_compatible(home, r.get("home_team") or ""):
            continue
        if not flags_compatible(away, r.get("away_team") or ""):
            continue
        if name_score(home, r.get("home_team") or "") < 2:
            continue
        if name_score(away, r.get("away_team") or "") < 2:
            continue
        try:
            rk = parse_iso(r.get("kickoff_utc") or "")
        except ValueError:
            continue
        if abs((rk - kick).total_seconds()) > 30 * 60:
            continue
        item = {"match_id": r.get("match_id"), "league": r.get("league"), "kickoff_utc": r.get("kickoff_utc")}
        hit_fi.append(item)
        if str(r.get("analysis_eligible")) == "1" and r.get("pin_favorite"):
            hit_pin.append(item)
    return hit_fi, hit_pin

def complete_run_ids():
    runs = os.path.join(PILOT, "runs")
    if not os.path.isdir(runs):
        return []
    names = []
    for name in sorted(os.listdir(runs)):
        if os.path.exists(os.path.join(runs, name, "manifest.json")):
            names.append(name)
    return names

def latest_run_dir():
    names = complete_run_ids()
    return os.path.join(PILOT, "runs", names[-1]) if names else None

def leagues_for_this_run(now):
    last = latest_run_dir()
    if last is None:
        return None, "full_no_previous_run"
    marker = os.path.join(last, "scan_mode.txt")
    # full scan if the newest full scan is older than 24h
    fulls = []
    runs = os.path.join(PILOT, "runs")
    for name in complete_run_ids():
        mode_path = os.path.join(runs, name, "scan_mode.txt")
        if os.path.exists(mode_path) and open(mode_path, encoding="utf-8").read().startswith("full"):
            fulls.append(name)
    if not fulls:
        return None, "full_no_full_scan_yet"
    last_full = max(fulls)
    last_full_dt = datetime.strptime(last_full, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if now - last_full_dt >= FULL_SCAN_EVERY:
        return None, "full_24h"
    active_path = os.path.join(last, "active_leagues.json")
    if not os.path.exists(active_path):
        return None, "full_missing_active"
    codes = json.load(open(active_path, encoding="utf-8"))
    return codes, "active_only"

def run_cycle():
    now = utcnow()
    run_id = stamp(now)
    run_dir = os.path.join(PILOT, "runs", run_id)
    os.makedirs(os.path.join(run_dir, "fixtures"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "html"), exist_ok=True)
    errors = []
    files = []

    def save(rel, body, fetched):
        path = os.path.join(run_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            errors.append({"error": "refusing_overwrite", "path": rel})
            return
        with open(path, "wb") as f:
            f.write(body)
        files.append({"path": rel, "sha256": sha256_bytes(body), "bytes": len(body), "fetched_utc": fetched})

    codes, mode = leagues_for_this_run(now)
    with open(os.path.join(run_dir, "scan_mode.txt"), "w", encoding="utf-8") as f:
        f.write(mode + "\n")
    if codes is None:
        status, body, fetched = fetch(API + "/leagues/", "application/json", errors, API_GAP)
        save("leagues.json", body, fetched)
        if status != 200:
            errors.append({"where": "leagues", "status": status})
            codes = []
        else:
            payload = json.loads(body)
            codes = [x.get("league") for x in payload.get("leagues") or [] if x.get("league")]
            save_json_note = payload.get("count")
        league_codes = codes
    else:
        league_codes = list(codes)

    matches = []
    active = []
    for code in league_codes:
        status, body, fetched = fetch(API + f"/leagues/{code}/fixtures/", "application/json", errors, API_GAP)
        save(f"fixtures/{code}.json", body, fetched)
        if status != 200 or not body:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            errors.append({"where": "fixtures_json", "league": code})
            continue
        rows = payload.get("matches") or []
        future = []
        for m in rows:
            ko = kickoff_utc(m.get("match_date") or "", m.get("time") or "")
            if not ko:
                errors.append({"where": "kickoff", "slug": m.get("slug")})
                continue
            if parse_iso(ko) <= now:
                continue
            if m.get("status") not in (None, "", "scheduled"):
                continue
            m = dict(m)
            m["_kickoff_utc"] = ko
            m["_fixtures_sha"] = sha256_bytes(body)
            future.append(m)
        if future:
            active.append(code)
        matches.extend(future)

    with open(os.path.join(run_dir, "active_leagues.json"), "w", encoding="utf-8") as f:
        json.dump(sorted(set(active)), f)

    ledger = read_ledger()
    parsed = []
    overlap_fi = []
    overlap_pin = []
    for m in matches:
        slug = m.get("slug") or ""
        safe = slug.replace("/", "__")
        url = SITE + slug
        status, body, fetched = fetch(url, "text/html", errors, HTML_GAP)
        html_rel = f"html/{safe}.html"
        if body:
            save(html_rel, body, fetched)
        model = ((m.get("model_predictions") or {}).get("dc_v2") or {})
        rec = {
            "run_id": run_id,
            "slug": slug,
            "league": m.get("real_league_name") or m.get("league"),
            "league_code": m.get("league"),
            "home": m.get("home_team"),
            "away": m.get("away_team"),
            "json_match_date": m.get("match_date"),
            "json_time": m.get("time"),
            "kickoff_rule": "json_naive_utc",
            "kickoff_utc": m.get("_kickoff_utc"),
            "status": m.get("status"),
            "model": model.get("model"),
            "computed_at": model.get("computed_at"),
            "fixtures_sha256": m.get("_fixtures_sha"),
            "html_sha256": sha256_bytes(body) if body else None,
            "html_fetched_utc": fetched,
            "source_url": url,
            "triplet": False,
            "discard_reason": None,
            "p1": None,
            "px": None,
            "p2": None,
        }
        if status != 200 or not body:
            rec["discard_reason"] = "html_http_error"
        else:
            tri = explicit_triplet(body.decode("utf-8", "replace"))
            if not tri:
                rec["discard_reason"] = "html_no_explicit_1x2"
            else:
                rec["triplet"] = True
                rec["p1"] = tri["p1"]
                rec["px"] = tri["px"]
                rec["p2"] = tri["p2"]
                rec["labels"] = tri["labels"]
                fi, pin = match_ledger(rec["home"], rec["away"], parse_iso(rec["kickoff_utc"]), ledger)
                if len(fi) == 1:
                    overlap_fi.append({"slug": slug, **fi[0]})
                elif len(fi) > 1:
                    errors.append({"where": "ambiguous_fi", "slug": slug, "n": len(fi)})
                if len(pin) == 1:
                    overlap_pin.append({"slug": slug, **pin[0]})
                elif len(pin) > 1:
                    errors.append({"where": "ambiguous_pin", "slug": slug, "n": len(pin)})
        parsed.append(rec)

    with open(os.path.join(run_dir, "parsed.jsonl"), "w", encoding="utf-8") as f:
        for rec in parsed:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    index_path = os.path.join(PILOT, "index.jsonl")
    with open(index_path, "a", encoding="utf-8") as f:
        for rec in parsed:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    stability = compare_previous(run_id, parsed)
    with open(os.path.join(run_dir, "stability.json"), "w", encoding="utf-8") as f:
        json.dump(stability, f, indent=2)
    both = [x["slug"] for x in overlap_fi if x["slug"] in {y["slug"] for y in overlap_pin}]
    overlap = {
        "footinsights": overlap_fi,
        "pinnacle": overlap_pin,
        "footinsights_and_pinnacle": both,
        "n_footinsights": len(overlap_fi),
        "n_pinnacle": len(overlap_pin),
        "n_both": len(both),
    }
    with open(os.path.join(run_dir, "overlap.json"), "w", encoding="utf-8") as f:
        json.dump(overlap, f, indent=2)
    with open(os.path.join(run_dir, "errors.json"), "w", encoding="utf-8") as f:
        json.dump(errors, f, indent=2)
    manifest = {
        "run_id": run_id,
        "started_utc": iso(now),
        "finished_utc": iso(),
        "scan_mode": mode,
        "kickoff_rule": "JSON match_date + time stored as UTC. HTML CET label is not used.",
        "observed_future": len(parsed),
        "triplet": sum(1 for r in parsed if r["triplet"]),
        "discarded": sum(1 for r in parsed if not r["triplet"]),
        "files": files,
    }
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    summary = {
        "run_id": run_id,
        "scan_mode": mode,
        "observed_future": len(parsed),
        "triplet": manifest["triplet"],
        "discarded": manifest["discarded"],
        "overlap": {k: overlap[k] for k in ("n_footinsights", "n_pinnacle", "n_both")},
        "stability": {
            "compared": stability["compared"],
            "prob_changes": len(stability["probability_changes"]),
            "computed_at_changes": len(stability["computed_at_changes"]),
            "kickoff_changes": len(stability["kickoff_changes"]),
        },
        "errors": len(errors),
    }
    print(json.dumps(summary))
    return summary

def load_parsed(run_id):
    path = os.path.join(PILOT, "runs", run_id, "parsed.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]

def compare_previous(run_id, parsed):
    runs = sorted(d for d in os.listdir(os.path.join(PILOT, "runs")) if os.path.exists(os.path.join(PILOT, "runs", d, "manifest.json")))
    prev = None
    for name in runs:
        if name < run_id:
            prev = name
    out = {
        "previous_run": prev,
        "compared": 0,
        "slug_missing_now": [],
        "probability_changes": [],
        "computed_at_changes": [],
        "kickoff_changes": [],
        "model_changes": [],
    }
    if not prev:
        return out
    old = {r["slug"]: r for r in load_parsed(prev) if r.get("triplet")}
    new = {r["slug"]: r for r in parsed if r.get("triplet")}
    for slug, a in old.items():
        b = new.get(slug)
        if not b:
            out["slug_missing_now"].append(slug)
            continue
        out["compared"] += 1
        if a.get("kickoff_utc") != b.get("kickoff_utc"):
            out["kickoff_changes"].append({"slug": slug, "old": a.get("kickoff_utc"), "new": b.get("kickoff_utc")})
        if (a.get("p1"), a.get("px"), a.get("p2")) != (b.get("p1"), b.get("px"), b.get("p2")):
            out["probability_changes"].append({
                "slug": slug,
                "old": [a.get("p1"), a.get("px"), a.get("p2")],
                "new": [b.get("p1"), b.get("px"), b.get("p2")],
            })
        if a.get("computed_at") != b.get("computed_at"):
            out["computed_at_changes"].append({"slug": slug, "old": a.get("computed_at"), "new": b.get("computed_at")})
        if a.get("model") != b.get("model"):
            out["model_changes"].append({"slug": slug, "old": a.get("model"), "new": b.get("model")})
    return out

def all_runs():
    return complete_run_ids()

def write_final():
    slugs = {}
    discarded = set()
    triplets = set()
    fi = set()
    pin = set()
    both = set()
    prob_changes = []
    computed_changes = []
    errors = []
    for run_id in all_runs():
        for rec in load_parsed(run_id):
            slugs[rec["slug"]] = rec
            if rec.get("triplet"):
                triplets.add(rec["slug"])
            else:
                discarded.add(rec["slug"])
        stab_path = os.path.join(PILOT, "runs", run_id, "stability.json")
        if os.path.exists(stab_path):
            stab = json.load(open(stab_path, encoding="utf-8"))
            for ch in stab.get("probability_changes") or []:
                prob_changes.append({"run_id": run_id, **ch})
            for ch in stab.get("computed_at_changes") or []:
                computed_changes.append({"run_id": run_id, **ch})
        err_path = os.path.join(PILOT, "runs", run_id, "errors.json")
        if os.path.exists(err_path):
            for e in json.load(open(err_path, encoding="utf-8")):
                errors.append({"run_id": run_id, **e})
        ov_path = os.path.join(PILOT, "runs", run_id, "overlap.json")
        if os.path.exists(ov_path):
            ov = json.load(open(ov_path, encoding="utf-8"))
            for x in ov.get("footinsights") or []:
                fi.add(x["slug"])
            for x in ov.get("pinnacle") or []:
                pin.add(x["slug"])
            for slug in ov.get("footinsights_and_pinnacle") or []:
                both.add(slug)
    # a slug discarded in one run but complete in another is not a final discard
    discarded -= triplets
    if prob_changes or computed_changes:
        classification = "continuare staging"
        why = "Le probabilità o computed_at si sono mossi: servono altri snapshot prima di entrare nel ledger."
    elif len(all_runs()) < 2:
        classification = "continuare staging"
        why = "Meno di due run: la stabilità non è ancora misurata."
    elif not triplets:
        classification = "scartare"
        why = "Nessun tripletto 1X2 esplicito."
    else:
        classification = "continuare staging"
        why = "Snapshot stabili finora, ma il pareggio esiste solo nell'HTML e non c'è ancora sovrapposizione col ledger. Non integrare."
    report = {
        "window_started_utc": json.load(open(os.path.join(PILOT, "STARTED.json"), encoding="utf-8"))["started_utc"],
        "written_utc": iso(),
        "runs": len(all_runs()),
        "observed_unique": len(slugs),
        "triplet_unique": len(triplets),
        "discarded_unique": len(discarded),
        "common_footinsights": len(fi),
        "common_pinnacle": len(pin),
        "common_footinsights_and_pinnacle": len(both),
        "probability_changes": prob_changes[:30],
        "probability_change_count": len(prob_changes),
        "computed_at_changes": computed_changes[:30],
        "computed_at_change_count": len(computed_changes),
        "error_count": len(errors),
        "errors_sample": errors[:20],
        "classification": classification,
        "why": why,
    }
    path = os.path.join(PILOT, "FINAL.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(json.dumps({k: report[k] for k in (
        "observed_unique", "triplet_unique", "discarded_unique",
        "common_footinsights", "common_pinnacle", "common_footinsights_and_pinnacle",
        "probability_change_count", "computed_at_change_count", "error_count", "classification",
    )}))
    return report

def ensure_started():
    os.makedirs(PILOT, exist_ok=True)
    path = os.path.join(PILOT, "STARTED.json")
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    now = utcnow()
    payload = {
        "started_utc": iso(now),
        "deadline_utc": iso(now + WINDOW),
        "interval_seconds": INTERVAL,
        "kickoff_rule": "JSON match_date + time = UTC",
        "ledger": "read-only",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return payload

def self_test():
    html = open(os.path.join(ROOT, "raw/page_2026-09-29-boreham-wood-vs-kidderminster.html"), encoding="utf-8").read()
    tri = explicit_triplet(html)
    assert tri and tri["labels"][1] == "Draw", tri
    assert abs(tri["p1"] - 0.537) < 1e-9
    bad = open(os.path.join(ROOT, "raw/page_2026-09-29-hartlepool-vs-harrogate.html"), encoding="utf-8").read()
    assert explicit_triplet(bad) is None
    assert kickoff_utc("2026-09-29", "22:30") == "2026-09-29T22:30:00Z"
    print("SELFTEST_OK")

if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        self_test()
    elif "--final" in sys.argv:
        write_final()
    else:
        ensure_started()
        run_cycle()
