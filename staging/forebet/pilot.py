#!/usr/bin/env python3
"""48-hour Forebet pilot. Staging only. One fixed pilot_id. 16 planned slots.

A retry is another attempt inside the same slot. It is not a new planned snapshot.
The ratio reported at the end is successful slots / 16.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import unicodedata
from datetime import datetime, timedelta, timezone

import extract

ROOT = os.path.dirname(os.path.abspath(__file__))
PILOT = os.path.join(ROOT, "pilot48")
VAR = os.path.join(PILOT, "var")
LEDGER = os.path.abspath(os.path.join(ROOT, "..", "..", "prospective", "prospective_ledger.csv"))
SEAL_ROOTS = [os.path.join(ROOT, "2026-09-29")]
PLANNED = 16
INTERVAL = 3 * 3600
WINDOW = timedelta(hours=48)
MAX_ATTEMPTS = 3
RETRY_SLEEP = (0, 90, 240)
PROXIES = ("it", "it", None)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(dt=None):
    return (dt or utcnow()).strftime("%Y%m%dT%H%M%SZ")


def parse_iso(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_key():
    key = os.environ.get("BROWSER_USE_API_KEY", "").strip()
    if key:
        return key
    path = os.path.join(VAR, "api_key")
    if os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    raise SystemExit("missing BROWSER_USE_API_KEY")


def started_path():
    return os.path.join(PILOT, "STARTED.json")


def slots_path():
    return os.path.join(PILOT, "slots.jsonl")


def attempts_path():
    return os.path.join(PILOT, "attempts.jsonl")


def observations_path():
    return os.path.join(PILOT, "observations.jsonl")


def ensure_started():
    path = started_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    started = utcnow()
    pilot_id = "forebet-48h-" + stamp(started)
    slots = []
    for n in range(1, PLANNED + 1):
        due = started + timedelta(seconds=INTERVAL * (n - 1))
        slots.append({"slot": n, "scheduled_for_utc": iso(due)})
    meta = {
        "pilot_id": pilot_id,
        "started_utc": iso(started),
        "deadline_utc": iso(started + WINDOW),
        "interval_seconds": INTERVAL,
        "planned_slots": PLANNED,
        "slots": slots,
        "ledger": "read-only",
        "agent_used": False,
        "parser": "frozen-unified-span-or-b",
        "ratio_definition": "successful_planned_slots/16; retries stay inside the slot and are not extra snapshots",
    }
    os.makedirs(PILOT, exist_ok=True)
    atomic_write(path, meta)
    atomic_write(os.path.join(PILOT, "seal_at_start.json"), {"pilot_id": pilot_id, "at_utc": iso(started), "files": seal_files()})
    return meta


def seal_files():
    found = []
    targets = [LEDGER]
    for root in SEAL_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _, names in os.walk(root):
            if os.path.basename(dirpath).startswith("20") and "s0" in os.path.basename(dirpath):
                continue
            for name in names:
                if name in {"page.html", "report.json", "fixtures.json", "manifest.json"}:
                    targets.append(os.path.join(dirpath, name))
    for path in targets:
        if os.path.isfile(path):
            found.append({"path": path, "sha256": sha256_file(path)})
    found.sort(key=lambda item: item["path"])
    return found


def verify_seals(meta):
    base = os.path.join(PILOT, "seal_at_start.json")
    if not os.path.exists(base):
        return {"ok": False, "reason": "missing seal_at_start"}
    original = json.load(open(base, encoding="utf-8"))["files"]
    changed = []
    missing = []
    for item in original:
        path = item["path"]
        if not os.path.isfile(path):
            missing.append(path)
            continue
        current = sha256_file(path)
        if current != item["sha256"]:
            changed.append(path)
    manifest_drift = []
    for row in read_jsonl(attempts_path()):
        if row.get("event") != "sealed" or not row.get("out") or not row.get("manifest_sha256"):
            continue
        path = os.path.join(row["out"], "manifest.sha256")
        if not os.path.isfile(path):
            manifest_drift.append(row.get("run_id"))
            continue
        if open(path, encoding="utf-8").read().strip() != row["manifest_sha256"]:
            manifest_drift.append(row.get("run_id"))
    return {
        "ok": not changed and not missing and not manifest_drift,
        "checked": len(original),
        "changed": changed,
        "missing": missing,
        "manifest_drift": manifest_drift,
        "ledger_sha256": sha256_file(LEDGER) if os.path.isfile(LEDGER) else None,
    }


def closed_slots():
    by_slot = {}
    for row in read_jsonl(slots_path()):
        by_slot[row["slot"]] = row
    return [by_slot[n] for n in sorted(by_slot)]


def attempts_for(slot):
    return [row for row in read_jsonl(attempts_path()) if row.get("slot") == slot]


def counter(meta=None):
    meta = meta or ensure_started()
    closed = closed_slots()
    closed_ids = {row["slot"] for row in closed}
    success = [row for row in closed if row.get("status") == "SUCCESS"]
    failed = [row for row in closed if row.get("status") != "SUCCESS"]
    sealed = [row for row in read_jsonl(attempts_path()) if row.get("event") == "sealed"]
    by_slot = {}
    for row in sealed:
        by_slot.setdefault(row["slot"], []).append(row)
    retry_attempts = sum(max(0, len(rows) - 1) for rows in by_slot.values())
    open_slots = [slot for slot in by_slot if slot not in closed_ids]
    return {
        "pilot_id": meta["pilot_id"],
        "planned_slots": PLANNED,
        "successful_slots": len(success),
        "failed_slots": len(failed),
        "open_slot": open_slots[-1] if open_slots else None,
        "not_run_slots": PLANNED - len(closed) - len(open_slots),
        "snapshot_ratio": f"{len(success)}/{PLANNED}",
        "executed_ratio": f"{len(closed)}/{PLANNED}",
        "retry_attempts": retry_attempts,
        "canonical_run_ids": [row.get("canonical_run_id") for row in success],
        "retry_run_ids": [run_id for row in closed for run_id in (row.get("retry_run_ids") or [])],
    }


def next_action(meta, now=None):
    now = now or utcnow()
    deadline = parse_iso(meta["deadline_utc"])
    closed = {row["slot"] for row in closed_slots()}
    open_rows = attempts_for_open()
    if open_rows:
        return {"action": "continue", "slot": open_rows[0]["slot"]}
    pending = [item for item in meta["slots"] if item["slot"] not in closed]
    if not pending:
        if now >= deadline:
            return {"action": "finish"}
        return {"action": "sleep", "sleep_seconds": max(1, int((deadline - now).total_seconds())), "reason": "wait_deadline"}
    nxt = pending[0]
    due = parse_iso(nxt["scheduled_for_utc"])
    if now < due:
        if now >= deadline:
            return {"action": "finish"}
        return {
            "action": "sleep",
            "slot": nxt["slot"],
            "scheduled_for_utc": nxt["scheduled_for_utc"],
            "sleep_seconds": max(1, int((due - now).total_seconds())),
        }
    return {"action": "open", "slot": nxt["slot"], "scheduled_for_utc": nxt["scheduled_for_utc"]}


def attempts_for_open():
    closed = {row["slot"] for row in closed_slots()}
    rows = [row for row in read_jsonl(attempts_path()) if row.get("slot") not in closed]
    if not rows:
        return []
    slot = max(row["slot"] for row in rows)
    return [row for row in rows if row["slot"] == slot]


def money(value):
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def session_cost(report):
    session = report.get("session") or {}
    browser = money(session.get("browserCost"))
    proxy = money(session.get("proxyCost"))
    captcha = money(session.get("captchaCost") or session.get("captcha_cost") or 0)
    return {
        "browser_cost": browser,
        "proxy_cost": proxy,
        "captcha_cost": captcha,
        "proxy_used_mb": money(session.get("proxyUsedMb")),
        "total_cost": round(browser + proxy + captcha, 8),
        "session_id": session.get("id"),
        "duration_seconds": report.get("duration_seconds"),
    }


def side_flags(name):
    text = extract.canon(name)
    flags = set()
    if re.search(r"\b(women|woman|ladies|femenil|femminile|frauen|feminino|femenino)\b", text):
        flags.add("women")
    for label in ("u18", "u19", "u20", "u21", "u23"):
        if re.search(rf"\b{label}\b", text):
            flags.add(label)
    if re.search(r"\b(reserves|reserve)\b", text) or re.search(r"\bb\b", text):
        flags.add("b")
    return flags


def flags_conflict(left, right):
    return side_flags(left) != side_flags(right)


def league_conflict(a, b):
    if not a or not b:
        return False
    ca, cb = extract.canon(a), extract.canon(b)
    if not ca or not cb or ca == cb:
        return False
    ta, tb = set(ca.split()), set(cb.split())
    return not (ta & tb)


def explain_completeness(report, rows):
    xhr = report.get("xhr_audit") or {}
    ids = [row.get("match_id") for row in rows if row.get("match_id")]
    from collections import Counter
    counts = Counter(ids)
    duplicate_ids = sorted(i for i, n in counts.items() if n > 1)
    errors = [row for row in rows if row.get("parse_status") != "OK"]
    error_kinds = {}
    for row in errors:
        for kind in row.get("parse_errors") or ["unknown"]:
            error_kinds[kind] = error_kinds.get(kind, 0) + 1
    dates = {}
    for row in rows:
        text = row.get("kickoff_original") or ""
        day = text.split(" ")[0]
        dates[day] = dates.get(day, 0) + 1
    structural = False
    reason = None
    other_errors = sum(n for kind, n in error_kinds.items() if kind != "triplet")
    if report.get("nav_http") != 200:
        structural = False
    if other_errors > 5 or (rows and sum(1 for row in rows if row.get("home")) < len(rows) * 0.5):
        structural = True
        reason = "parser would have to change; refused"
    missing = xhr.get("xhr_ids_missing_in_dom")
    mismatch = xhr.get("probability_mismatches")
    http_ok = report.get("nav_http") == 200 or report.get("document_recovered") is True
    explained = (
        bool(xhr)
        and missing == 0
        and mismatch == 0
        and not structural
        and http_ok
        and not report.get("challenge_wall")
        and not report.get("captcha")
    )
    declared = report.get("declared_counter")
    xhr_n = xhr.get("xhr_matches")
    badge_gap = None if declared is None or xhr_n is None else declared - xhr_n
    explanation = None
    if explained:
        explanation = (
            f"menu badge={declared}; getrs={xhr_n}; dom={len(rows)}; ok={sum(1 for r in rows if r.get('parse_status')=='OK')}; "
            f"parse_error={len(errors)}; duplicate_ids={duplicate_ids or 'none'}; "
            f"badge_gap={badge_gap} because the badge is not the payload length. "
            "Every getrs id is in the DOM and every parsed triplet matches getrs."
        )
    return {
        "dom_rows": len(rows),
        "ok": sum(1 for row in rows if row.get("parse_status") == "OK"),
        "parse_errors": len(errors),
        "error_kinds": error_kinds,
        "duplicate_ids": duplicate_ids,
        "with_triplet": sum(1 for row in rows if row.get("p1") is not None),
        "without_triplet": sum(1 for row in rows if row.get("p1") is None),
        "with_odds": sum(1 for row in rows if row.get("odds_1")),
        "without_odds": sum(1 for row in rows if row.get("parse_status") == "OK" and not row.get("odds_1")),
        "kickoff_dates": dates,
        "declared_counter": declared,
        "xhr_matches": xhr_n,
        "badge_gap": badge_gap,
        "xhr_ids_missing_in_dom": missing,
        "probability_mismatches": mismatch,
        "dom_ids_not_in_xhr": xhr.get("dom_ids_not_in_xhr"),
        "structural_regression": structural,
        "structural_reason": reason,
        "explained": explained,
        "explanation": explanation,
    }


def timezone_proof(rows):
    ledger = extract.load_ledger_latest()
    charts = load_charts()
    samples = []
    offsets = []
    ambiguous = []
    for row in rows:
        if row.get("parse_status") != "OK":
            continue
        local = extract.parse_kickoff(row.get("kickoff_original"))
        if local is None:
            continue
        local = local.replace(tzinfo=timezone.utc)
        key = (extract.canon(row.get("home")), extract.canon(row.get("away")))
        sources = []
        for src in ledger:
            if (extract.canon(src.get("home_team")), extract.canon(src.get("away_team"))) != key:
                continue
            if flags_conflict(row.get("home"), src.get("home_team")) or flags_conflict(row.get("away"), src.get("away_team")):
                continue
            if league_conflict(row.get("league"), src.get("league")):
                continue
            remote = parse_iso(src.get("kickoff_utc"))
            if remote is None:
                continue
            sources.append(("footinsights", remote, src.get("match_id")))
            pin = parse_iso(src.get("pin_kickoff_utc"))
            if pin is not None and (extract.canon(src.get("pin_home") or ""), extract.canon(src.get("pin_away") or "")) == key:
                sources.append(("pinnacle", pin, src.get("match_id")))
        for src in charts:
            if (extract.canon(src.get("home")), extract.canon(src.get("away"))) != key:
                continue
            if flags_conflict(row.get("home"), src.get("home")) or flags_conflict(row.get("away"), src.get("away")):
                continue
            remote = parse_iso(src.get("kickoff_utc"))
            if remote is not None:
                sources.append(("football_charts", remote, src.get("slug")))
        if not sources:
            continue
        deltas = []
        for name, remote, source_id in sources:
            minutes = round((local - remote).total_seconds() / 60)
            deltas.append((name, minutes, source_id, remote))
        minute_values = {item[1] for item in deltas}
        if len(minute_values) != 1:
            ambiguous.append({"home": row.get("home"), "away": row.get("away"), "deltas": [
                {"source": name, "delta_minutes": minutes} for name, minutes, _, _ in deltas
            ]})
            continue
        minutes = next(iter(minute_values))
        if minutes % 60 != 0:
            ambiguous.append({"home": row.get("home"), "away": row.get("away"), "delta_minutes": minutes})
            continue
        offsets.append(minutes)
        if len(samples) < 12:
            name, minutes, source_id, remote = deltas[0]
            samples.append({
                "home": row.get("home"),
                "away": row.get("away"),
                "kickoff_forebet_raw": row.get("kickoff_original"),
                "timezone_forebet": f"UTC+{minutes // 60}" if minutes % 60 == 0 else None,
                "kickoff_utc": remote.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": name,
                "source_id": source_id,
                "delta_minutes": minutes,
                "sources_agreeing": sorted({item[0] for item in deltas}),
            })
    unique = sorted(set(offsets))
    pool = len(offsets) + len(ambiguous)
    share = (len(ambiguous) / pool) if pool else 1
    resolved = len(offsets) >= 10 and len(unique) == 1 and share < 0.20
    return {
        "compared": len(offsets),
        "ambiguous": len(ambiguous),
        "ambiguous_share": round(share, 4),
        "ambiguous_sample": ambiguous[:6],
        "unique_delta_minutes": unique,
        "offset_hours": (unique[0] // 60) if resolved else None,
        "resolved": resolved,
        "resolution_rule": "at least 10 source-agreeing fixtures, one offset, ambiguous share under 20 percent",
        "samples": samples,
        "fifa_schedule": "not_available",
    }


def apply_pilot_timezone(rows, proof):
    offset = proof.get("offset_hours") if proof.get("resolved") else None
    for row in rows:
        row["timezone_confidence"] = "high" if proof.get("resolved") else "unresolved"
        row["timezone_assumed"] = f"UTC+{offset}" if offset is not None else None
        local = extract.parse_kickoff(row.get("kickoff_original"))
        if offset is not None and local is not None:
            row["kickoff_utc"] = (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            row["kickoff_utc"] = None
    return rows


def leakage_flags(rows, snapshot_ts):
    snap = parse_iso(snapshot_ts)
    flagged = []
    for row in rows:
        kick = parse_iso(row.get("kickoff_utc"))
        state = row.get("row_state")
        valid = bool(kick and snap and snap < kick and state == "SCHEDULED")
        row["valid_prematch"] = valid
        if row.get("parse_status") == "OK" and not valid:
            row["prematch_status"] = "INVALID_PREMATCH"
            flagged.append({
                "match_id": row.get("match_id"),
                "home": row.get("home"),
                "away": row.get("away"),
                "row_state": state,
                "kickoff_utc": row.get("kickoff_utc"),
                "reason": "snapshot_not_before_kickoff" if not (kick and snap and snap < kick) else "not_scheduled",
            })
        elif valid:
            row["prematch_status"] = "VALID_PREMATCH"
        else:
            row["prematch_status"] = "NOT_USED"
    return flagged


def load_charts():
    root = os.path.abspath(os.path.join(ROOT, "..", "football_charts", "pilot", "runs"))
    if not os.path.isdir(root):
        return []
    runs = sorted(name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name)))
    for name in reversed(runs):
        path = os.path.join(root, name, "parsed.jsonl")
        if not os.path.isfile(path):
            continue
        rows = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
        if rows:
            return rows
    return []


def as_prob(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.5:
        number = number / 100.0
    if number < 0 or number > 1:
        return None
    return number


def pick_of(p1, px, p2):
    vals = [p1, px, p2]
    if any(v is None for v in vals):
        return None
    if vals.count(max(vals)) != 1:
        return None
    return "1X2"[vals.index(max(vals))]


def match_sources(rows):
    ledger = extract.load_ledger_latest()
    charts = [row for row in load_charts() if row.get("triplet")]
    fi, pin, fc = [], [], []
    rejected = []
    for row in rows:
        if row.get("parse_status") != "OK" or not row.get("valid_prematch"):
            continue
        kick = parse_iso(row.get("kickoff_utc"))
        key = (extract.canon(row.get("home")), extract.canon(row.get("away")))
        fi_hits, pin_hits, fc_hits = [], [], []
        for src in ledger:
            same_name = (extract.canon(src.get("home_team")), extract.canon(src.get("away_team"))) == key
            pin_name = (extract.canon(src.get("pin_home") or ""), extract.canon(src.get("pin_away") or "")) == key
            if same_name:
                if flags_conflict(row.get("home"), src.get("home_team")) or flags_conflict(row.get("away"), src.get("away_team")) or league_conflict(row.get("league"), src.get("league")):
                    rejected.append({"forebet": row.get("match_id"), "reason": "league_or_category", "source": "footinsights"})
                elif extract.same_kick(src.get("kickoff_utc"), kick):
                    fi_hits.append(src)
            if pin_name and extract.same_kick(src.get("pin_kickoff_utc"), kick):
                if league_conflict(row.get("league"), src.get("pin_league")):
                    rejected.append({"forebet": row.get("match_id"), "reason": "league", "source": "pinnacle"})
                else:
                    pin_hits.append(src)
        for src in charts:
            if flags_conflict(row.get("home"), src.get("home")) or flags_conflict(row.get("away"), src.get("away")):
                continue
            if league_conflict(row.get("league"), src.get("league")):
                continue
            if (extract.canon(src.get("home")), extract.canon(src.get("away"))) == key and extract.same_kick(src.get("kickoff_utc"), kick):
                fc_hits.append(src)
        if len(fi_hits) > 1 or len(pin_hits) > 1 or len(fc_hits) > 1:
            rejected.append({"forebet": row.get("match_id"), "reason": "ambiguous"})
            continue
        item = {"forebet_id": row.get("match_id"), "home": row.get("home"), "away": row.get("away"), "kickoff_utc": row.get("kickoff_utc")}
        if len(fi_hits) == 1:
            src = fi_hits[0]
            fi.append({**item, "match_id": src.get("match_id"), "p1": as_prob(src.get("p_home_raw")), "px": as_prob(src.get("p_draw_raw")), "p2": as_prob(src.get("p_away_raw"))})
        if len(pin_hits) == 1:
            src = pin_hits[0]
            pin.append({**item, "match_id": src.get("match_id"), "p1": as_prob(src.get("fair_home")), "px": as_prob(src.get("fair_draw")), "p2": as_prob(src.get("fair_away"))})
        if len(fc_hits) == 1:
            src = fc_hits[0]
            fc.append({**item, "slug": src.get("slug"), "p1": as_prob(src.get("p1")), "px": as_prob(src.get("px")), "p2": as_prob(src.get("p2"))})
    def ids(rows_):
        return {row["forebet_id"] for row in rows_}
    fi_ids, pin_ids, fc_ids = ids(fi), ids(pin), ids(fc)
    return {
        "forebet_prematch": sum(1 for row in rows if row.get("valid_prematch")),
        "footinsights": len(fi),
        "football_charts": len(fc),
        "pinnacle": len(pin),
        "forebet_fi_pinnacle": len(fi_ids & pin_ids),
        "forebet_fc_pinnacle": len(fc_ids & pin_ids),
        "all_available_models": len(fi_ids & fc_ids & pin_ids),
        "rejected_ambiguous_or_league": len(rejected),
        "joins": {"footinsights": fi, "football_charts": fc, "pinnacle": pin},
    }


def pearson(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    if den == 0:
        return None
    return round(num / den, 4)


def model_agreement(matches):
    """matches: list of {models: {name: (p1,px,p2)}} for FI, FC, Forebet. Pinnacle separate."""
    both = {"1": [], "X": [], "2": []}
    agree3 = 0
    agree2 = 0
    comparable = 0
    dispersions = []
    vs_pin = []
    for match in matches:
        models = match["models"]
        names = [name for name in ("footinsights", "football_charts", "forebet") if models.get(name) and all(v is not None for v in models[name])]
        picks = {name: pick_of(*models[name]) for name in names}
        picks = {name: pick for name, pick in picks.items() if pick}
        if len(picks) == 3:
            comparable += 1
            values = list(picks.values())
            if len(set(values)) == 1:
                agree3 += 1
            elif len(set(values)) == 2:
                agree2 += 1
            for index in range(3):
                column = [models[name][index] for name in ("footinsights", "football_charts", "forebet")]
                dispersions.append(statistics.pstdev(column))
        pin = models.get("pinnacle")
        fb = models.get("forebet")
        if pin and fb and all(v is not None for v in pin + fb):
            vs_pin.append(sum(abs(a - b) for a, b in zip(fb, pin)) / 3)
        for left, right in (("forebet", "footinsights"), ("forebet", "football_charts"), ("forebet", "pinnacle"), ("footinsights", "pinnacle")):
            a, b = models.get(left), models.get(right)
            if not a or not b or any(v is None for v in a + b):
                continue
            both.setdefault(left + "|" + right, {"1": [], "X": [], "2": []})
    # correlations gathered below from the same matches
    pairs = {}
    for left, right in (
        ("forebet", "footinsights"),
        ("forebet", "football_charts"),
        ("forebet", "pinnacle"),
        ("footinsights", "football_charts"),
        ("footinsights", "pinnacle"),
        ("football_charts", "pinnacle"),
    ):
        cols = {"1": [], "X": [], "2": []}
        same = 0
        total = 0
        for match in matches:
            a, b = match["models"].get(left), match["models"].get(right)
            if not a or not b or any(v is None for v in a + b):
                continue
            pa, pb = pick_of(*a), pick_of(*b)
            if pa and pb:
                total += 1
                same += int(pa == pb)
            for label, index in (("1", 0), ("X", 1), ("2", 2)):
                cols[label].append((a[index], b[index]))
        pairs[f"{left}_vs_{right}"] = {
            "n": len(cols["1"]),
            "same_sign": round(same / total, 4) if total else None,
            "same_sign_n": total,
            "corr_p1": pearson([a for a, _ in cols["1"]], [b for _, b in cols["1"]]),
            "corr_px": pearson([a for a, _ in cols["X"]], [b for _, b in cols["X"]]),
            "corr_p2": pearson([a for a, _ in cols["2"]], [b for _, b in cols["2"]]),
        }
    return {
        "three_model_n": comparable,
        "agree_3_of_3": agree3,
        "agree_exactly_2_of_3": agree2,
        "agree_at_least_2_of_3": agree3 + agree2,
        "mean_dispersion": round(sum(dispersions) / len(dispersions), 4) if dispersions else None,
        "mean_abs_diff_forebet_vs_pinnacle": round(sum(vs_pin) / len(vs_pin), 4) if vs_pin else None,
        "pairs": pairs,
    }


def combine_models(matching):
    by_id = {}
    joins = matching["joins"]
    for row in joins["footinsights"]:
        by_id.setdefault(row["forebet_id"], {"models": {}})["models"]["footinsights"] = (row["p1"], row["px"], row["p2"])
    for row in joins["football_charts"]:
        by_id.setdefault(row["forebet_id"], {"models": {}})["models"]["football_charts"] = (row["p1"], row["px"], row["p2"])
    for row in joins["pinnacle"]:
        by_id.setdefault(row["forebet_id"], {"models": {}})["models"]["pinnacle"] = (row["p1"], row["px"], row["p2"])
    # forebet probs are attached by the caller via forebet_index
    return by_id


def record_observations(meta, run_id, rows):
    for row in rows:
        if row.get("parse_status") != "OK" or not row.get("valid_prematch"):
            continue
        if row.get("p1") is None:
            continue
        append_jsonl(observations_path(), {
            "pilot_id": meta["pilot_id"],
            "run_id": run_id,
            "observed_utc": row.get("snapshot_timestamp_utc"),
            "match_id": row.get("match_id"),
            "home": row.get("home"),
            "away": row.get("away"),
            "kickoff_utc": row.get("kickoff_utc"),
            "p1": row.get("p1"),
            "px": row.get("px"),
            "p2": row.get("p2"),
            "valid_prematch": True,
        })


def probability_stability():
    grouped = {}
    for row in read_jsonl(observations_path()):
        grouped.setdefault(row.get("match_id"), []).append(row)
    changed = []
    max_abs = 0
    for match_id, rows in grouped.items():
        rows.sort(key=lambda item: item.get("observed_utc") or "")
        first, last = rows[0], rows[-1]
        deltas = {
            "delta_p1": last["p1"] - first["p1"],
            "delta_px": last["px"] - first["px"],
            "delta_p2": last["p2"] - first["p2"],
        }
        peak = 0
        for index in range(1, len(rows)):
            for key in ("p1", "px", "p2"):
                peak = max(peak, abs(rows[index][key] - rows[index - 1][key]))
        max_abs = max(max_abs, peak, max(abs(v) for v in deltas.values()))
        if any(deltas.values()) or peak:
            kick = parse_iso(last.get("kickoff_utc"))
            seen = parse_iso(last.get("observed_utc"))
            changed.append({
                "match_id": match_id,
                "home": last.get("home"),
                "away": last.get("away"),
                "observations": len(rows),
                "first": {"utc": first.get("observed_utc"), "p1": first["p1"], "px": first["px"], "p2": first["p2"]},
                "last": {"utc": last.get("observed_utc"), "p1": last["p1"], "px": last["px"], "p2": last["p2"]},
                **deltas,
                "max_step": peak,
                "minutes_before_kickoff": int((kick - seen).total_seconds() / 60) if kick and seen else None,
            })
    changed.sort(key=lambda item: -max(abs(item["delta_p1"]), abs(item["delta_px"]), abs(item["delta_p2"])))
    return {
        "matches_observed": len(grouped),
        "matches_with_change": len(changed),
        "max_abs_delta": max_abs,
        "sample": changed[:8],
    }


def cumulative_matching():
    """Last successful snapshot of each match, read from sealed fixtures. No ledger writes."""
    latest = {}
    for slot in closed_slots():
        if slot.get("status") != "SUCCESS":
            continue
        path = slot.get("fixtures_json")
        if not path or not os.path.isfile(path):
            continue
        rows = json.load(open(path, encoding="utf-8"))
        matching = match_sources(rows)
        forebet = {row.get("match_id"): row for row in rows if row.get("valid_prematch") and row.get("parse_status") == "OK"}
        combined = combine_models(matching)
        for forebet_id, payload in combined.items():
            src = forebet.get(forebet_id)
            if not src:
                continue
            payload["models"]["forebet"] = (as_prob(src.get("p1")), as_prob(src.get("px")), as_prob(src.get("p2")))
            latest[forebet_id] = payload
        slot_counts = {k: matching[k] for k in matching if k != "joins"}
        slot["matching_counts"] = slot_counts
    agreement = model_agreement(list(latest.values()))
    return agreement


def cost_summary():
    attempts = [row for row in read_jsonl(attempts_path()) if row.get("event") == "sealed"]
    total = round(sum(money(row.get("total_cost")) for row in attempts), 8)
    success_slots = [row for row in closed_slots() if row.get("status") == "SUCCESS"]
    success_cost = [money(row.get("slot_cost")) for row in success_slots]
    mean = (sum(success_cost) / len(success_cost)) if success_cost else None
    fixtures = sum(int(row.get("ok") or 0) for row in success_slots)
    per_100 = (mean * 100 / (fixtures / len(success_slots))) if mean and fixtures else None
    daily = mean * (24 * 3600 / INTERVAL) if mean is not None else None
    monthly = daily * 30 if daily is not None else None
    return {
        "attempts": len(attempts),
        "total_cost_including_retries": total,
        "mean_cost_per_successful_slot": None if mean is None else round(mean, 8),
        "valid_fixtures_on_success_slots": fixtures,
        "mean_cost_per_100_fixtures": None if per_100 is None else round(per_100, 8),
        "estimated_daily_8_runs": None if daily is None else round(daily, 6),
        "estimated_monthly_every_3h": None if monthly is None else round(monthly, 4),
        "note": "estimates use the mean of successful slots only; retries are in the total, not in the monthly run rate",
    }


def write_manifest(out, meta, slot, attempt, report, completeness, proof, leakage, matching_counts):
    files = {}
    for name in sorted(os.listdir(out)):
        path = os.path.join(out, name)
        if os.path.isfile(path) and name not in {"manifest.json", "manifest.sha256"}:
            files[name] = {"sha256": sha256_file(path), "bytes": os.path.getsize(path)}
    manifest = {
        "pilot_id": meta["pilot_id"],
        "slot": slot,
        "attempt": attempt,
        "planned_slots": PLANNED,
        "run_id": os.path.basename(out),
        "sealed_utc": iso(),
        "snapshot_timestamp_utc": report.get("started_utc"),
        "agent_used": False,
        "session_id": (report.get("session") or {}).get("id"),
        "duration_seconds": report.get("duration_seconds"),
        "cost": session_cost(report),
        "nav_http": report.get("nav_http"),
        "final_url": report.get("final_url"),
        "cf_ray": report.get("cf_ray"),
        "captcha": report.get("captcha"),
        "challenge_wall": report.get("challenge_wall"),
        "redirected": report.get("redirected"),
        "completeness": completeness,
        "timezone": {k: proof[k] for k in proof if k != "samples"},
        "timezone_samples": proof.get("samples"),
        "invalid_prematch": leakage,
        "matching": matching_counts,
        "files": files,
    }
    path = os.path.join(out, "manifest.json")
    atomic_write(path, manifest)
    digest = sha256_file(path)
    with open(os.path.join(out, "manifest.sha256"), "w", encoding="utf-8") as fh:
        fh.write(digest + "\n")
    return digest


def attempt_dir(when, slot, attempt):
    run_id = f"{stamp(when)}-s{slot:02d}a{attempt}"
    day = when.strftime("%Y-%m-%d")
    out = os.path.join(ROOT, day, run_id)
    if os.path.exists(out):
        raise SystemExit(f"refusing to reuse {out}")
    return run_id, out


def run_attempt(meta, slot, attempt, proxy):
    when = utcnow()
    run_id, out = attempt_dir(when, slot, attempt)
    append_jsonl(attempts_path(), {
        "event": "started",
        "pilot_id": meta["pilot_id"],
        "slot": slot,
        "attempt": attempt,
        "run_id": run_id,
        "proxy": proxy,
        "started_utc": iso(when),
        "counts_as_planned_snapshot": False,
    })
    key = load_key()
    try:
        report = extract.run_browser(key, out, proxy_country=proxy)
    except Exception as exc:
        report = {"started_utc": iso(when), "playwright_error": type(exc).__name__ + ": " + str(exc)[:400], "agent_used": False}
    rows = report.get("rows") or []
    if rows and not report.get("playwright_error"):
        proof = timezone_proof(rows)
        rows = apply_pilot_timezone(rows, proof)
        for row in rows:
            row["snapshot_timestamp_utc"] = report.get("started_utc")
            row["run_id"] = run_id
            row["pilot_id"] = meta["pilot_id"]
            row["slot"] = slot
            row["attempt"] = attempt
        leakage = leakage_flags(rows, report.get("started_utc"))
        for row in rows:
            row.pop("raw", None)
        extract.write_normalized(out, run_id, report.get("started_utc"), rows)
        import csv as _csv
        extra = ["pilot_id", "slot", "attempt", "valid_prematch", "prematch_status"]
        csv_path = os.path.join(out, "fixtures.csv")
        with open(csv_path, newline="", encoding="utf-8") as fh:
            existing = list(_csv.DictReader(fh))
        if existing:
            fields = list(existing[0].keys()) + [name for name in extra if name not in existing[0]]
            by_id = {row.get("match_id"): row for row in rows}
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                writer = _csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for item in existing:
                    src = by_id.get(item.get("match_id")) or {}
                    for name in extra:
                        item[name] = src.get(name)
                    writer.writerow(item)
        json.dump(rows, open(os.path.join(out, "fixtures.json"), "w"), ensure_ascii=False)
    else:
        proof = {"resolved": False, "compared": 0, "samples": [], "ambiguous": 0, "unique_delta_minutes": []}
        leakage = []
    completeness = explain_completeness(report, rows)
    report["pilot_id"] = meta["pilot_id"]
    report["slot"] = slot
    report["attempt"] = attempt
    report["completeness"] = completeness
    report["timezone_proof"] = {k: proof[k] for k in proof if k != "samples"}
    report["rows"] = [{k: v for k, v in row.items() if k != "raw"} for row in rows]
    slim = {k: v for k, v in report.items() if k != "rows"}
    atomic_write(os.path.join(out, "report.json"), slim)
    matching = match_sources(rows) if rows else {"forebet_prematch": 0, "footinsights": 0, "football_charts": 0, "pinnacle": 0, "forebet_fi_pinnacle": 0, "forebet_fc_pinnacle": 0, "all_available_models": 0, "rejected_ambiguous_or_league": 0, "joins": {"footinsights": [], "football_charts": [], "pinnacle": []}}
    matching_counts = {k: matching[k] for k in matching if k != "joins"}
    if completeness.get("explained") and proof.get("resolved"):
        by_id = combine_models(matching)
        forebet = {row.get("match_id"): row for row in rows if row.get("valid_prematch")}
        for forebet_id, payload in by_id.items():
            src = forebet.get(forebet_id)
            if src:
                payload["models"]["forebet"] = (as_prob(src.get("p1")), as_prob(src.get("px")), as_prob(src.get("p2")))
        atomic_write(os.path.join(out, "agreement.json"), model_agreement(list(by_id.values())))
    digest = write_manifest(out, meta, slot, attempt, report, completeness, proof, leakage, matching_counts)
    success = bool(
        completeness.get("explained")
        and not completeness.get("structural_regression")
        and (report.get("nav_http") == 200 or report.get("document_recovered") is True)
    )
    if success:
        record_observations(meta, run_id, rows)
    sealed = {
        "event": "sealed",
        "pilot_id": meta["pilot_id"],
        "slot": slot,
        "attempt": attempt,
        "run_id": run_id,
        "out": out,
        "status": "SUCCESS" if success else "FAILED",
        "counts_as_planned_snapshot": False,
        "failure": None if success else (completeness.get("structural_reason") or report.get("playwright_error") or report.get("create_error") or "completeness_not_explained"),
        "structural_regression": completeness.get("structural_regression"),
        "nav_http": report.get("nav_http"),
        "captcha": report.get("captcha"),
        "challenge_wall": report.get("challenge_wall"),
        "cf_ray": report.get("cf_ray"),
        "ok": completeness.get("ok"),
        "parse_errors": completeness.get("parse_errors"),
        "manifest_sha256": digest,
        **session_cost(report),
    }
    append_jsonl(attempts_path(), sealed)
    return sealed


def close_slot(meta, slot, scheduled_for):
    sealed = [row for row in attempts_for(slot) if row.get("event") == "sealed"]
    success = [row for row in sealed if row.get("status") == "SUCCESS"]
    canonical = success[-1] if success else (sealed[-1] if sealed else None)
    retries = [row["run_id"] for row in sealed if not canonical or row["run_id"] != canonical["run_id"]]
    status = "SUCCESS" if success else "FAILED"
    record = {
        "pilot_id": meta["pilot_id"],
        "slot": slot,
        "planned_slots": PLANNED,
        "scheduled_for_utc": scheduled_for,
        "closed_utc": iso(),
        "status": status,
        "attempts": len(sealed),
        "canonical_run_id": None if not canonical else canonical["run_id"],
        "fixtures_json": None if not canonical else os.path.join(canonical["out"], "fixtures.json"),
        "retry_run_ids": retries,
        "slot_cost": round(sum(money(row.get("total_cost")) for row in sealed), 8),
        "ok": None if not canonical else canonical.get("ok"),
        "parse_errors": None if not canonical else canonical.get("parse_errors"),
        "counts_as_one_planned_slot": True,
    }
    success_n = sum(1 for row in closed_slots() if row.get("status") == "SUCCESS") + (1 if status == "SUCCESS" else 0)
    record["snapshot_ratio_after_close"] = f"{success_n}/{PLANNED}"
    append_jsonl(slots_path(), record)
    atomic_write(os.path.join(PILOT, "status.json"), build_status(meta))
    return record


def build_status(meta):
    info = counter(meta)
    cloudflare = [row for row in read_jsonl(attempts_path()) if row.get("event") == "sealed" and (row.get("captcha") or row.get("challenge_wall") or row.get("nav_http") not in (200, None))]
    return {
        "pilot_id": info["pilot_id"],
        "status": "IN_PROGRESS",
        "deadline_utc": meta["deadline_utc"],
        "snapshot_ratio": info["snapshot_ratio"],
        "executed_ratio": info["executed_ratio"],
        "successful_slots": info["successful_slots"],
        "failed_slots": info["failed_slots"],
        "retry_attempts": info["retry_attempts"],
        "retry_run_ids": info["retry_run_ids"],
        "canonical_run_ids": info["canonical_run_ids"],
        "open_slot": info["open_slot"],
        "probability": probability_stability(),
        "costs": cost_summary(),
        "seals": verify_seals(meta),
        "cloudflare_or_http_failures": len(cloudflare),
        "classification": "IN_PROGRESS",
    }


def run_due_slot(meta):
    action = next_action(meta)
    if action["action"] == "sleep":
        return action
    if action["action"] == "finish":
        return action
    slot = action["slot"]
    scheduled = action.get("scheduled_for_utc")
    if scheduled is None:
        scheduled = next(item["scheduled_for_utc"] for item in meta["slots"] if item["slot"] == slot)
    done = [row for row in attempts_for(slot) if row.get("event") == "sealed"]
    if any(row.get("status") == "SUCCESS" for row in done):
        close_slot(meta, slot, scheduled)
        return {"action": "closed", "slot": slot, "snapshot_ratio": counter(meta)["snapshot_ratio"]}
    if any(row.get("structural_regression") for row in done):
        close_slot(meta, slot, scheduled)
        return {"action": "closed_structural", "slot": slot, "snapshot_ratio": counter(meta)["snapshot_ratio"]}
    attempt = len(done) + 1
    if attempt > MAX_ATTEMPTS:
        close_slot(meta, slot, scheduled)
        return {"action": "closed_failed", "slot": slot, "snapshot_ratio": counter(meta)["snapshot_ratio"]}
    proxy = PROXIES[min(attempt - 1, len(PROXIES) - 1)]
    if attempt > 1:
        import time
        time.sleep(RETRY_SLEEP[min(attempt - 1, len(RETRY_SLEEP) - 1)])
    sealed = run_attempt(meta, slot, attempt, proxy)
    if sealed["status"] == "SUCCESS" or sealed.get("structural_regression") or attempt >= MAX_ATTEMPTS:
        close_slot(meta, slot, scheduled)
    atomic_write(os.path.join(PILOT, "status.json"), build_status(meta))
    return {
        "action": "attempt",
        "slot": slot,
        "attempt": attempt,
        "attempt_status": sealed["status"],
        "run_id": sealed["run_id"],
        "snapshot_ratio": counter(meta)["snapshot_ratio"],
        "retry": attempt > 1,
    }


def classify(meta):
    info = counter(meta)
    status = build_status(meta)
    seals = status["seals"]
    success_slots = [row for row in closed_slots() if row.get("status") == "SUCCESS"]
    structural = any(row.get("structural_regression") for row in read_jsonl(attempts_path()))
    tz_fail = 0
    tz_ok = 0
    for slot in success_slots:
        manifest = os.path.join(os.path.dirname(slot["fixtures_json"] or ""), "manifest.json") if slot.get("fixtures_json") else ""
        # manifest sits next to fixtures
        if slot.get("fixtures_json"):
            manifest = os.path.join(os.path.dirname(slot["fixtures_json"]), "manifest.json")
            if os.path.isfile(manifest):
                body = json.load(open(manifest, encoding="utf-8"))
                if body.get("timezone", {}).get("resolved"):
                    tz_ok += 1
                else:
                    tz_fail += 1
    captcha_slots = sum(1 for row in success_slots if False)
    http_fail = status["cloudflare_or_http_failures"]
    reasons = []
    if not seals.get("ok"):
        reasons.append("sealed files changed")
    if structural:
        reasons.append("structural regression")
    if info["successful_slots"] < 12:
        reasons.append("too many missed slots")
    if tz_ok < max(1, info["successful_slots"] // 2):
        reasons.append("timezone unresolved")
    if http_fail > info["successful_slots"]:
        reasons.append("access unstable")
    label = "CONTINUARE STAGING"
    if reasons and ("sealed files changed" in reasons or "structural regression" in reasons or "timezone unresolved" in reasons):
        label = "SCARTARE"
    elif not reasons and info["successful_slots"] >= 14 and tz_fail == 0 and seals.get("ok") and not structural:
        label = "INTEGRARE"
    return {
        "classification": label,
        "reasons": reasons,
        "timezone_resolved_slots": tz_ok,
        "timezone_unresolved_slots": tz_fail,
        "not_integrated": True,
    }


def write_final(meta):
    final = build_status(meta)
    final["status"] = "FINISHED"
    final["classification_detail"] = classify(meta)
    final["classification"] = final["classification_detail"]["classification"]
    final["finished_utc"] = iso()
    final["agreement"] = cumulative_matching()
    atomic_write(os.path.join(PILOT, "FINAL.json"), final)
    atomic_write(os.path.join(PILOT, "status.json"), final)
    return final
