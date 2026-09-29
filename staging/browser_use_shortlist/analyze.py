#!/usr/bin/env python3
"""Offline parse of saved shortlist HTML. Does not open a browser and does not write the ledger."""
from __future__ import annotations

import csv
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from html import unescape

ROOT = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.abspath(os.path.join(ROOT, "..", "..", "prospective", "prospective_ledger.csv"))
SEAL = os.path.join(ROOT, "seal_before_full.json")


def canon(name):
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    stop = {"fc", "cf", "sc", "ac", "afc", "fk", "sk", "the"}
    return " ".join(part for part in text.split() if part not in stop)


def flags(name):
    text = canon(name)
    found = set()
    if re.search(r"\b(women|woman|ladies|w)\b", text):
        found.add("women")
    for label in ("u18", "u19", "u20", "u21", "u23"):
        if re.search(rf"\b{label}\b", text):
            found.add(label)
    return found


def text_of(html):
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def read(path):
    return open(path, encoding="utf-8", errors="replace").read()


def parse_elofoot(path):
    text = text_of(read(path))
    rows = []
    pattern = re.compile(
        r"(\d{2}:\d{2}) UK/Lagos (.+?) (\d+\s+[–-]\s+\d+) (.+?) Predicted score · (\d+)% probability 1 (\d+) % X (\d+) % 2 (\d+) %"
    )
    for match in pattern.finditer(text):
        clock, home, score, away, score_p, p1, px, p2 = match.groups()
        rows.append({
            "home": home.strip(),
            "away": away.strip(),
            "kickoff_raw": clock + " UK/Lagos",
            "timezone_label": "UK time (page also says Lagos/WAT)",
            "correct_score": re.sub(r"\s+", "", score),
            "correct_score_probability": score_p + "%",
            "p1": int(p1),
            "px": int(px),
            "p2": int(p2),
            "triplet": True,
        })
    return rows


def parse_betstudy(path):
    html = read(path)
    rows = []
    for row in re.findall(r"<tr>\s*<td class=\"time\">(.*?)</tr>", html, re.S):
        clock = re.search(r"<span>(\d{2}/\d{2})</span><span>(\d{2}:\d{2})</span>", row)
        teams = re.search(r'href="/prediction/[^"]+">([^<]+)<br>([^<]+)</a>', row)
        probs = re.findall(r"<td>(\d+)%</td>", row)
        if not clock or not teams or len(probs) < 3:
            continue
        rows.append({
            "home": teams.group(1).strip(),
            "away": teams.group(2).strip(),
            "kickoff_raw": f"{clock.group(1)} {clock.group(2)}",
            "timezone_label": "not labeled on the row; compared later with the anchor",
            "p1": int(probs[0]),
            "px": int(probs[1]),
            "p2": int(probs[2]),
            "triplet": True,
        })
    return rows


def parse_dratings(path):
    html = read(path)
    rows = []
    for row in re.findall(r"<tr[\s\S]*?</tr>", html):
        when = re.search(r'datetime="([^"]+)"', row)
        teams = re.findall(r'class="table-cell--mw d--ib lh--12">\s*([^<]+?)\s*<span', row)
        wins = re.findall(r'class="tc--(?:red|green)">(\d+\.\d+)%</span>', row)
        draw = re.search(r'class="tc--blackT">(\d+\.\d+)%</td>', row)
        if not when or len(teams) < 2 or len(wins) < 2 or not draw:
            continue
        rows.append({
            "home": teams[0].strip(),
            "away": teams[1].strip(),
            "kickoff_raw": when.group(1),
            "kickoff_utc": when.group(1).replace("+00:00", "Z"),
            "timezone_label": "datetime attribute UTC",
            "p1": float(wins[0]),
            "p2": float(wins[1]),
            "px": float(draw.group(1)),
            "triplet": True,
            "note": "win cell lists home then away; the next cell is the draw",
        })
    return rows


def parse_predictlix(path):
    html = read(path)
    rows = []
    for cls, row in re.findall(r'<tr class="(match-row[^"]*)">(.*?)</tr>', html, re.S):
        if "upcoming" not in cls:
            continue
        def grab(pat):
            found = re.search(pat, row)
            return found.group(1).strip() if found else None
        rows.append({
            "home": grab(r'class="home-team">([^<]+)'),
            "away": grab(r'class="away-team">([^<]+)'),
            "kickoff_raw": grab(r'class="time-cell">([^<]+)'),
            "timezone_label": "page header says UTC+3",
            "confidence": grab(r'confidence-badge[^>]*>([^<]+)'),
            "pick_probability": grab(r'prob-badge[^>]*>([^<]+)'),
            "pick": grab(r'prediction-value[^>]*>\s*([^<]+)'),
            "odds": grab(r'data-odd="([^"]+)"'),
            "p1": None,
            "px": None,
            "p2": None,
            "triplet": False,
        })
    return rows


def parse_statsultra(path):
    text = text_of(read(path))
    rows = []
    pattern = re.compile(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{1,2} [A-Za-z]{3}) ([A-Z]{2,4}) ([A-Za-z .'\-]+?) (\d+)% ([A-Z]{2,4}) ([A-Za-z .'\-]+?) (\d+)% Draw (\d+)%"
    )
    for match in pattern.finditer(text):
        day, date, _c1, home, p1, _c2, away, p2, px = match.groups()
        rows.append({
            "home": home.strip(),
            "away": away.strip(),
            "kickoff_raw": f"{day} {date}",
            "timezone_label": "date only, no clock on the card",
            "p1": int(p1),
            "px": int(px),
            "p2": int(p2),
            "triplet": True,
        })
    return rows


def parse_prosoccer(path):
    text = text_of(read(path))
    rows = []
    # Rendered card repeats the triplet. Keep the labeled form: 1 nn% X nn% 2 nn%
    pattern = re.compile(r"(FT|HT|\d{1,2}:\d{2})?\s*([A-Za-z][^%]{1,40}?)\s+1\s+(\d+)%\s+X\s+(\d+)%\s+2\s+(\d+)%")
    seen = set()
    for match in pattern.finditer(text):
        state, home, p1, px, p2 = match.groups()
        key = (home.strip(), p1, px, p2)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "state": state,
            "home_blob": home.strip(),
            "p1": int(p1),
            "px": int(px),
            "p2": int(p2),
            "triplet": True,
            "future": state not in {"FT", "HT"} and state is not None,
        })
    return rows


def load_anchor():
    latest = {}
    with open(LEDGER, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            prev = latest.get(row["match_id"])
            if prev is None or row["snapshot_timestamp_utc"] > prev["snapshot_timestamp_utc"]:
                latest[row["match_id"]] = row
    out = []
    for row in latest.values():
        kick = row.get("kickoff_utc") or ""
        if kick < "2026-09-29T15:50:00":
            continue
        if (row.get("match_status") or "").upper() in {"FT", "AET", "FINISHED"}:
            continue
        out.append(row)
    return out


def kick_dt(text):
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def apply_offset(raw, hours):
    # raw like 29/09 16:00 or 19:00
    match = re.search(r"(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", raw or "")
    if match:
        day, month, hh, mm = map(int, match.groups())
        local = datetime(2026, month, day, hh, mm, tzinfo=timezone.utc)
        return local - timedelta(hours=hours)
    match = re.search(r"(\d{2}):(\d{2})", raw or "")
    if match and "UK" in (raw or ""):
        hh, mm = map(int, match.groups())
        local = datetime(2026, 9, 29, hh, mm, tzinfo=timezone.utc)
        return local - timedelta(hours=hours)
    match = re.search(r"^(\d{2}):(\d{2})$", raw or "")
    if match:
        hh, mm = map(int, match.groups())
        local = datetime(2026, 9, 29, hh, mm, tzinfo=timezone.utc)
        return local - timedelta(hours=hours)
    return None


def match_anchor(rows, anchor, offset_hours):
    found, ambiguous, unresolved = [], [], []
    for row in rows:
        if row.get("kickoff_utc"):
            kick = kick_dt(row["kickoff_utc"])
        elif offset_hours is None:
            unresolved.append(row.get("home"))
            continue
        else:
            kick = apply_offset(row.get("kickoff_raw"), offset_hours)
        if kick is None or not row.get("home") or not row.get("away"):
            unresolved.append(row.get("home"))
            continue
        hits = []
        for src in anchor:
            if flags(row["home"]) != flags(src["home_team"]) or flags(row["away"]) != flags(src["away_team"]):
                continue
            if canon(row["home"]) != canon(src["home_team"]) or canon(row["away"]) != canon(src["away_team"]):
                continue
            remote = kick_dt(src["kickoff_utc"])
            if remote and abs((kick - remote).total_seconds()) <= 1800:
                hits.append(src)
        if len(hits) == 1:
            found.append((row, hits[0]))
        elif len(hits) > 1:
            ambiguous.append(row.get("home"))
    return found, ambiguous, unresolved


def money(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def costs():
    sessions = []
    for dirpath, _, names in os.walk(ROOT):
        if "report.json" not in names:
            continue
        report = json.load(open(os.path.join(dirpath, "report.json"), encoding="utf-8"))
        session = report.get("session") or {}
        if not session.get("id"):
            continue
        sessions.append({
            "dir": dirpath.replace(ROOT + "/", ""),
            "session_id": session.get("id"),
            "browser_cost": money(session.get("browserCost")),
            "proxy_cost": money(session.get("proxyCost")),
            "captcha_cost": money(session.get("captchaCost") or 0),
            "proxy_mb": money(session.get("proxyUsedMb")),
            "duration_seconds": report.get("duration_seconds"),
        })
    total = sum(item["browser_cost"] + item["proxy_cost"] + item["captcha_cost"] for item in sessions)
    mean = total / len(sessions) if sessions else None
    return {
        "sessions": len(sessions),
        "total_cost": round(total, 6),
        "mean_cost_per_session": None if mean is None else round(mean, 6),
        "proxy_mb": round(sum(item["proxy_mb"] for item in sessions), 4),
        "captcha_sessions": sum(1 for item in sessions if item["captcha_cost"]),
        "estimated_daily_one_session_every_3h": None if mean is None else round(mean * 8, 6),
        "estimated_monthly_one_session_every_3h": None if mean is None else round(mean * 8 * 30, 4),
        "note": "every observed session billed the same browser minimum; captcha solver was off",
    }


def verify_seal():
    original = json.load(open(SEAL, encoding="utf-8"))["files"]
    changed = []
    for item in original:
        if not os.path.isfile(item["path"]):
            changed.append(item["path"] + " MISSING")
            continue
        import hashlib
        digest = hashlib.sha256(open(item["path"], "rb").read()).hexdigest()
        if digest != item["sha256"]:
            changed.append(item["path"])
    return {"checked": len(original), "changed": changed, "ok": not changed}


def delta(a_rows, b_rows):
    def key(row):
        return (canon(row.get("home") or ""), canon(row.get("away") or ""))
    am = {key(row): row for row in a_rows if row.get("home") and row.get("away")}
    bm = {key(row): row for row in b_rows if row.get("home") and row.get("away")}
    common = set(am) & set(bm)
    changes = []
    for item in common:
        if not am[item].get("triplet") or not bm[item].get("triplet"):
            if am[item].get("pick_probability") != bm[item].get("pick_probability"):
                changes.append({"match": item, "first": am[item].get("pick_probability"), "second": bm[item].get("pick_probability")})
            continue
        d1 = am[item]["p1"] - bm[item]["p1"]
        dx = am[item]["px"] - bm[item]["px"]
        d2 = am[item]["p2"] - bm[item]["p2"]
        if d1 or dx or d2:
            changes.append({"match": item, "delta_p1": d1, "delta_px": dx, "delta_p2": d2})
    return {"common": len(common), "changed": len(changes), "sample": changes[:5]}


def main():
    elofoot_a = parse_elofoot(os.path.join(ROOT, "elofoot/20260929T155323Z-p2/page.html"))
    elofoot_b = parse_elofoot(os.path.join(ROOT, "elofoot/20260929T155700Z-r2/page.html"))
    bet_a = parse_betstudy(os.path.join(ROOT, "betstudy/20260929T155323Z-p2/page.html"))
    bet_b = parse_betstudy(os.path.join(ROOT, "betstudy/20260929T155700Z-r2/page.html"))
    dra_a = parse_dratings(os.path.join(ROOT, "dratings/20260929T155323Z-p2/page.html"))
    dra_b = parse_dratings(os.path.join(ROOT, "dratings/20260929T155700Z-r2/page.html"))
    pred_a = parse_predictlix(os.path.join(ROOT, "predictlix/20260929T155049Z/page.html"))
    pred_b = parse_predictlix(os.path.join(ROOT, "predictlix/20260929T155700Z-r2/page.html"))
    ultra_a = parse_statsultra(os.path.join(ROOT, "statsultra/20260929T155049Z/page.html"))
    ultra_b = parse_statsultra(os.path.join(ROOT, "statsultra/20260929T155700Z-r2/page.html"))
    pro = parse_prosoccer(os.path.join(ROOT, "prosoccer/20260929T155900Z-upcoming/page.html"))
    anchor = load_anchor()
    # Offsets checked against the anchor, not assumed.
    # Elofoot page says UK time. 2026-09-29 UK civil time is UTC+1. BetStudy rows have no label.
    report = {
        "anchor_future_matches": len(anchor),
        "elofoot": {"n": len(elofoot_a), "repeat": delta(elofoot_a, elofoot_b), "sample": elofoot_a[:3]},
        "betstudy": {"n": len(bet_a), "repeat": delta(bet_a, bet_b), "sample": bet_a[:3]},
        "dratings": {"n": len(dra_a), "repeat": delta(dra_a, dra_b), "sample": dra_a[:2]},
        "predictlix": {"n": len(pred_a), "triplet": False, "repeat": delta(pred_a, pred_b), "sample": pred_a[:2]},
        "statsultra": {"n": len(ultra_a), "repeat": delta(ultra_a, ultra_b), "sample": ultra_a[:3]},
        "prosoccer_cards": len(pro),
        "prosoccer_future_flag": sum(1 for row in pro if row.get("future")),
        "costs": costs(),
        "seal": verify_seal(),
    }
    for name, rows, offset in (
        ("elofoot_uk_plus1", elofoot_a, 1),
        ("betstudy_as_utc", bet_a, 0),
        ("betstudy_as_uk", bet_a, 1),
        ("predictlix_utc_plus3", pred_a, 3),
    ):
        # predictlix kickoff_raw is HH:MM only
        found, ambiguous, unresolved = match_anchor(rows, anchor, offset)
        report[name] = {"found": len(found), "ambiguous": len(ambiguous), "unresolved": len(unresolved), "examples": [
            {"home": row["home"], "away": row["away"], "anchor": src["match_id"], "anchor_kick": src["kickoff_utc"]}
            for row, src in found[:5]
        ]}
    found, ambiguous, unresolved = match_anchor(dra_a, anchor, None)
    report["dratings_utc_attr"] = {"found": len(found), "ambiguous": len(ambiguous), "unresolved": len(unresolved)}
    json.dump(report, open(os.path.join(ROOT, "REPORT.json"), "w"), indent=2, ensure_ascii=False, default=str)
    print(json.dumps({k: report[k] for k in report if k not in {"elofoot", "betstudy", "dratings", "predictlix", "statsultra"}}, indent=2)[:4000])
    for name in ("elofoot", "betstudy", "dratings", "predictlix", "statsultra"):
        item = report[name]
        print(name, "n", item["n"], "repeat", item["repeat"])


if __name__ == "__main__":
    main()
