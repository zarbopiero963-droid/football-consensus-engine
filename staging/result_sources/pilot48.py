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
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect
from app.result_validation import upsert_observation
from staging.page_capture import navigate_capture, confirmed_browser_stop, capture_summary
from app.source_resilience import run_source, SourceCircuit, assert_owner, public_state
from app.source_fallback import pilot_fallback_report
from staging.result_sources.worldfootball import (CONTRACT as WORLDFOOTBALL_CONTRACT,
    parse_html as parse_worldfootball_html, parse_document as worldfootball_document)

RESULT_MATCH_CONTRACT = "RESULT_PAIR_DATE_COMPETITION_V1"

BROWSER_API = "https://api.browser-use.com/api/v4/browsers"
PILOT_ID = os.environ.get("RESULT_SOURCE_PILOT_ID", "result-source-pilot-48h-issue20-v1")
ROME = ZoneInfo("Europe/Rome")
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



def canon(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"\b(fc|cf|afc|sc|calcio|club|the)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def exact_label(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def competition_name(row):
    # The explorer publishes a separate competition/country/season label. Keep
    # those literal values; this is no alias from Friendlies to International.
    raw = row.get("competition_name") or row.get("competition") or ""
    if row.get("competition_name"):
        return raw
    label = re.fullmatch(r"(.+) - ([^,]+), (\d{4}(?:/\d{2,4})?)", raw)
    return label.group(1) if label else raw


def _parse_source_date(value: str):
    normalized = value.replace("Sept", "Sep")
    for fmt in ("%d %b %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(normalized, fmt).date()
        except ValueError:
            pass
    return None


def parse_smart_soccer_stats_results(text: str) -> list[dict]:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    date_re = re.compile(r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{4}")
    i = 0
    while i + 7 < len(lines):
        if not date_re.fullmatch(lines[i]):
            i += 1
            continue
        try:
            home = lines[i + 1]
            hg = int(lines[i + 2])
            sep = lines[i + 3]
            ag = int(lines[i + 4])
            away = lines[i + 5]
        except (ValueError, IndexError):
            i += 1
            continue
        if sep != "-":
            i += 1
            continue
        ht_home = ht_away = None
        ht = re.fullmatch(r"\((\d+)\s*-\s*(\d+)\)", lines[i + 6])
        if ht:
            ht_home, ht_away = int(ht.group(1)), int(ht.group(2))
        competition = lines[i + 7] if i + 7 < len(lines) else ""
        source_date = _parse_source_date(lines[i])
        out.append({
            "home_team": home,
            "away_team": away,
            "competition": competition,
            "source_date": source_date,
            "status": "FINISHED",
            "final_home_goals": hg,
            "final_away_goals": ag,
            "ht_home_goals": ht_home,
            "ht_away_goals": ht_away,
            "corners_home": None,
            "corners_away": None,
            "ht_corners_home": None,
            "ht_corners_away": None,
        })
        i += 8
    return out


def parse_worldfootball_results(text: str) -> list[dict]:
    """Legacy text input cannot prove lifecycle; require the published HTML."""
    return parse_worldfootball_html(text)


def result_anchor_rows():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          SELECT DISTINCT ON(match_id)
            match_id,home_team,away_team,league,kickoff_utc
          FROM model_snapshots
          WHERE valid_prematch=TRUE
          ORDER BY match_id,snapshot_timestamp_utc DESC
        """)
        return list(cur.fetchall())


def match_result_anchor(row: dict, anchors: list[dict]):
    source_date = row.get("source_date")
    if source_date is None:
        return None, "DATE_UNRESOLVED"
    home, away = exact_label(row.get("home_team")), exact_label(row.get("away_team"))
    league = exact_label(competition_name(row))
    if not home or not away or home == away:
        return None, "PAIR_UNRESOLVED"
    if not league:
        return None, "COMPETITION_UNRESOLVED"
    native_kickoff = row.get("source_kickoff_utc")
    if native_kickoff is not None and (not isinstance(native_kickoff, datetime) or native_kickoff.tzinfo is None):
        return None, "TIMEZONE_UNRESOLVED"
    candidates = []
    rejected = []
    for anchor in anchors:
        if exact_label(anchor.get("home_team")) != home or exact_label(anchor.get("away_team")) != away:
            continue
        kickoff = anchor.get("kickoff_utc")
        if not isinstance(kickoff, datetime) or kickoff.tzinfo is None:
            rejected.append("ANCHOR_TIME_UNRESOLVED")
            continue
        if native_kickoff is not None:
            if abs((kickoff - native_kickoff).total_seconds()) > 15 * 60:
                rejected.append("KICKOFF_MISMATCH")
                continue
        elif kickoff.astimezone(ROME).date() != source_date:
            rejected.append("DATE_MISMATCH")
            continue
        if not anchor.get("league"):
            rejected.append("ANCHOR_COMPETITION_UNRESOLVED")
            continue
        if exact_label(anchor["league"]) != league:
            rejected.append("COMPETITION_MISMATCH")
            continue
        candidates.append(anchor)
    if len(candidates) == 1:
        status = "EXACT_PAIR_KICKOFF_COMPETITION_DIAGNOSTIC" if native_kickoff is not None else "EXACT_PAIR_DATE_COMPETITION_DIAGNOSTIC"
        return candidates[0], status
    if not candidates:
        return None, rejected[0] if rejected else "NOT_FOUND"
    return None, "AMBIGUOUS"


def result_match(row, anchors, observed_at):
    if row.get("status") not in {"FT", "FINISHED"}:
        return None, "NOT_FINISHED"
    if any(type(row.get(f)) is not int or row[f] < 0 for f in ("final_home_goals", "final_away_goals")):
        return None, "INVALID_FT_SCORE"
    if row.get("source_kickoff_utc") is not None:
        kickoff = row['source_kickoff_utc']
        if not isinstance(kickoff, datetime) or kickoff.tzinfo is None:
            return None, "TIMEZONE_UNRESOLVED"
        end = row.get("source_end_utc")
        if (not isinstance(end, datetime) or end.tzinfo is None or observed_at.tzinfo is None
                or end > observed_at or kickoff >= end):
            return None, "END_NOT_OBSERVED"
    return match_result_anchor(row, anchors)


def persist_pilot_observations(source: str, rows: list[dict], observed_at, raw_sha256: str, *, cursor=None, anchors=None) -> dict:
    if source not in {"smart_soccer_stats", "worldfootball"}:
        return {"parsed": len(rows), "matched": 0, "persisted": 0}
    anchors = result_anchor_rows() if anchors is None else anchors
    matched = persisted = 0
    provider = f"pilot:{source}"
    for row in rows:
        anchor, match_status = result_match(row, anchors, observed_at)
        if anchor is None:
            continue
        matched += 1
        payload = {
            "provider_match_id": row.get("source_match_id") or f"{source}|{row['source_date']}|{canon(row['home_team'])}|{canon(row['away_team'])}",
            "status": row["status"],
            "final_home_goals": row.get("final_home_goals"),
            "final_away_goals": row.get("final_away_goals"),
            "ht_home_goals": row.get("ht_home_goals"),
            "ht_away_goals": row.get("ht_away_goals"),
            "corners_home": row.get("corners_home"),
            "corners_away": row.get("corners_away"),
            "ht_corners_home": row.get("ht_corners_home"),
            "ht_corners_away": row.get("ht_corners_away"),
            "pilot_id": PILOT_ID,
            "source_date": str(row["source_date"]),
            "competition": row.get("competition") or "",
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "match_status": match_status,
            "result_match_contract": RESULT_MATCH_CONTRACT,
            "competition_name": competition_name(row),
            "source_kickoff_raw": row.get("source_kickoff_raw"),
            "source_kickoff_utc": row.get("source_kickoff_utc").isoformat() if row.get("source_kickoff_utc") is not None else None,
            "source_end_raw": row.get("source_end_raw"),
            "source_end_utc": row.get("source_end_utc").isoformat() if row.get("source_end_utc") is not None else None,
            "lifecycle_evidence": row.get("lifecycle_evidence"),
            "timezone_status": row.get("timezone_status", "TIMEZONE_UNRESOLVED"),
            "source_parser_contract": row.get("source_parser_contract"),
            "source_competition_id": row.get("source_competition_id"),
            "source_gender": row.get("source_gender"),
            "source_competition_type": row.get("source_competition_type"),
            "anchor_kickoff_utc": anchor["kickoff_utc"].isoformat(),
            "raw_page_sha256": raw_sha256,
            "production_eligible": False,
        }
        inserted = upsert_observation(
            anchor["match_id"],
            provider,
            observed_at,
            row.get("source_kickoff_utc") or anchor["kickoff_utc"],
            payload,
            cursor=cursor,
        )
        persisted += int(inserted)
    return {"parsed": len(rows), "matched": matched, "persisted": persisted}



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
    return run_source(connect, "result", PILOT_ID, source, url, slot_no,
        lambda claim: _collect_source(source, url, slot_no, claim),
        lambda sid: confirmed_browser_stop(browser_api, BROWSER_API, sid))


def _collect_source(source, url, slot_no, claim):
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
        raise RuntimeError(f"Browser create failed HTTP status={status}")
    sid, cdp = created["id"], created["cdpUrl"]
    html = b""
    shot = b""
    body_text = ""
    final_url = url
    http_status = None
    page_errors = []
    circuit = SourceCircuit(connect, claim["key"])
    try:
        circuit.register_browser(claim, sid)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            page.on("pageerror", lambda exc: page_errors.append(str(exc)[:300]))
            parser = {"smart_soccer_stats": parse_smart_soccer_stats_results}.get(source)
            html_parser = parse_worldfootball_html if source == "worldfootball" else None
            capture = navigate_capture(page, url, parser,
                                       html_parser=html_parser,
                                       evidence_kind="normalized_result_rows" if parser or html_parser else "capability_only")
            http_status = capture.audit["http_status"]
            final_url = page.url
            body_text = capture.raw_text
            html = capture.html
            shot = page.screenshot(full_page=True, timeout=30000)
            browser.close()
    finally:
        sess = confirmed_browser_stop(browser_api, BROWSER_API, sid)
        circuit.clear_stopped_browser(claim)

    rows_found, capabilities = inspect_source(source, body_text, html.decode("utf-8", "replace"))
    normalized_rows = capture.parsed if capture.accepted else []
    parser_audit = None
    if source == "worldfootball":
        try:
            parser_audit = worldfootball_document(html)["audit"]
        except Exception as exc:
            # Audit must not throw away the raw already captured on a parser
            # fault (including the bounded DOM/HTML limits).
            normalized_rows = []
            capture.audit["state"] = "PARSER_ERROR"
            capture.audit["parser_error_class"] = type(exc).__name__
            parser_audit = {"contract": WORLDFOOTBALL_CONTRACT,
                "state": "PARSER_ERROR", "rows_accepted": 0,
                "error_class": type(exc).__name__}
    observed_at = datetime.fromisoformat(capture.audit["captured_at_utc"])
    fallback_candidates = []
    anchors = result_anchor_rows() if normalized_rows else []
    match_rejections = Counter()
    for row in normalized_rows:
        anchor, match_status = result_match(row, anchors, observed_at)
        if anchor is not None:
            fallback_candidates.append({**row, 'match_id': anchor['match_id'],
                'provider': f'pilot:{source}', 'channel': 'PILOT',
                'production_eligible': False, 'match_status': match_status,
                'result_match_contract': RESULT_MATCH_CONTRACT,
                'observed_at_utc': observed_at.isoformat(), 'raw_page_sha256': sha(html),
                'http_status': http_status, 'capture_state': capture.audit['state']})
        else:
            match_rejections[match_status] += 1
    observation_stats = {"parsed": len(normalized_rows), "matched": 0, "persisted": 0}
    manifest = {
        "source_resilience": public_state(claim["state"]),
        "pilot_id": PILOT_ID,
        "source": source,
        "slot_no": slot_no,
        "page_errors": page_errors[:20],
        "rows_found": rows_found,
        "capabilities": capabilities,
        "raw_html_bytes": len(html),
        "screenshot_bytes": len(shot),
        "normalized_observations": observation_stats,
        "fallback_candidates": json.loads(json.dumps(fallback_candidates, default=str)),
        "page_capture": capture.audit,
        "result_match_audit": {"contract": RESULT_MATCH_CONTRACT,
            "rows_checked": len(normalized_rows), "matched": len(fallback_candidates),
            "rejection_counts": dict(match_rejections), "production_safe": False},
    }
    if source == "worldfootball":
        manifest["result_parser_audit"] = parser_audit

    with connect() as conn, conn.cursor() as cur:
        assert_owner(cur, claim)
        observation_stats = persist_pilot_observations(source, normalized_rows, observed_at, sha(html), cursor=cur, anchors=anchors)
        manifest["normalized_observations"] = observation_stats
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
        "normalized_observations": observation_stats,
        "fallback_candidates": fallback_candidates,
        "page_capture": capture_summary(capture.audit),
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
                    "error_class": type(exc).__name__,
                }
            )
    for outcome in results:
        outcome.pop('fallback_candidates', None)
    with connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT manifest FROM result_source_pilot_runs WHERE pilot_id=%s AND slot_no=%s',
                    (PILOT_ID, slot_no))
        candidates = [row for run in cur.fetchall() for row in run['manifest'].get('fallback_candidates', [])]
        fallback = pilot_fallback_report(candidates)
        if any(outcome.get('status') == 'CIRCUIT_BUSY' for outcome in results):
            fallback['status'] = 'DEFERRED_CONCURRENT_ACQUISITION'
        else:
            cur.execute('''INSERT INTO runtime_state(key,value) VALUES (%s,%s::jsonb)
                         ON CONFLICT(key) DO NOTHING''',
                        (f'result_source_fallback:v1:{PILOT_ID}:{slot_no}', json.dumps(fallback, default=str)))
        cur.execute(
            "UPDATE result_source_pilot_control SET updated_at=NOW() WHERE pilot_id=%s",
            (PILOT_ID,),
        )
        conn.commit()
    print(json.dumps({"result_source_pilot_id": PILOT_ID, "slot_no": slot_no, "results": results,
                      "fallback": fallback}, default=str))


if __name__ == "__main__":
    main()
