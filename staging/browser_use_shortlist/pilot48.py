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
    # public list explicitly labels UK/Lagos. Use Europe/London only for rows
    # whose date can be resolved from a nearby "Thu, Oct 1" style header.
    flat = re.sub(r"\s+", " ", text)
    out = []
    pattern = re.compile(
        r"(\d{2}:\d{2}) UK/Lagos · ([A-Za-z0-9 .'-]+?) "
        r"([A-Za-zÀ-ÿ0-9 .'-]+?) (\d+) [–-] (\d+) "
        r"([A-Za-zÀ-ÿ0-9 .'-]+?) Predicted score · (\d+)% probability "
        r"1 (\d+) % X (\d+) % 2 (\d+) %"
    )
    for m in pattern.finditer(flat):
        clock, league, home, sh, sa, away, scorep, p1, px, p2 = m.groups()
        out.append({
            "home_team": home.strip(), "away_team": away.strip(), "league": league.strip(),
            "kickoff_raw": clock + " UK/Lagos", "source_timezone": "Europe/London",
            "timezone_status": "DATE_UNRESOLVED",
            "p1": float(p1), "px": float(px), "p2": float(p2),
            "correct_score": f"{sh}-{sa}",
            "payload_extra": {"correct_score_probability": float(scorep)},
        })
    return out


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
            html = page.content().encode("utf-8")
            shot = page.screenshot(full_page=True, timeout=30000)
            browser.close()
    finally:
        browser_api("PATCH", f"{BROWSER_API}/{sid}", {"action": "stop"})
        _, sess = browser_api("GET", f"{BROWSER_API}/{sid}")

    preds = PARSERS[source](text)
    if source == "prosoccer":
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
    return {"source": source, "run_id": run_id, "rows": len(preds), "session": {k:sess.get(k) for k in ("browserCost","proxyCost","proxyUsedMb","captchaCost")}}


def main():
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
