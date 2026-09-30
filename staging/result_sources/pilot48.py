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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect

BROWSER_API = "https://api.browser-use.com/api/v4/browsers"
PILOT_ID = os.environ.get("RESULT_SOURCE_PILOT_ID", "result-source-pilot-48h-issue20-v1")
SOURCES = {
    "footiqo": "https://footiqo.com/database/leagues/brazil-serie-a/",
    "smart_soccer_stats": "https://www.smartsoccerstats.eu/game-statistics",
    "statbunker": "https://www.statbunker.com/competitions/LastMatches?comp_id=785",
    "worldfootball": "https://www.worldfootball.net/competition/co111/italy-serie-a/all-matches/",
}


def utcnow():
    return datetime.now(timezone.utc)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fnum(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def ensure_control():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM result_source_pilot_control WHERE pilot_id=%s", (PILOT_ID,))
        row = cur.fetchone()
        if row:
            if utcnow() >= row["ends_at_utc"] and row["status"] != "COMPLETE":
                cur.execute(
                    "UPDATE result_source_pilot_control SET status='COMPLETE',updated_at=NOW() WHERE pilot_id=%s",
                    (PILOT_ID,),
                )
                conn.commit()
                row["status"] = "COMPLETE"
            return row
        start = utcnow()
        end = start + timedelta(hours=48)
        cur.execute(
            """INSERT INTO result_source_pilot_control(
                 pilot_id,started_at_utc,ends_at_utc,status,slots_expected
               ) VALUES (%s,%s,%s,'RUNNING',16)""",
            (PILOT_ID, start, end),
        )
        conn.commit()
        return {
            "pilot_id": PILOT_ID,
            "started_at_utc": start,
            "ends_at_utc": end,
            "status": "RUNNING",
            "slots_expected": 16,
        }


def current_slot(start):
    delta = utcnow() - start
    return max(1, min(16, int(delta.total_seconds() // (3 * 3600)) + 1))


def inspect_source(source: str, text: str, html: str) -> tuple[int, dict]:
    low = text.lower()
    if source == "footiqo":
        rows = max(
            len(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)),
            len(re.findall(r"\bcurrent season\b", low)),
        )
        caps = {
            "ft_score": all(x in low for x in ("ft home goals", "ft away goals")),
            "ht_score": all(x in low for x in ("1h home goals", "1h away goals")),
            "corners_ft": ("hcft" in low and "acft" in low) or "corners & cards" in low,
            "corners_ht": ("hc1h" in low and "ac1h" in low) or "first half" in low,
        }
    elif source == "smart_soccer_stats":
        rows = len(re.findall(r"\(\d+\s*-\s*\d+\)", text))
        caps = {
            "ft_score": bool(re.search(r"\n\d+\s*-\s*\d+\n", text)),
            "ht_score": rows > 0,
            "corners_ft": "corners & fouls" in low,
            "corners_ht": "half-time" in low and "corners & fouls" in low,
        }
    elif source == "statbunker":
        ids = set(re.findall(r"match_id(?:=|%3D)(\d+)", html, flags=re.I))
        rows = len(ids)
        caps = {
            "ft_score": "lastmatches" in low or "match overview" in low,
            "ht_score": False,
            "corners_ft": False,
            "corners_ht": False,
        }
    elif source == "worldfootball":
        rows = len(re.findall(r"\b\d{1,2}:\d{1,2}\b", text))
        caps = {
            "ft_score": rows > 0 or "finished" in low,
            "ht_score": "half-time" in low or "halftime" in low,
            "corners_ft": "corners" in low,
            "corners_ht": False,
        }
    else:
        rows, caps = 0, {}
    return rows, caps


def collect_source(source: str, url: str, slot_no: int):
    started = utcnow()
    run_id = f"{PILOT_ID}-{source}-s{slot_no:02d}-{uuid.uuid4().hex[:8]}"
    status, created = browser_api(
        "POST",
        BROWSER_API,
        {
            "proxyCountryCode": "it",
            "timeout": 8,
            "solveCaptchas": False,
            "enableRecording": False,
            "browserScreenWidth": 1366,
            "browserScreenHeight": 900,
            "metadata": {"purpose": f"{PILOT_ID}-{source}-s{slot_no:02d}"},
        },
    )
    if status not in (200, 201) or not created.get("cdpUrl"):
        raise RuntimeError(f"Browser create failed: {status} {created}")
    sid, cdp = created["id"], created["cdpUrl"]
    html = b""
    shot = b""
    body_text = ""
    final_url = url
    http_status = None
    page_errors = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            page.on("pageerror", lambda exc: page_errors.append(str(exc)[:300]))
            resp = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                page.wait_for_timeout(5000)
            final_url = page.url
            http_status = resp.status if resp else None
            body_text = page.locator("body").inner_text(timeout=30000)
            html = page.content().encode("utf-8")
            shot = page.screenshot(full_page=True, timeout=30000)
            browser.close()
    finally:
        browser_api("PATCH", f"{BROWSER_API}/{sid}", {"action": "stop"})
        _, sess = browser_api("GET", f"{BROWSER_API}/{sid}")

    rows_found, capabilities = inspect_source(source, body_text, html.decode("utf-8", "replace"))
    manifest = {
        "pilot_id": PILOT_ID,
        "source": source,
        "slot_no": slot_no,
        "page_errors": page_errors[:20],
        "rows_found": rows_found,
        "capabilities": capabilities,
        "raw_html_bytes": len(html),
        "screenshot_bytes": len(shot),
    }

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO result_source_pilot_runs(
                 run_id,pilot_id,source,slot_no,started_at_utc,finished_at_utc,status,
                 final_url,http_status,browser_session_id,browser_cost,proxy_cost,
                 proxy_used_mb,captcha_cost,rows_found,capabilities,html_sha256,
                 screenshot_sha256,html_gzip,screenshot_png,manifest
               ) VALUES (%s,%s,%s,%s,%s,%s,'OK',%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb)
               ON CONFLICT (pilot_id,source,slot_no) DO NOTHING""",
            (
                run_id,
                PILOT_ID,
                source,
                slot_no,
                started,
                utcnow(),
                final_url,
                http_status,
                sid,
                fnum(sess.get("browserCost")),
                fnum(sess.get("proxyCost")),
                fnum(sess.get("proxyUsedMb")),
                fnum(sess.get("captchaCost") or 0),
                rows_found,
                json.dumps(capabilities),
                sha(html),
                sha(shot),
                gzip.compress(html, compresslevel=6),
                shot,
                json.dumps(manifest),
            ),
        )
        conn.commit()

    return {
        "source": source,
        "run_id": run_id,
        "rows_found": rows_found,
        "capabilities": capabilities,
        "session": {k: sess.get(k) for k in ("browserCost", "proxyCost", "proxyUsedMb", "captchaCost")},
    }


def main():
    if not os.environ.get("BROWSER_USE_API_KEY", "").strip():
        raise SystemExit("BROWSER_USE_API_KEY is required; result-source pilot clock has not started")
    if not os.environ.get("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required; result-source pilot clock has not started")
    control = ensure_control()
    if control["status"] == "COMPLETE" or utcnow() >= control["ends_at_utc"]:
        print(json.dumps({"result_source_pilot_id": PILOT_ID, "status": "COMPLETE"}))
        return
    slot_no = current_slot(control["started_at_utc"])
    results = []
    for source, url in SOURCES.items():
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM result_source_pilot_runs WHERE pilot_id=%s AND source=%s AND slot_no=%s",
                (PILOT_ID, source, slot_no),
            )
            if cur.fetchone():
                results.append({"source": source, "slot_no": slot_no, "status": "ALREADY_DONE"})
                continue
        try:
            results.append(collect_source(source, url, slot_no))
        except Exception as exc:
            results.append(
                {
                    "source": source,
                    "slot_no": slot_no,
                    "status": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE result_source_pilot_control SET updated_at=NOW() WHERE pilot_id=%s",
            (PILOT_ID,),
        )
        conn.commit()
    print(json.dumps({"result_source_pilot_id": PILOT_ID, "slot_no": slot_no, "results": results}, default=str))


if __name__ == "__main__":
    main()
