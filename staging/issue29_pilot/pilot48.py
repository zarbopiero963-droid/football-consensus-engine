#!/usr/bin/env python3
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.db import connect
from staging.page_capture import capture_page

BROWSER_API = "https://api.browser-use.com/api/v4/browsers"
ROME = ZoneInfo("Europe/Rome")
PILOT_ID = os.environ.get("ISSUE29_PILOT_ID", "source-pilot-48h-issue29-v1")

SOURCES = {
    "xgtoday": "https://xgtoday.com/",
    "kickform": "https://www.thepunterspage.com/kickform/",
    "thedatabetics": "https://thedatabetics.com/football-predictions/",
    "kickoff_ai": "https://kickoff.ai/",
    "vitisport": "https://www.vitisport.it/",
    "clubelo": "https://clubelo.com/Fixtures",
    "opta_analyst": "https://theanalyst.com/",
    "scoutingstats_ai": "https://scoutingstats.ai/",
    "aipredict_io": "https://aipredict.io/football/",
    "totalcorner": "https://www.totalcorner.com/",
}


def utcnow():
    return datetime.now(timezone.utc)


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


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


def pct(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)%", (s or "").strip())
    return float(m.group(1)) if m else None


def valid_triplet(a, b, c):
    vals = [a, b, c]
    return all(v is not None and 0 <= v <= 100 for v in vals) and 98 <= sum(vals) <= 102


def run_quality(http_status, rows_parsed):
    """Acquisition and parsing are distinct; neither establishes eligibility."""
    return {
        "transport_status": ("HTTP_OK" if http_status == 200 else
                             "HTTP_UNOBSERVED" if http_status is None else "HTTP_ERROR"),
        "content_parse_status": "PARSED" if rows_parsed else "EMPTY",
    }


def parse_xgtoday(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    for i in range(1, len(lines) - 6):
        p1 = pct(lines[i])
        px = pct(lines[i + 2]) if i + 2 < len(lines) else None
        p2 = pct(lines[i + 5]) if i + 5 < len(lines) else None
        if not valid_triplet(p1, px, p2):
            continue
        home = lines[i - 1]
        away = lines[i + 4]
        if pct(home) is not None or pct(away) is not None:
            continue
        out.append({
            "home_team": home, "away_team": away, "p1": p1, "px": px, "p2": p2,
            "kickoff_raw": "", "source_timezone": "", "timezone_status": "TIMEZONE_UNRESOLVED",
            "parse_status": "OK", "payload_extra": {"source_layer": "published_1x2"},
        })
    return _dedupe(out)


def parse_kickform(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    for i, line in enumerate(lines):
        if not re.fullmatch(r"\d{2}/\d{2}", line) or i < 2 or i + 4 >= len(lines):
            continue
        if not re.fullmatch(r"\d{2}:\d{2}", lines[i + 1]):
            continue
        p1, px, p2 = pct(lines[i + 2]), pct(lines[i + 3]), pct(lines[i + 4])
        if valid_triplet(p1, px, p2):
            out.append({
                "home_team": lines[i - 2], "away_team": lines[i - 1],
                "kickoff_raw": f"{line} {lines[i + 1]}",
                "source_timezone": "", "timezone_status": "TIMEZONE_UNRESOLVED",
                "p1": p1, "px": px, "p2": p2, "parse_status": "OK",
                "payload_extra": {"source_layer": "kickform_algorithm"},
            })
    return _dedupe(out)


def parse_thedatabetics(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    for i in range(1, len(lines) - 5):
        if lines[i] != "vs":
            continue
        home, away = lines[i - 1], lines[i + 1]
        pick = lines[i + 2] if i + 2 < len(lines) else ""
        probability = pct(lines[i + 3]) if i + 3 < len(lines) else None
        clock = lines[i + 4] if i + 4 < len(lines) and re.fullmatch(r"\d{2}:\d{2}", lines[i + 4]) else ""
        if probability is None:
            continue
        row = {
            "home_team": home, "away_team": away, "kickoff_raw": clock,
            "source_timezone": "", "timezone_status": "TIMEZONE_UNRESOLVED",
            "p1": None, "px": None, "p2": None, "parse_status": "PARTIAL_PICK",
            "payload_extra": {"selection": pick, "probability": probability},
        }
        low = pick.lower()
        if "home" in low:
            row["p1"] = probability
        elif "draw" in low:
            row["px"] = probability
        elif "away" in low:
            row["p2"] = probability
        out.append(row)
    return _dedupe(out)


def parse_kickoff_ai(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    for i in range(1, len(lines) - 4):
        p1, px, p2 = pct(lines[i]), pct(lines[i + 1]), pct(lines[i + 2])
        if not valid_triplet(p1, px, p2):
            continue
        if not re.search(r"\d{1,2}:\d{2}.*\d{1,2}\s+[A-Z]+", lines[i + 3], re.I):
            continue
        out.append({
            "home_team": lines[i - 1], "away_team": lines[i + 4],
            "kickoff_raw": lines[i + 3], "source_timezone": "",
            "timezone_status": "TIMEZONE_UNRESOLVED",
            "p1": p1, "px": px, "p2": p2, "parse_status": "OK",
            "payload_extra": {"source_layer": "kickoff_ai_ml"},
        })
    return _dedupe(out)


def parse_vitisport(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    pick_tokens = {"1", "2", "0", "10", "02", "12", "-"}
    for i in range(len(lines) - 4):
        if not re.fullmatch(r"\d{1,2}:\d{2}", lines[i]):
            continue
        home = lines[i + 1]
        score = lines[i + 2]
        away = lines[i + 3]
        pick = lines[i + 4]
        if not re.fullmatch(r"\d+\s*:\s*\d+", score):
            continue
        if pick not in pick_tokens:
            continue
        row = {
            "home_team": home,
            "away_team": away,
            "kickoff_raw": lines[i],
            "source_timezone": "",
            "timezone_status": "TIMEZONE_UNRESOLVED",
            "p1": None,
            "px": None,
            "p2": None,
            "parse_status": "PARTIAL_PICK",
            "payload_extra": {
                "selection": pick,
                "correct_score": re.sub(r"\s+", "", score),
                "source_layer": "vitisport_index",
            },
        }
        out.append(row)
    return _dedupe(out)


def parse_clubelo(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    for i, line in enumerate(lines):
        if "CEST" not in line or i + 2 >= len(lines):
            continue
        home_line, away_line = lines[i + 1], lines[i + 2]
        hp = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)%", home_line)]
        ap = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)%", away_line)]
        if len(hp) < 2 or len(ap) < 1:
            continue
        p1, px, p2 = hp[0], hp[1], ap[0]
        if not valid_triplet(p1, px, p2):
            continue
        home = re.split(r"\t|\d+\s+", home_line, maxsplit=1)[0].strip()
        away = re.split(r"\t|\d+\s+", away_line, maxsplit=1)[0].strip()
        home = re.sub(r"^\d+\s+", "", home)
        away = re.sub(r"^\d+\s+", "", away)
        out.append({
            "home_team": home, "away_team": away, "kickoff_raw": line,
            "source_timezone": "CEST", "timezone_status": "VERIFIED_SOURCE_TZ",
            "p1": p1, "px": px, "p2": p2, "parse_status": "OK",
            "payload_extra": {"source_layer": "clubelo"},
        })
    return _dedupe(out)


def parse_opta(text):
    pattern = re.compile(
        r"Win probability:\s*(.+?)\s+(\d+(?:\.\d+)?)%,\s*draw\s+(\d+(?:\.\d+)?)%,\s*(.+?)\s+(\d+(?:\.\d+)?)%\.",
        re.I
    )
    out = []
    for home, p1, px, away, p2 in pattern.findall(text):
        a, b, c = float(p1), float(px), float(p2)
        if not valid_triplet(a, b, c):
            continue
        out.append({
            "home_team": home.strip(), "away_team": away.strip(),
            "kickoff_raw": "", "source_timezone": "", "timezone_status": "TIMEZONE_UNRESOLVED",
            "p1": a, "px": b, "p2": c, "parse_status": "OK",
            "payload_extra": {"source_layer": "opta_win_probability"},
        })
    return _dedupe(out)


def parse_scoutingstats(text):
    pattern = re.compile(
        r"([A-Za-zÀ-ÿ .'-]+)\s+\d+(?:\.\d+)?[–-]\d+(?:\.\d+)?\s+PREDICTED GOALS\s+"
        r"([A-Za-zÀ-ÿ .'-]+)\s+Home\s+(\d+(?:\.\d+)?)%\s+Draw\s+(\d+(?:\.\d+)?)%\s+Away\s+(\d+(?:\.\d+)?)%",
        re.I
    )
    out = []
    for home, away, p1, px, p2 in pattern.findall(text):
        a, b, c = float(p1), float(px), float(p2)
        if valid_triplet(a, b, c):
            out.append({
                "home_team": home.strip(), "away_team": away.strip(),
                "kickoff_raw": "", "source_timezone": "", "timezone_status": "TIMEZONE_UNRESOLVED",
                "p1": a, "px": b, "p2": c, "parse_status": "OK",
                "payload_extra": {"source_layer": "scoutingstats_model"},
            })
    return _dedupe(out)


def parse_aipredict(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    # The current public free card labels AI probabilities explicitly. Adjacent
    # locked cards expose ODDS bars instead; never parse those as model vectors.
    for i,line in enumerate(lines):
        if line != 'FREE TIP OF THE DAY' or lines.count('FREE TIP OF THE DAY') != 1: continue
        end = next((j for j in range(i+1,len(lines)) if lines[j] == 'Full AI analysis →'),None)
        if end is None: continue
        card = lines[i+1:end]
        if (card.count('AI PREDICTION') != 1 or card.count('AI') != 1 or
                any(x.upper() in ('ODDS','LIVE','HT','FT') for x in card)):
            continue
        ai = card.index('AI')
        if (ai < 2 or ai+3 >= len(card) or
                sum(bool(re.match(r'[1X2]\s*·',x)) for x in card) != 3): continue
        values=[]
        for label,value in zip(('1','X','2'),card[ai+1:ai+4]):
            match=re.fullmatch(re.escape(label)+r'\s*·\s*(\d+(?:\.\d+)?)%',value)
            if match is None: break
            values.append(float(match[1]))
        if len(values) != 3 or not valid_triplet(*values): continue
        home,away=card[0],card[ai-1]
        if not any(c.isalpha() for c in home) or not any(c.isalpha() for c in away): continue
        kickoff=lines[i-2] if i>=2 and re.fullmatch(r'\d{1,2}:\d{2}',lines[i-2]) else ''
        out.append({'home_team':home,'away_team':away,'league':lines[i-1] if i else '',
            'kickoff_raw':kickoff,'source_timezone':'','timezone_status':'TIMEZONE_UNRESOLVED',
            'p1':values[0],'px':values[1],'p2':values[2],'parse_status':'OK',
            'payload_extra':{'source_layer':'aipredict_ai_free_tip',
                'probability_evidence':'PUBLIC_AI_LABELLED_1_X_2',
                'market_odds_role':'sanity_check_only_claimed',
                'source_time_policy':'OWN_DEVICE_TIME_ZONE_NOT_CANONICAL',
                'display_prediction_raw':card[1:card.index('AI PREDICTION')]}})
    for i, line in enumerate(lines):
        if line.upper() != "AI PREDICTION":
            continue
        if i < 2:
            continue
        home = lines[i - 2]
        score_or_time = lines[i - 1]
        if not any(c.isalpha() for c in home) or score_or_time.upper() in ('LIVE','HT','FT'):
            continue
        j = i + 1
        if j >= len(lines):
            continue
        # Legacy public layout had a single-letter badge and explicit AI role.
        if not re.fullmatch(r'[A-Z]',lines[j]): continue
        j += 1
        if j >= len(lines):
            continue
        away = lines[j]
        j += 1
        if j >= len(lines) or lines[j].upper() != 'AI': continue
        j += 1
        if j + 2 >= len(lines):
            continue
        a, b, c = pct(lines[j]), pct(lines[j + 1]), pct(lines[j + 2])
        if not valid_triplet(a, b, c):
            continue
        out.append({
            "home_team": home,
            "away_team": away,
            "kickoff_raw": score_or_time if re.fullmatch(r"\d{1,2}:\d{2}", score_or_time) else "",
            "source_timezone": "",
            "timezone_status": "TIMEZONE_UNRESOLVED",
            "p1": a, "px": b, "p2": c,
            "parse_status": "OK",
            "payload_extra": {
                "source_layer": "aipredict_ai",
                "market_odds_role": "sanity_check_only_claimed",
                "display_score_or_time": score_or_time,
            },
        })
    return _dedupe(out)


def parse_totalcorner(text):
    out = []
    rx = re.compile(
        r"([^\n\t]+)\t(\d{2}/\d{2}\s+\d{2}:\d{2})\t([^\n\t]+)\tvs\t([^\n\t]+)\t"
        r"(\d+(?:\.\d+)?)\t(\d+(?:\.\d+)?)\t(\d+)\t(\d+)\t(Over|Under)\t(\d+)%",
        re.I
    )
    for league, kickoff, home, away, line, avg, over, under, selection, profit in rx.findall(text):
        out.append({
            "home_team": home.strip(), "away_team": away.strip(), "league": league.strip(),
            "kickoff_raw": kickoff, "source_timezone": "Europe/Rome",
            "timezone_status": "VERIFIED_SOURCE_TZ",
            "p1": None, "px": None, "p2": None, "parse_status": "CORNERS_ONLY",
            "payload_extra": {
                "market": "CORNERS_FT", "line": float(line), "average": float(avg),
                "over_probability": float(over), "under_probability": float(under),
                "selection": selection.upper(), "profit_display_pct": float(profit),
                "source_layer": "free_corner_model",
            },
        })
    return _dedupe(out)


PARSERS = {
    "xgtoday": parse_xgtoday,
    "kickform": parse_kickform,
    "thedatabetics": parse_thedatabetics,
    "kickoff_ai": parse_kickoff_ai,
    "vitisport": parse_vitisport,
    "clubelo": parse_clubelo,
    "opta_analyst": parse_opta,
    "scoutingstats_ai": parse_scoutingstats,
    "aipredict_io": parse_aipredict,
    "totalcorner": parse_totalcorner,
}


def _dedupe(rows):
    seen, out = set(), []
    for r in rows:
        key = (canon(r.get("home_team", "")), canon(r.get("away_team", "")), r.get("kickoff_raw", ""))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def current_slot(start):
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
            "INSERT INTO source_pilot_control(pilot_id,started_at_utc,ends_at_utc,status,slots_expected) "
            "VALUES (%s,%s,%s,'RUNNING',16)",
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
    return candidates[0]["match_id"], "FOUND_NAME_ONLY", 0.7, "kickoff unresolved or source-specific"


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
    final_url = url
    http_status = None
    page_errors = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            session = context.new_cdp_session(page)
            session.send("Emulation.setTimezoneOverride", {"timezoneId": "Europe/Rome"})
            page.on("pageerror", lambda exc: page_errors.append(str(exc)[:300]))
            resp = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            http_status = resp.status if resp else None
            capture = capture_page(page, http_status, PARSERS[source], evidence_kind="prediction_rows")
            final_url = page.url
            html = capture.html
            shot = page.screenshot(full_page=True, timeout=30000)
            browser.close()
    finally:
        browser_api("PATCH", f"{BROWSER_API}/{sid}", {"action": "stop"})
        _, sess = browser_api("GET", f"{BROWSER_API}/{sid}")

    preds = capture.parsed if capture.accepted else []
    invalid_triplets = 0
    for pred in preds:
        if all(pred.get(k) is not None for k in ("p1", "px", "p2")) and not valid_triplet(pred["p1"], pred["px"], pred["p2"]):
            invalid_triplets += 1

    manifest = {
        "pilot_id": PILOT_ID, "source": source, "slot_no": slot_no,
        "browser_timezone_override": "Europe/Rome",
        "rows_parsed": len(preds), "invalid_triplets": invalid_triplets,
        "page_errors": page_errors[:20], "raw_html_bytes": len(html),
        "screenshot_bytes": len(shot), "http_status": http_status,
        **run_quality(http_status, len(preds)),
        "page_capture": capture.audit,
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
            key = hashlib.sha256(
                f"{source}|{pred['home_team']}|{pred['away_team']}|{pred.get('kickoff_raw','')}".encode()
            ).hexdigest()[:24]
            extra = pred.get("payload_extra", {})
            payload = {**pred, **extra}
            cur.execute("""
              INSERT INTO source_pilot_predictions(
                run_id,source,source_match_key,home_team,away_team,league,kickoff_raw,
                source_timezone,kickoff_utc,timezone_status,p1,px,p2,over25,under25,
                btts_yes,btts_no,xg_home,xg_away,correct_score,parse_status,payload
              ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,NULL,NULL,NULL,NULL,NULL,NULL,'',%s,%s::jsonb)
            """, (
                run_id,source,key,pred["home_team"],pred["away_team"],pred.get("league",""),
                pred.get("kickoff_raw",""),pred.get("source_timezone",""),
                pred.get("timezone_status","TIMEZONE_UNRESOLVED"),
                pred.get("p1"),pred.get("px"),pred.get("p2"),
                pred.get("parse_status","OK"),json.dumps(payload, default=str)
            ))
            anchor_id, mstatus, confidence, reason = match_anchor(pred, anchors)
            cur.execute("""
              INSERT INTO source_pilot_matches(run_id,source_match_key,anchor_match_id,match_status,match_confidence,reason)
              VALUES (%s,%s,%s,%s,%s,%s)
            """, (run_id,key,anchor_id,mstatus,confidence,reason))
        conn.commit()

    return {
        "source": source, "run_id": run_id, "rows": len(preds),
        "http_status": http_status,
        "page_capture": capture.audit,
        "session": {k:sess.get(k) for k in ("browserCost","proxyCost","proxyUsedMb","captchaCost")},
    }


def main():
    if not os.environ.get("BROWSER_USE_API_KEY", "").strip():
        raise SystemExit("BROWSER_USE_API_KEY is required; issue29 pilot clock has not started")
    if not os.environ.get("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required; issue29 pilot clock has not started")

    control = ensure_control()
    if control["status"] == "COMPLETE" or utcnow() >= control["ends_at_utc"]:
        print(json.dumps({"pilot_id": PILOT_ID, "status": "COMPLETE"}))
        return

    slot_no = current_slot(control["started_at_utc"])
    anchors = anchor_rows()
    results = []
    for source, url in SOURCES.items():
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM source_pilot_runs WHERE pilot_id=%s AND source=%s AND slot_no=%s",
                (PILOT_ID, source, slot_no)
            )
            if cur.fetchone():
                results.append({"source": source, "slot_no": slot_no, "status": "ALREADY_DONE"})
                continue
        try:
            results.append(collect_source(source, url, slot_no, anchors))
        except Exception as exc:
            results.append({"source": source, "slot_no": slot_no, "status": "ERROR", "error": f"{type(exc).__name__}: {exc}"})

    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE source_pilot_control SET updated_at=NOW() WHERE pilot_id=%s", (PILOT_ID,))
        conn.commit()

    print(json.dumps({"pilot_id": PILOT_ID, "slot_no": slot_no, "results": results}, default=str))


if __name__ == "__main__":
    main()
