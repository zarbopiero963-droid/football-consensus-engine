#!/usr/bin/env python3
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import connect

BROWSER_API = "https://api.browser-use.com/api/v4/browsers"
ROME = ZoneInfo("Europe/Rome")
LONDON = ZoneInfo("Europe/London")
PILOT_ID = os.environ.get("SOURCE_PILOT_ID", "source-pilot-48h-issue1-v1")
SOURCES = {
    "prosoccer": "https://prosoccer.eu/predictions/today",
    "statsultra": "https://statsultra.com/",
    "elofoot": "https://www.elofoot.com/en",
}


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def browser_api(method: str, url: str, body=None):
    key = os.environ["BROWSER_USE_API_KEY"]
    raw = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=raw, method=method)
    req.add_header("X-Browser-Use-API-Key", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, {"error": text[:500]}


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def canon(s: str):
    s = (s or "").lower()
    s = re.sub(r"\b(fc|cf|afc|sc|the)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def parse_prosoccer(text: str):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    i = 0
    while i < len(lines):
        if re.fullmatch(r"\d{1,2}:\d{2}", lines[i]):
            clock = lines[i]
            block = []
            j = i + 1
            while j < len(lines) and not re.fullmatch(r"(?:\d{1,2}:\d{2}|FT|HT)", lines[j]):
                block.append(lines[j])
                j += 1
            pct = [float(x[:-1]) for x in block if re.fullmatch(r"\d+(?:\.\d+)?%", x)]
            names = [x for x in block if not re.fullmatch(r"\d+(?:\.\d+)?%", x) and not re.fullmatch(r"\d+", x)]
            if len(names) >= 3 and len(pct) >= 5:
                home, away, league = names[0], names[1], names[2]
                out.append({
                    "home_team": home, "away_team": away, "league": league,
                    "kickoff_raw": clock, "source_timezone": "Europe/Rome",
                    "timezone_status": "VERIFIED_BROWSER_TZ",
                    "p1": pct[0], "px": pct[1], "p2": pct[2],
                    "over25": pct[3], "under25": pct[4],
                })
            i = j
        else:
            i += 1
    return out


def parse_statsultra(text: str):
    flat = re.sub(r"\s+", " ", text)
    # Example structure:
    # ELD Eldense 49.7 V OVI Real Oviedo 61.3 28% ELD 30% DRAW 42% OVI
    pattern = re.compile(
        r"\b([A-Z]{2,4})\s+([A-Za-zÀ-ÿ .'-]+?)\s+\d+(?:\.\d+)?\s+V\s+"
        r"([A-Z]{2,4})\s+([A-Za-zÀ-ÿ .'-]+?)\s+\d+(?:\.\d+)?\s+"
        r"(\d+)%\s+\1\s+(\d+)%\s+DRAW\s+(\d+)%\s+\3"
    )
    out = []
    for m in pattern.finditer(flat):
        c1, home, c2, away, p1, px, p2 = m.groups()
        tail = flat[m.end():m.end()+220]
        cs = re.search(r"LIKELIEST SCORELINES\s+(\d+-\d+)\s+(\d+(?:\.\d+)?)%", tail)
        btts = re.search(r"BOTH SCORE\s+(\d+)%", tail)
        o25 = re.search(r"OVER 2\.5\s+(\d+)%", tail)
        xg = re.search(r"EXP\. GOALS\s+(\d+(?:\.\d+)?)–(\d+(?:\.\d+)?)", tail)
        out.append({
            "home_team": home.strip(), "away_team": away.strip(), "league": "",
            "kickoff_raw": "", "source_timezone": "",
            "timezone_status": "TIMEZONE_UNRESOLVED",
            "p1": float(p1), "px": float(px), "p2": float(p2),
            "btts_yes": fnum(btts.group(1)) if btts else None,
            "over25": fnum(o25.group(1)) if o25 else None,
            "under25": 100.0-fnum(o25.group(1)) if o25 else None,
            "xg_home": fnum(xg.group(1)) if xg else None,
            "xg_away": fnum(xg.group(2)) if xg else None,
            "correct_score": cs.group(1) if cs else "",
        })
    return out


def parse_elofoot(text: str):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    for i, line in enumerate(lines):
        head = re.fullmatch(r"(\d{2}:\d{2}) UK/Lagos · (.+)", line)
        if not head or i + 9 >= len(lines):
            continue
        score = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", lines[i + 2])
        published = re.fullmatch(r"Predicted score · (\d+)% probability", lines[i + 4])
        if not score or not published:
            continue
        if lines[i + 5] != "1" or lines[i + 7] != "X" or lines[i + 9] != "2":
            continue
        p1 = re.fullmatch(r"(\d+)\s*%", lines[i + 6])
        px = re.fullmatch(r"(\d+)\s*%", lines[i + 8])
        p2 = re.fullmatch(r"(\d+)\s*%", lines[i + 10]) if i + 10 < len(lines) else None
        if not (p1 and px and p2):
            continue
        clock, league = head.groups()
        sh, sa = score.groups()
        out.append({
            "home_team": lines[i + 1],
            "away_team": lines[i + 3],
            "league": league,
            "kickoff_raw": clock + " UK/Lagos",
            "source_timezone": "Europe/London",
            "timezone_status": "DATE_UNRESOLVED",
            "p1": float(p1.group(1)),
            "px": float(px.group(1)),
            "p2": float(p2.group(1)),
            "correct_score": f"{sh}-{sa}",
            "payload_extra": {"correct_score_probability": float(published.group(1))},
        })
    return out

PROSOCCER_EXTRA_PAGES = {
    "goals": "https://prosoccer.eu/football/under-over",
    "correct_scores": "https://prosoccer.eu/football/correct-scores",
    "half_time": "https://prosoccer.eu/football/half-time",
    "ht_correct_scores": "https://prosoccer.eu/football/ht-correct-scores",
    "corners": "https://prosoccer.eu/football/corners",
    "htft": "https://prosoccer.eu/football/half-time-full-time",
}


def _prosoccer_blocks(text: str):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    blocks = []
    i = 0
    while i < len(lines):
        if re.fullmatch(r"\d{1,2}:\d{2}", lines[i]):
            start = i
            j = i + 1
            while j < len(lines) and not re.fullmatch(r"(?:\d{1,2}:\d{2}|FT|HT)", lines[j]):
                j += 1
            block = lines[start:j]
            if len(block) >= 4:
                blocks.append(block)
            i = j
        else:
            i += 1
    return blocks


def _pair_key(home: str, away: str):
    return canon(home) + "|" + canon(away)


def parse_prosoccer_goal_lines(text: str):
    out = {}
    for b in _prosoccer_blocks(text):
        home, away = b[1], b[2]
        picks = []
        for i in range(4, len(b) - 1):
            m = re.fullmatch(r"(Over|Under)\s+(\d+(?:\.\d+)?)", b[i], re.I)
            p = re.fullmatch(r"(\d+(?:\.\d+)?)%", b[i + 1])
            if m and p:
                picks.append({
                    "selection": m.group(1).upper(),
                    "line": float(m.group(2)),
                    "probability": float(p.group(1)),
                })
        if picks:
            out[_pair_key(home, away)] = picks
    return out


def parse_prosoccer_scorelines(text: str, *, half_time=False):
    out = {}
    for b in _prosoccer_blocks(text):
        home, away = b[1], b[2]
        picks = []
        for i in range(4, len(b) - 1):
            m = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", b[i])
            p = re.fullmatch(r"(\d+(?:\.\d+)?)%", b[i + 1])
            if m and p:
                picks.append({
                    "score": f"{m.group(1)}-{m.group(2)}",
                    "probability": float(p.group(1)),
                })
        if picks:
            out[_pair_key(home, away)] = picks[:3]
    return out


def parse_prosoccer_ht_1x2(text: str):
    out = {}
    for b in _prosoccer_blocks(text):
        home, away = b[1], b[2]
        vals = {}
        for i in range(4, len(b) - 1):
            m = re.fullmatch(r"HT\s+([1X2])", b[i], re.I)
            p = re.fullmatch(r"(\d+(?:\.\d+)?)%", b[i + 1])
            if m and p:
                vals[m.group(1).upper()] = float(p.group(1))
        if all(k in vals for k in ("1", "X", "2")):
            out[_pair_key(home, away)] = vals
    return out


def parse_prosoccer_corners(text: str):
    out = {}
    for b in _prosoccer_blocks(text):
        home, away = b[1], b[2]
        picks = []
        for i in range(4, len(b) - 1):
            m = re.fullmatch(r"(Over|Under)\s+(\d+(?:\.\d+)?)", b[i], re.I)
            p = re.fullmatch(r"(\d+(?:\.\d+)?)%", b[i + 1])
            if m and p:
                picks.append({
                    "selection": m.group(1).upper(),
                    "line": float(m.group(2)),
                    "probability": float(p.group(1)),
                })
        if picks:
            out[_pair_key(home, away)] = picks
    return out


def parse_prosoccer_htft(text: str):
    out = {}
    for b in _prosoccer_blocks(text):
        home, away = b[1], b[2]
        picks = []
        for i in range(4, len(b) - 1):
            m = re.fullmatch(r"([1X2])/([1X2])", b[i])
            p = re.fullmatch(r"(\d+(?:\.\d+)?)%", b[i + 1])
            if m and p:
                picks.append({
                    "selection": f"{m.group(1)}/{m.group(2)}",
                    "probability": float(p.group(1)),
                })
        if picks:
            out[_pair_key(home, away)] = picks[:3]
    return out


def enrich_prosoccer_predictions(preds, extra_text):
    idx = {_pair_key(p["home_team"], p["away_team"]): p for p in preds}
    goal_lines = parse_prosoccer_goal_lines(extra_text.get("goals", ""))
    scorelines = parse_prosoccer_scorelines(extra_text.get("correct_scores", ""))
    ht = parse_prosoccer_ht_1x2(extra_text.get("half_time", ""))
    ht_scores = parse_prosoccer_scorelines(extra_text.get("ht_correct_scores", ""), half_time=True)
    corners = parse_prosoccer_corners(extra_text.get("corners", ""))
    htft = parse_prosoccer_htft(extra_text.get("htft", ""))

    for key, pred in idx.items():
        extra = pred.setdefault("payload_extra", {})
        if key in goal_lines:
            extra["goal_lines"] = goal_lines[key]
        if key in scorelines:
            extra["correct_scores"] = scorelines[key]
            pred["correct_score"] = scorelines[key][0]["score"]
            extra["correct_score_probability"] = scorelines[key][0]["probability"]
        if key in ht:
            extra["ht_p1"] = ht[key]["1"]
            extra["ht_px"] = ht[key]["X"]
            extra["ht_p2"] = ht[key]["2"]
        if key in ht_scores:
            extra["ht_correct_scores"] = ht_scores[key]
            extra["ht_correct_score"] = ht_scores[key][0]["score"]
            extra["ht_correct_score_probability"] = ht_scores[key][0]["probability"]
        if key in corners:
            extra["corner_lines"] = corners[key]
            best = max(corners[key], key=lambda x: x["probability"])
            extra["corners_line"] = best["line"]
            if best["selection"] == "OVER":
                extra["corners_over"] = best["probability"]
            else:
                extra["corners_under"] = best["probability"]
        if key in htft:
            extra["htft_top"] = htft[key]
            extra["htft_selection"] = htft[key][0]["selection"]
            extra["htft_probability"] = htft[key][0]["probability"]
    return preds


PARSERS = {
    "prosoccer": parse_prosoccer,
    "statsultra": parse_statsultra,
    "elofoot": parse_elofoot,
}


def current_slot(cur, start):
    delta = utcnow() - start
    return max(1, min(16, int(delta.total_seconds() // (3 * 3600)) + 1))


def ensure_control():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM source_pilot_control WHERE pilot_id=%s", (PILOT_ID,))
        row = cur.fetchone()
        if row:
            if utcnow() >= row["ends_at_utc"] and row["status"] != "COMPLETE":
                cur.execute("UPDATE source_pilot_control SET status='COMPLETE',updated_at=NOW() WHERE pilot_id=%s", (PILOT_ID,))
                conn.commit()
                row["status"] = "COMPLETE"
            return row
        start = utcnow()
        end = start + timedelta(hours=48)
        cur.execute(
            "INSERT INTO source_pilot_control(pilot_id,started_at_utc,ends_at_utc,status,slots_expected) VALUES (%s,%s,%s,'RUNNING',16)",
            (PILOT_ID, start, end),
        )
        conn.commit()
        return {"pilot_id": PILOT_ID, "started_at_utc": start, "ends_at_utc": end, "status": "RUNNING", "slots_expected": 16}


def anchor_rows():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
        SELECT DISTINCT ON (match_id)
          match_id,home_team,away_team,league,kickoff_utc,snapshot_timestamp_utc
        FROM model_snapshots
        WHERE valid_prematch=TRUE
          AND kickoff_utc > NOW() - INTERVAL '12 hours'
          AND kickoff_utc < NOW() + INTERVAL '10 days'
        ORDER BY match_id,snapshot_timestamp_utc DESC
        """)
        return list(cur.fetchall())


def match_anchor(pred, anchors):
    home, away = canon(pred["home_team"]), canon(pred["away_team"])
    candidates = [a for a in anchors if canon(a["home_team"]) == home and canon(a["away_team"]) == away]
    if not candidates:
        return None, "NOT_FOUND", 0.0, "team pair not in anchor"
    if len(candidates) > 1:
        return None, "AMBIGUOUS", 0.0, "multiple anchor team-pair matches"
    a = candidates[0]
    if pred.get("kickoff_utc"):
        diff = abs((pred["kickoff_utc"] - a["kickoff_utc"]).total_seconds())
        if diff > 3600:
            return a["match_id"], "TIMEZONE_UNRESOLVED", 0.5, f"kickoff diff {diff/3600:.2f}h"
        return a["match_id"], "FOUND", 1.0, ""
    return a["match_id"], "FOUND_NAME_ONLY", 0.7, "kickoff unresolved"


def resolve_prosoccer_date(pred):
    now_it = datetime.now(ROME)
    hh, mm = map(int, pred["kickoff_raw"].split(":"))
    local = datetime(now_it.year, now_it.month, now_it.day, hh, mm, tzinfo=ROME)
    pred["kickoff_utc"] = local.astimezone(timezone.utc)


def collect_source(source, url, slot_no, anchors):
    started = utcnow()
    run_id = f"{PILOT_ID}-{source}-s{slot_no:02d}-{uuid.uuid4().hex[:8]}"
    status, created = browser_api("POST", BROWSER_API, {
        "proxyCountryCode": "it",
        "timeout": 8,
        "solveCaptchas": False,
        "enableRecording": False,
        "browserScreenWidth": 1366,
        "browserScreenHeight": 900,
        "metadata": {"purpose": f"{PILOT_ID}-{source}-s{slot_no:02d}"},
    })
    if status not in (200, 201) or not created.get("cdpUrl"):
        raise RuntimeError(f"Browser create failed: {status} {created}")
    sid, cdp = created["id"], created["cdpUrl"]
    html = b""
    shot = b""
    text = ""
    final_url = url
    http_status = None
    page_errors = []
    extra_text = {}
    extra_page_status = {}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            cdp_session = context.new_cdp_session(page)
            cdp_session.send("Emulation.setTimezoneOverride", {"timezoneId": "Europe/Rome"})
            page.on("pageerror", lambda exc: page_errors.append(str(exc)[:300]))
            resp = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                page.wait_for_timeout(5000)
            final_url = page.url
            http_status = resp.status if resp else None
            text = page.locator("body").inner_text(timeout=30000)
            main_html = page.content().encode("utf-8")
            shot = page.screenshot(full_page=True, timeout=30000)
            html_parts = [b"<!-- PAGE:main -->\n" + main_html]

            if source == "prosoccer":
                for label, page_url in PROSOCCER_EXTRA_PAGES.items():
                    r = page.goto(page_url, wait_until="domcontentloaded", timeout=90000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=30000)
                    except Exception:
                        page.wait_for_timeout(4000)
                    extra_text[label] = page.locator("body").inner_text(timeout=30000)
                    raw = page.content().encode("utf-8")
                    html_parts.append(f"\n<!-- PAGE:{label} URL:{page_url} -->\n".encode() + raw)
                    extra_page_status[label] = {
                        "url": page.url,
                        "http_status": r.status if r else None,
                        "html_sha256": sha(raw),
                        "html_bytes": len(raw),
                    }
            html = b"".join(html_parts)
            browser.close()
    finally:
        browser_api("PATCH", f"{BROWSER_API}/{sid}", {"action": "stop"})
        _, sess = browser_api("GET", f"{BROWSER_API}/{sid}")

    preds = PARSERS[source](text)
    if source == "prosoccer":
        enrich_prosoccer_predictions(preds, extra_text)
        for pred in preds:
            resolve_prosoccer_date(pred)

    manifest = {
        "pilot_id": PILOT_ID,
        "source": source,
        "slot_no": slot_no,
        "browser_timezone_override": "Europe/Rome",
        "visible_rows": len(preds),
        "page_errors": page_errors[:20],
        "raw_html_bytes": len(html),
        "screenshot_bytes": len(shot),
        "extra_pages": extra_page_status,
    }

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          INSERT INTO source_pilot_runs(
            run_id,pilot_id,source,slot_no,started_at_utc,finished_at_utc,status,
            final_url,http_status,browser_session_id,browser_cost,proxy_cost,
            proxy_used_mb,captcha_cost,html_sha256,screenshot_sha256,html_gzip,
            screenshot_png,manifest
          ) VALUES (%s,%s,%s,%s,%s,%s,'OK',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
          ON CONFLICT (pilot_id,source,slot_no) DO NOTHING
        """, (
            run_id,PILOT_ID,source,slot_no,started,utcnow(),final_url,http_status,sid,
            fnum(sess.get("browserCost")),fnum(sess.get("proxyCost")),
            fnum(sess.get("proxyUsedMb")),fnum(sess.get("captchaCost") or 0),
            sha(html),sha(shot),gzip.compress(html, compresslevel=6),shot,json.dumps(manifest)
        ))
        for pred in preds:
            key = hashlib.sha256(f"{source}|{pred['home_team']}|{pred['away_team']}|{pred.get('kickoff_raw','')}".encode()).hexdigest()[:24]
            extra = pred.pop("payload_extra", {})
            payload = {**pred, **extra}
            cur.execute("""
              INSERT INTO source_pilot_predictions(
                run_id,source,source_match_key,home_team,away_team,league,kickoff_raw,
                source_timezone,kickoff_utc,timezone_status,p1,px,p2,over25,under25,
                btts_yes,btts_no,xg_home,xg_away,correct_score,parse_status,payload
              ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OK',%s::jsonb)
            """, (
                run_id,source,key,pred["home_team"],pred["away_team"],pred.get("league",""),
                pred.get("kickoff_raw",""),pred.get("source_timezone",""),pred.get("kickoff_utc"),
                pred.get("timezone_status","TIMEZONE_UNRESOLVED"),
                pred.get("p1"),pred.get("px"),pred.get("p2"),pred.get("over25"),pred.get("under25"),
                pred.get("btts_yes"),pred.get("btts_no"),pred.get("xg_home"),pred.get("xg_away"),
                pred.get("correct_score",""),json.dumps(payload)
            ))
            anchor_id, mstatus, confidence, reason = match_anchor(pred, anchors)
            cur.execute("""
              INSERT INTO source_pilot_matches(run_id,source_match_key,anchor_match_id,match_status,match_confidence,reason)
              VALUES (%s,%s,%s,%s,%s,%s)
            """, (run_id,key,anchor_id,mstatus,confidence,reason))
        conn.commit()
    return {
        "source": source,
        "run_id": run_id,
        "rows": len(preds),
        "extra_pages": sorted(extra_page_status),
        "session": {k:sess.get(k) for k in ("browserCost","proxyCost","proxyUsedMb","captchaCost")},
    }

def main():
    if not os.environ.get("BROWSER_USE_API_KEY", "").strip():
        raise SystemExit("BROWSER_USE_API_KEY is required; pilot clock has not started")
    if not os.environ.get("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required; pilot clock has not started")
    control = ensure_control()
    if control["status"] == "COMPLETE" or utcnow() >= control["ends_at_utc"]:
        print(json.dumps({"pilot_id": PILOT_ID, "status": "COMPLETE"}))
        return
    slot_no = current_slot(None, control["started_at_utc"])
    anchors = anchor_rows()
    results = []
    for source, url in SOURCES.items():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM source_pilot_runs WHERE pilot_id=%s AND source=%s AND slot_no=%s", (PILOT_ID,source,slot_no))
            if cur.fetchone():
                results.append({"source": source, "slot_no": slot_no, "status": "ALREADY_DONE"})
                continue
        try:
            results.append(collect_source(source,url,slot_no,anchors))
        except Exception as exc:
            results.append({"source":source,"slot_no":slot_no,"status":"ERROR","error":f"{type(exc).__name__}: {exc}"})
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE source_pilot_control SET updated_at=NOW() WHERE pilot_id=%s", (PILOT_ID,))
        conn.commit()
    print(json.dumps({"pilot_id":PILOT_ID,"slot_no":slot_no,"results":results},default=str))


if __name__ == "__main__":
    main()
