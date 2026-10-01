#!/usr/bin/env python3
"""Append-only prospective snapshot of FootInsights pre-match 1X2 and Pinnacle moneylines.

Never overwrites an existing file. Never writes a result into a model snapshot.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))
from app.persist import guarded, set_runtime_state, upsert_snapshots  # noqa: E402
from app.identities import category_flags
from app.market_reference import audit_market_candidates, exact_market_match, MATCH_CONTRACT  # noqa: E402

SCRAPER_VERSION = "prospective-ledger-3"
STRATEGY_FILE = os.path.join(ROOT, "strategies_preregistered.json")
UA = "Mozilla/5.0 (compatible; prospective-ledger/1.0; prematch-audit)"
FI_INDEXES = [
    "https://footinsights.com/predictions/",
    "https://footinsights.com/predictions/tomorrow/",
    "https://footinsights.com/predictions/weekend/",
]
PIN_LEAGUES = "https://guest.api.arcadia.pinnacle.com/0.1/sports/29/leagues?brandId=0"
PIN_FALLBACK_BATCH_SIZE = 12
BRIDGE_503_BACKOFF_SECONDS = (3, 10, 30)

def utcnow():
    return datetime.now(timezone.utc)

def stamp(dt=None):
    dt = dt or utcnow()
    return dt.strftime("%Y%m%dT%H%M%SZ")

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def write_new(path: str, data: bytes):
    """Write only if the path does not exist. Never overwrite."""
    if os.path.exists(path):
        raise FileExistsError(path)
    ensure_dir(os.path.dirname(path))
    with open(path, "xb") as f:
        f.write(data)
    return path

def append_text(path: str, text: str):
    ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)

def http_get(url: str, timeout=45, headers=None):
    h = {"User-Agent": UA, "Accept": "text/html,application/json"}
    if headers:
        h.update(headers)
    req = Request(url, headers=h)
    try:
        with urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, body, dict(r.headers)
    except HTTPError as e:
        body = e.read() if e.fp else b""
        return e.code, body, {}
    except URLError as e:
        raise RuntimeError(f"urlerror {url} {e}") from e


def bridge_response(req, *, timeout, audit):
    """Retry only a received 503, never auth, stop failure or unknown delivery."""
    for attempt in range(len(BRIDGE_503_BACKOFF_SECONDS) + 1):
        try:
            response = urlopen(req, timeout=timeout)
        except HTTPError as exc:
            audit.append({'attempt': attempt + 1, 'http_status': exc.code,
                          'received_at_utc': utcnow().isoformat()})
            exc.close()
            if exc.code != 503 or attempt == len(BRIDGE_503_BACKOFF_SECONDS):
                raise
            time.sleep(BRIDGE_503_BACKOFF_SECONDS[attempt])
        else:
            audit.append({'attempt': attempt + 1, 'http_status': getattr(response, 'status', None),
                          'received_at_utc': utcnow().isoformat()})
            return response


def fetch_pinnacle_leagues_via_bridge(audit=None):
    token = os.environ.get("COLLECTOR_BRIDGE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("COLLECTOR_BRIDGE_TOKEN is not configured")
    base = os.environ.get(
        "BROWSER_BRIDGE_URL",
        "https://football-consensus-browser-bridge.onrender.com",
    ).rstrip("/")
    req = Request(
        base + "/internal/pinnacle/leagues",
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "X-Collector-Token": token,
        },
    )
    with bridge_response(req, timeout=75, audit=audit if audit is not None else []) as r:
        payload = json.loads(r.read().decode("utf-8"))
    if payload.get("http_status") != 200 or payload.get("truncated"):
        raise RuntimeError(
            f"bridge pinnacle leagues failed status={payload.get('http_status')} truncated={payload.get('truncated')}"
        )
    body = str(payload.get("body") or "").encode("utf-8")
    if not body:
        raise RuntimeError("bridge pinnacle leagues returned empty body")
    return 200, body


def valid_pinnacle_response(pair):
    if not pair or pair[0] != 200 or not pair[1]:
        return False
    try:
        def invalid_constant(value): raise ValueError('nonfinite JSON constant')
        rows = json.loads(pair[1],parse_constant=invalid_constant)
        return isinstance(rows, list) and all(isinstance(r, dict) for r in rows)
    except (ValueError, UnicodeDecodeError):
        return False


def fetch_pinnacle_batch_via_bridge(league_ids):
    if (not league_ids or len(league_ids) > PIN_FALLBACK_BATCH_SIZE or
            len(set(league_ids)) != len(league_ids) or
            any(type(x) is not int or not 1 <= x <= 999999999 for x in league_ids)):
        raise ValueError('invalid fallback league IDs')
    token = os.environ.get('COLLECTOR_BRIDGE_TOKEN', '').strip()
    if not token: raise RuntimeError('COLLECTOR_BRIDGE_TOKEN is not configured')
    base = os.environ.get('BROWSER_BRIDGE_URL',
        'https://football-consensus-browser-bridge.onrender.com').rstrip('/')
    req = Request(base+'/internal/pinnacle/league-batch?ids='+','.join(map(str,league_ids)),
        headers={'User-Agent':UA,'Accept':'application/json','X-Collector-Token':token})
    requests_audit = []
    with bridge_response(req, timeout=210, audit=requests_audit) as response:
        # The 20 MiB raw batch can grow when JSON strings are escaped in the envelope.
        raw=response.read(45*1024*1024+1)
        if len(raw) > 45*1024*1024: raise RuntimeError('fallback response too large')
        payload = json.loads(raw)
    if payload.get('session_stopped') is not True or payload.get('transport') != 'BROWSER_JSON_DOCUMENT':
        raise RuntimeError('unconfirmed browser transport/stop')
    items = payload.get('items')
    if (not isinstance(items,list) or any(not isinstance(x,dict) or type(x.get('id')) is not int for x in items) or
            sorted(x['id'] for x in items) != sorted(league_ids)):
        raise RuntimeError('fallback league identity mismatch')
    out = []
    for item in items:
        row = {'id':item['id'],'err':item.get('error'),'transport':'browser_bridge_fallback',
               'captured_at':utcnow(),'matchups':None,'markets':None,'response_audit':{},
               'bridge_request_audit': requests_audit}
        if not row['err']:
            try:
                for kind,suffix in (('matchups','matchups?brandId=0'),('markets','markets/straight')):
                    d=item[kind];body=d['body'].encode('utf-8')
                    url=f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{item['id']}/{suffix}"
                    if (d.get('http_status') != 200 or d.get('truncated') is not False or
                            d.get('final_url') != url or d.get('requested_url') != url or
                            d.get('bytes_utf8') != len(body) or len(body) > 5*1024*1024 or
                            d.get('sha256') != sha256_bytes(body) or not parse_iso(d.get('captured_at_utc')) or
                            not valid_pinnacle_response((200,body))):
                        raise ValueError('invalid complete fallback document')
                    row[kind]=(200,body)
                    row['response_audit'][kind]={k:d[k] for k in ('captured_at_utc','bytes_utf8','sha256')}
            except (KeyError,ValueError,TypeError,AttributeError) as exc:
                row['err']='invalid fallback document:'+type(exc).__name__
        out.append(row)
    return out

def strip_accents(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))

def canon(s: str) -> str:
    s = strip_accents(s).lower().replace("&", " and ").replace("'", "")
    s = s.replace(".", " ")
    s = re.sub(r"\b(fc|cf|sc|ac|afc|ud|cd|sd|fk|sk|sv|rc|as|ss|us|osc)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def tokens(s: str):
    return {t for t in canon(s).split() if len(t) > 1}

def name_score(a, b):
    na, nb = canon(a), canon(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 3
    if na in nb or nb in na:
        return 2
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0
    inter = ta & tb
    if not inter:
        return 0
    j = len(inter) / len(ta | tb)
    if j >= 0.6:
        return 1
    return 0

def women_or_youth(s: str) -> str:
    return category_flags(s)

def american_to_decimal(price):
    a = float(price)
    if a >= 100:
        return round(1 + a / 100.0, 4)
    if a <= -100:
        return round(1 + 100.0 / (-a), 4)
    if a > 1.01:
        return round(a, 4)
    return None

def fair_from_odds(h, d, a):
    inv = [1 / h, 1 / d, 1 / a]
    s = sum(inv)
    return [x / s for x in inv], s

def parse_iso(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # No verified source timezone: never invent UTC.
    return dt.astimezone(timezone.utc)

def extract_urls(html: str):
    urls = set(re.findall(r"https://footinsights\.com/predictions/([a-z0-9-]+)/", html))
    urls.update(re.findall(r'href="/predictions/([a-z0-9-]+)/"', html))
    return urls

def slug_date(slug: str):
    m = re.search(r"(20\d{2}-\d{2}-\d{2})$", slug)
    return m.group(1) if m else None

def parse_fi_html(html: str, url: str):
    events = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        objs = data if isinstance(data, list) else [data]
        for o in objs:
            if isinstance(o, dict) and o.get("@type") == "SportsEvent":
                events.append(o)
    # The published timestamp must belong to one unambiguous source event.
    # Do not choose the first event when a page contains conflicting fixtures.
    ev = events[0] if len(events) == 1 else {}
    home = ((ev.get("homeTeam") or {}).get("name")) or ""
    away = ((ev.get("awayTeam") or {}).get("name")) or ""
    league = ((ev.get("organizer") or {}).get("name")) or ""
    kick = parse_iso(ev.get("startDate"))
    widths = [float(x) for x in re.findall(r'class="model" style="width:\s*([0-9.]+)%"', html)]
    bars = {
        "p1": re.search(r'class="p1" style="width:\s*([0-9.]+)%"', html),
        "px": re.search(r'class="px" style="width:\s*([0-9.]+)%"', html),
        "p2": re.search(r'class="p2" style="width:\s*([0-9.]+)%"', html),
    }
    raw = None
    source = ""
    if len(widths) >= 3 and 90 <= sum(widths[:3]) <= 110:
        raw = widths[:3]
        source = "model-bar-width"
    elif all(bars.values()):
        raw = [float(bars["p1"].group(1)), float(bars["px"].group(1)), float(bars["p2"].group(1))]
        source = "prob-bar-width"
    legend = re.findall(r'class="pl"[^>]*>.*?<b[^>]*>(\d+)%</b>', html, re.S)
    predicted = None
    m = re.search(r"predicted\s+(\d{4}-\d{2}-\d{2})", html)
    if m:
        predicted = m.group(1)
    book = "Bet365" if "odds by Bet365" in html else ""
    # three decimal odds in the model-vs-market block, in 1/X/2 order
    page_odds = re.findall(r'class="mvm-odds"[^>]*>@\s*([0-9.]+)', html)
    page_odds = page_odds[:3] if len(page_odds) >= 3 else []
    return {
        "home": home,
        "away": away,
        "league": league,
        "kickoff": kick,
        "kickoff_raw": ev.get("startDate") or "",
        "timezone_status": "EXPLICIT_OFFSET" if kick else "TIMEZONE_UNRESOLVED",
        "source_time_evidence": "SportsEvent.startDate" if kick else "",
        "raw": raw,
        "prob_source": source,
        "legend_pct": legend[:3],
        "predicted_date": predicted,
        "page_book": book,
        "page_odds": page_odds,
        "event_status": ev.get("eventStatus") or "",
        "url": url,
    }

def load_strategy_hash():
    return sha256_file(STRATEGY_FILE)

def main():
    ensure_dir(os.path.join(ROOT, "var"))
    lock_handle = open(os.path.join(ROOT, "var", "collect.lock"), "a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("collect already running; no rows written")
        return
    now = utcnow()
    run_id = stamp(now)
    day = now.strftime("%Y-%m-%d")
    errors = []
    created = []  # (path, sha, role)

    def save(path, data: bytes, role):
        write_new(path, data)
        digest = sha256_bytes(data)
        created.append((path, digest, role, len(data)))
        return digest

    run_dir_fi = os.path.join(ROOT, "raw", "footinsights", day, run_id)
    run_dir_pin = os.path.join(ROOT, "raw", "pinnacle", day, run_id)
    run_dir_snap = os.path.join(ROOT, "snapshots", day, run_id)
    ensure_dir(run_dir_fi)
    ensure_dir(run_dir_pin)
    ensure_dir(run_dir_snap)

    # --- FootInsights index ---
    slugs = set()
    for url in FI_INDEXES:
        try:
            code, body, _ = http_get(url)
        except Exception as e:
            errors.append({"source": "footinsights", "url": url, "error": str(e), "at": now.isoformat()})
            continue
        path = os.path.join(run_dir_fi, "index_" + re.sub(r"[^a-z0-9]+", "_", url.split("/predictions/")[-1] or "today").strip("_") + ".html")
        if code != 200 or not body:
            errors.append({"source": "footinsights", "url": url, "status": code, "at": now.isoformat()})
            save(path, body or b"", "fi-index-error")
            continue
        save(path, body, "fi-index")
        slugs |= extract_urls(body.decode("utf-8", "replace"))

    # keep slugs whose date is today or later; drop obvious past pages
    future_slugs = []
    for slug in sorted(slugs):
        d = slug_date(slug)
        if d and d < day:
            continue
        future_slugs.append(slug)

    def fetch_match(slug):
        url = f"https://footinsights.com/predictions/{slug}/"
        try:
            code, body, _ = http_get(url)
            return slug, url, code, body, None, utcnow()
        except Exception as e:
            return slug, url, 0, b"", str(e), utcnow()

    pages = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_match, s) for s in future_slugs]
        for fut in as_completed(futs):
            pages.append(fut.result())

    model_rows = []
    n_fi_pages = 0
    n_invalid = 0
    n_no_prob = 0
    for slug, url, code, body, err, captured_at in sorted(pages, key=lambda x: x[0]):
        if err or code != 200 or not body:
            errors.append({"source": "footinsights", "url": url, "status": code, "error": err, "at": utcnow().isoformat()})
            if body:
                save(os.path.join(run_dir_fi, slug + ".error.html"), body, "fi-match-error")
            continue
        digest = save(os.path.join(run_dir_fi, slug + ".html"), body, "fi-match")
        n_fi_pages += 1
        parsed = parse_fi_html(body.decode("utf-8", "replace"), url)
        kick = parsed["kickoff"]
        valid = bool(kick and kick > captured_at and parsed["raw"])
        reason = ""
        if not parsed["raw"]:
            reason = "NO_PROBABILITY"
            n_no_prob += 1
            valid = False
        elif not kick:
            reason = "NO_KICKOFF"
            valid = False
        elif kick <= captured_at:
            reason = "INVALID_PREMATCH"
            n_invalid += 1
            valid = False
        minutes = (kick - captured_at).total_seconds() / 60.0 if kick else None
        raw = parsed["raw"]
        if raw:
            s = sum(raw)
            norm = [x / s for x in raw] if s else None
        else:
            norm = None
        pick = None
        if norm:
            pick = max(range(3), key=lambda i: norm[i])
        labels = ["1", "X", "2"]
        model_rows.append({
            "slug": slug,
            "url": url,
            "sha256": digest,
            "valid": valid,
            "invalid_reason": reason,
            "parsed": parsed,
            "captured_at": captured_at,
            "norm": norm,
            "pick": pick,
            "pick_label": labels[pick] if pick is not None else "",
            "minutes": minutes,
        })

    # --- Pinnacle ---
    pin_headers = {"Accept": "application/json", "Referer": "https://www.pinnacle.com/"}
    try:
        code, body, _ = http_get(PIN_LEAGUES, headers=pin_headers)
    except Exception as e:
        code, body = 0, b""
        errors.append({"source": "pinnacle", "url": PIN_LEAGUES, "error": str(e), "at": utcnow().isoformat()})
    leagues = []
    direct_error = None
    pin_leagues_requests = []
    pin_leagues_failure = None
    pin_leagues_transport = "direct"
    if code == 200 and body:
        try:
            leagues = json.loads(body.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            direct_error = f"bad direct leagues payload: {exc}"
    else:
        direct_error = f"direct leagues status={code}"

    if not isinstance(leagues, list) or not leagues:
        try:
            bridge_code, bridge_body = fetch_pinnacle_leagues_via_bridge(audit=pin_leagues_requests)
            leagues = json.loads(bridge_body.decode())
            if not isinstance(leagues, list) or not leagues:
                raise RuntimeError("bridge leagues payload is not a non-empty list")
            code, body = bridge_code, bridge_body
            pin_leagues_transport = "browser_bridge_fallback"
        except Exception as exc:
            pin_leagues_transport = "failed"
            pin_leagues_failure = {'direct_http_status': code,
                                   'bridge_error_type': type(exc).__name__,
                                   'bridge_http_status': exc.code if isinstance(exc, HTTPError) else None}
            errors.append({
                "source": "pinnacle",
                "url": PIN_LEAGUES,
                "status": code,
                "error": f"{direct_error}; bridge fallback failed: {type(exc).__name__}: {exc}",
                "at": utcnow().isoformat(),
            })
            leagues = []

    save(os.path.join(run_dir_pin, 'leagues_transport_audit.json'),
         json.dumps({'transport': pin_leagues_transport, 'bridge_requests': pin_leagues_requests,
                     'failure': pin_leagues_failure}, indent=2).encode(), 'pin-leagues-transport-audit')

    if leagues and body:
        save(os.path.join(run_dir_pin, "leagues.json"), body, "pin-leagues")

    league_ids = [L["id"] for L in leagues if L.get("matchupCount")]

    def fetch_league(lid):
        out = {"id": lid, "matchups": None, "markets": None, "err": None, "transport": "direct"}
        mu = f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{lid}/matchups?brandId=0"
        mk = f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{lid}/markets/straight"
        try:
            c1, b1, _ = http_get(mu, headers=pin_headers)
            c2, b2, _ = http_get(mk, headers=pin_headers)
            out.update(matchups=(c1, b1), markets=(c2, b2), captured_at=utcnow())
        except Exception as e:
            out["err"] = str(e)
        return out

    pin_matchups = []  # dicts ready to match
    league_name = {L["id"]: L.get("name") for L in leagues}
    league_items = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_league, lid) for lid in league_ids]
        for fut in as_completed(futs):
            item = fut.result(); league_items[item['id']] = item
    transport_audit = []
    failed_ids = []
    for lid in league_ids:
        item=league_items[lid]
        valid = not item['err'] and all(valid_pinnacle_response(item[k]) for k in ('matchups','markets'))
        audit={'id':lid,'direct_error':item['err'],'direct_complete':valid}
        for kind in ('matchups','markets'):
            pair=item[kind]
            audit[kind+'_direct_status']=pair[0] if pair else None
            audit[kind+'_direct_sha256']=sha256_bytes(pair[1]) if pair else None
        transport_audit.append(audit)
        if not valid: failed_ids.append(lid)
    batches=[failed_ids[i:i+PIN_FALLBACK_BATCH_SIZE] for i in range(0,len(failed_ids),PIN_FALLBACK_BATCH_SIZE)]
    # At most two remote browsers at once; each batch reuses one managed session.
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures={ex.submit(fetch_pinnacle_batch_via_bridge,batch):batch for batch in batches}
        for future in as_completed(futures):
            batch=futures[future]
            try:
                for item in future.result(): league_items[item['id']]=item
            except Exception as exc:
                for lid in batch:
                    league_items[lid]={'id':lid,'err':'bridge fallback failed:'+type(exc).__name__,
                                      'transport':'failed','matchups':None,'markets':None,
                                      'failed_http_status': exc.code if isinstance(exc, HTTPError) else None}
    for audit in transport_audit:
        item=league_items[audit['id']]
        audit.update(final_transport=item['transport'],final_error=item['err'])
        if item.get('response_audit'):audit['browser_responses']=item['response_audit']
        if item.get('bridge_request_audit'):audit['bridge_requests']=item['bridge_request_audit']
        if item.get('failed_http_status'):audit['failed_http_status']=item['failed_http_status']
        if item.get('captured_at'):audit['collector_acquired_at_utc']=item['captured_at'].isoformat()
    if transport_audit:
        save(os.path.join(run_dir_pin,'transport_audit.json'),json.dumps(transport_audit,indent=2).encode(),
             'pin-transport-audit')
    for lid in league_ids:
        item = league_items[lid]
        if item["err"]:
            errors.append({"source": "pinnacle", "league": lid, "error": item["err"], "at": utcnow().isoformat()})
            continue
        (c1, b1) = item["matchups"]
        (c2, b2) = item["markets"]
        if c1 == 200 and b1:
            save(os.path.join(run_dir_pin, f"matchups_{lid}.json"), b1, "pin-matchups")
        else:
            errors.append({"source": "pinnacle", "league": lid, "what": "matchups", "status": c1, "at": utcnow().isoformat()})
            continue
        if c2 == 200 and b2:
            save(os.path.join(run_dir_pin, f"markets_{lid}.json"), b2, "pin-markets")
        else:
            errors.append({"source": "pinnacle", "league": lid, "what": "markets", "status": c2, "at": utcnow().isoformat()})
            b2 = b""
        try:
            mus = json.loads(b1.decode())
            mks = json.loads(b2.decode()) if b2 else []
        except json.JSONDecodeError:
            errors.append({"source": "pinnacle", "league": lid, "error": "bad json", "at": utcnow().isoformat()})
            continue
        ml = {}
        duplicate_markets = set()
        for m in mks:
            if m.get("type") != "moneyline" or m.get("period") != 0 or m.get("isAlternate"):
                continue
            if m.get("key") not in (None, "s;0;m") and not str(m.get("key", "")).endswith(";m"):
                continue
            prices = {p.get("designation"): p.get("price") for p in m.get("prices") or []}
            if not all(k in prices for k in ("home", "draw", "away")):
                continue
            matchup_id = m.get("matchupId")
            if matchup_id in ml:
                duplicate_markets.add(matchup_id)
            ml[matchup_id] = m
        for mu in mus:
            if mu.get("type") not in (None, "matchup"):
                continue
            if mu.get("parentId") or mu.get("parent") or mu.get("isLive"):
                continue
            start = parse_iso(mu.get("startTime"))
            if not start or start <= item["captured_at"]:
                continue
            parts = mu.get("participants") or []
            home = next((p.get("name") for p in parts if p.get("alignment") == "home"), None)
            away = next((p.get("name") for p in parts if p.get("alignment") == "away"), None)
            if not home or not away:
                continue
            if mu.get("id") in duplicate_markets:
                continue
            market = ml.get(mu.get("id"))
            if not market or market.get("status") != "open":
                continue
            prices = {p["designation"]: p["price"] for p in market["prices"]}
            try:
                dec = [american_to_decimal(prices["home"]), american_to_decimal(prices["draw"]), american_to_decimal(prices["away"])]
            except (TypeError, ValueError, OverflowError):
                dec = []
            if len(dec) != 3 or not all(o is not None and math.isfinite(o) and o > 1 for o in dec):
                errors.append({"source": "pinnacle", "league": lid, "what": "invalid_prices", "at": utcnow().isoformat()})
                continue
            fair, over = fair_from_odds(*dec)
            pin_matchups.append({
                "matchup_id": mu.get("id"),
                "captured_at": item["captured_at"],
                "league_id": lid,
                "league": league_name.get(lid) or "",
                "home": home,
                "away": away,
                "start": start,
                "dec": dec,
                "fair": fair,
                "overround": over,
                "american": [prices["home"], prices["draw"], prices["away"]],
                "market_version": market.get("version"),
                "cutoff": market.get("cutoffAt"),
                "raw_file": f"markets_{lid}.json",
                "transport": item['transport'],
            })
    # --- match ---
    manual = []
    joined = 0
    unmatched = 0
    ledger = []
    match_audits = []
    match_status_counts = {}
    candidate_rejections = {}
    # Use a single odds batch timestamp: end of pinnacle pull is more honest.
    odds_stamp = utcnow()

    for row in model_rows:
        if not row["valid"]:
            continue
        p = row["parsed"]
        kick = p["kickoff"]
        cands = [o for o in pin_matchups if exact_market_match(p, o, o["captured_at"])]
        audit = audit_market_candidates(p, pin_matchups)
        match_status_counts[audit['status']] = match_status_counts.get(audit['status'], 0) + 1
        for reason, count in audit['candidate_first_rejections'].items():
            candidate_rejections[reason] = candidate_rejections.get(reason, 0) + count
        match_audits.append({'match_id': 'fi:' + row['slug'],
                             'model_raw_sha256': row['sha256'],
                             'home': p['home'], 'away': p['away'], 'league': p['league'],
                             'kickoff_utc': kick.isoformat(), **audit})
        chosen = None
        if len(cands) == 1:
            chosen = cands[0]
            joined += 1
        elif cands:
            manual.append({
                "slug": row["slug"], "fi": f"{p['home']} vs {p['away']}",
                "kickoff": kick.isoformat(), "reason": "ambiguous_exact_pair",
                "cands": [f"{o['home']} vs {o['away']} ({o['league']})" for o in cands[:4]],
            })
        else:
            unmatched += 1

        norm = row["norm"]
        pick = row["pick"]
        labels = ["1", "X", "2"]
        rec = {
            "match_id": "fi:" + row["slug"],
            "source_match_id": row["slug"],
            "league": p["league"],
            "country": "",
            "season": "",
            "kickoff_utc": kick.isoformat(),
            "kickoff_raw": p["kickoff_raw"],
            "timezone_status": p["timezone_status"],
            "source_time_evidence": p["source_time_evidence"],
            "home_team": p["home"],
            "away_team": p["away"],
            "home_canon": canon(p["home"]),
            "away_canon": canon(p["away"]),
            "team_flags": ",".join(sorted(set(filter(None,
                (women_or_youth(p["home"]) + "," + women_or_youth(p["away"])).split(","))))),
            "snapshot_timestamp_utc": row["captured_at"].isoformat(),
            "minutes_before_kickoff": round(row["minutes"], 2) if row["minutes"] is not None else "",
            "model_version": "not-exposed-on-page",
            "prediction_published_date": p["predicted_date"] or "",
            "scraper_version": SCRAPER_VERSION,
            "strategy_set_id": "prereg-2026-09-29",
            "p_home_raw": round(p["raw"][0], 6),
            "p_draw_raw": round(p["raw"][1], 6),
            "p_away_raw": round(p["raw"][2], 6),
            "p_home_normalized": round(norm[0], 6),
            "p_draw_normalized": round(norm[1], 6),
            "p_away_normalized": round(norm[2], 6),
            "prob_parse_source": p["prob_source"],
            "model_pick": labels[pick],
            "model_pick_probability": round(norm[pick], 6),
            "source_url": row["url"],
            "sha256_model_raw": row["sha256"],
            "page_book": p["page_book"],
            "page_odds_home": p["page_odds"][0] if p["page_odds"] else "",
            "page_odds_draw": p["page_odds"][1] if len(p["page_odds"]) > 1 else "",
            "page_odds_away": p["page_odds"][2] if len(p["page_odds"]) > 2 else "",
            "valid_prematch": 1,
            "analysis_eligible": 0,
            "market_match_audit": json.dumps(audit, separators=(',', ':')),
        }
        if chosen:
            fair = chosen["fair"]
            dec = chosen["dec"]
            edges = [norm[i] - fair[i] for i in range(3)]
            fav = max(range(3), key=lambda i: fair[i])
            rec.update({
                "pin_matchup_id": chosen["matchup_id"],
                "pin_league": chosen["league"],
                "pin_home": chosen["home"],
                "pin_away": chosen["away"],
                "pin_snapshot_timestamp_utc": chosen["captured_at"].isoformat(),
                "pin_kickoff_utc": chosen["start"].isoformat(),
                "odds_home": dec[0],
                "odds_draw": dec[1],
                "odds_away": dec[2],
                "american_home": chosen["american"][0],
                "american_draw": chosen["american"][1],
                "american_away": chosen["american"][2],
                "fair_home": round(fair[0], 6),
                "fair_draw": round(fair[1], 6),
                "fair_away": round(fair[2], 6),
                "overround": round(chosen["overround"], 6),
                "pin_favorite": labels[fav],
                "pin_favorite_probability": round(fair[fav], 6),
                "pin_favorite_odds": dec[fav],
                "edge_home": round(edges[0], 6),
                "edge_draw": round(edges[1], 6),
                "edge_away": round(edges[2], 6),
                "picked_odds": dec[pick],
                "picked_pin_fair": round(fair[pick], 6),
                "picked_edge": round(edges[pick], 6),
                "same_as_favorite": int(pick == fav),
                "market_version": chosen["market_version"],
                "sha256_odds_raw": sha256_file(os.path.join(run_dir_pin, chosen["raw_file"])),
                "odds_raw_file": chosen["raw_file"],
                "analysis_eligible": 1,
                "name_score_note": MATCH_CONTRACT,
                "pin_transport": chosen['transport'],
            })
        else:
            rec.update({
                "pin_matchup_id": "",
                "pin_league": "",
                "pin_home": "",
                "pin_away": "",
                "pin_snapshot_timestamp_utc": "",
                "pin_kickoff_utc": "",
                "odds_home": "",
                "odds_draw": "",
                "odds_away": "",
                "american_home": "",
                "american_draw": "",
                "american_away": "",
                "fair_home": "",
                "fair_draw": "",
                "fair_away": "",
                "overround": "",
                "pin_favorite": "",
                "pin_favorite_probability": "",
                "pin_favorite_odds": "",
                "edge_home": "",
                "edge_draw": "",
                "edge_away": "",
                "picked_odds": "",
                "picked_pin_fair": "",
                "picked_edge": "",
                "same_as_favorite": "",
                "market_version": "",
                "sha256_odds_raw": "",
                "odds_raw_file": "",
                "name_score_note": "unmatched_or_review",
                "pin_transport": "",
            })
        # result columns exist but stay empty. Filling them later is a different file.
        rec.update({
            "final_home_goals": "",
            "final_away_goals": "",
            "result_1x2": "",
            "match_status": "scheduled",
            "result_timestamp_utc": "",
            "result_source": "",
            "correct": "",
            "profit_flat_1": "",
        })
        ledger.append(rec)

    # invalid rows, stored aside, not in the primary ledger
    invalid_rows = []
    for row in model_rows:
        if row["valid"]:
            continue
        p = row["parsed"]
        invalid_rows.append({
            "match_id": "fi:" + row["slug"],
            "source_url": row["url"],
            "reason": row["invalid_reason"],
            "kickoff_utc": p["kickoff"].isoformat() if p["kickoff"] else "",
            "home_team": p["home"],
            "away_team": p["away"],
            "snapshot_timestamp_utc": row["captured_at"].isoformat(),
            "sha256_model_raw": row["sha256"],
        })

    fields = list(ledger[0].keys()) if ledger else []
    ledger_path = os.path.join(run_dir_snap, "prospective_ledger_slice.csv")
    if ledger:
        with open(ledger_path, "x", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(ledger)
        created.append((ledger_path, sha256_file(ledger_path), "ledger-slice", os.path.getsize(ledger_path)))

    grow = os.path.join(ROOT, "prospective_ledger.csv")
    existing = set()
    grow_fields = fields
    if os.path.exists(grow) and os.path.getsize(grow):
        with open(grow, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            grow_fields = reader.fieldnames or fields
            for prev in reader:
                existing.add((prev.get("match_id"), prev.get("snapshot_timestamp_utc"), prev.get("sha256_model_raw")))
    fresh = []
    for rec in ledger:
        key = (rec["match_id"], rec["snapshot_timestamp_utc"], rec["sha256_model_raw"])
        if key in existing:
            continue
        existing.add(key)
        fresh.append(rec)
    skipped = len(ledger) - len(fresh)
    ledger = fresh
    new_file = not os.path.exists(grow) or os.path.getsize(grow) == 0
    with open(grow, "a", newline="", encoding="utf-8") as f:
        # Preserve an existing CSV header/column order without rewriting history.
        # New audit columns still live in the versioned slice and DB payload.
        w = csv.DictWriter(f, fieldnames=grow_fields, extrasaction="ignore")
        if new_file and fields:
            w.writeheader()
        if ledger:
            w.writerows(ledger)

    if manual:
        mp = os.path.join(run_dir_snap, "manual_review.csv")
        with open(mp, "x", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["slug", "fi", "kickoff", "reason", "cands"])
            w.writeheader()
            for m in manual:
                m = dict(m)
                m["cands"] = " | ".join(m["cands"])
                w.writerow(m)
        created.append((mp, sha256_file(mp), "manual-review", os.path.getsize(mp)))
        append_text(os.path.join(ROOT, "normalized", "manual_review.csv"), "")  # ensure dir
        # append without rewriting prior review rows
        man_path = os.path.join(ROOT, "normalized", "manual_review.csv")
        write_header = not os.path.exists(man_path) or os.path.getsize(man_path) == 0
        with open(man_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["run_id", "slug", "fi", "kickoff", "reason", "cands"])
            if write_header:
                w.writeheader()
            for m in manual:
                w.writerow({
                    "run_id": run_id,
                    "slug": m["slug"],
                    "fi": m["fi"],
                    "kickoff": m["kickoff"],
                    "reason": m["reason"],
                    "cands": " | ".join(m["cands"]),
                })

    if invalid_rows:
        ip = os.path.join(run_dir_snap, "invalid_prematch.csv")
        with open(ip, "x", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(invalid_rows[0].keys()))
            w.writeheader()
            w.writerows(invalid_rows)
        created.append((ip, sha256_file(ip), "invalid", os.path.getsize(ip)))

    if errors:
        ep = os.path.join(run_dir_snap, "errors.json")
        raw = json.dumps(errors, indent=2).encode()
        save(ep, raw, "errors")
        append_text(os.path.join(ROOT, "raw", "errors", "errors.jsonl"), json.dumps({"run_id": run_id, "errors": errors}) + "\n")

    strategy_hash = load_strategy_hash()
    match_summary = {'contract': MATCH_CONTRACT, 'model_status_counts': match_status_counts,
                     'candidate_first_rejections': candidate_rejections}
    save(os.path.join(run_dir_snap, 'market_match_audit.json'),
         json.dumps({'summary': match_summary, 'models': match_audits}, indent=2).encode(),
         'market-match-audit')
    quality = {
        "run_id": run_id,
        "collected_at_utc": now.isoformat(),
        "odds_batch_finished_utc": odds_stamp.isoformat(),
        "scraper_version": SCRAPER_VERSION,
        "strategy_set_sha256": strategy_hash,
        "footinsights_slugs_seen": len(slugs),
        "footinsights_slugs_date_kept": len(future_slugs),
        "footinsights_pages_saved": n_fi_pages,
        "valid_prematch": len(ledger),
        "invalid_or_unparsed": len(invalid_rows),
        "invalid_because_kickoff_passed": n_invalid,
        "no_probability": n_no_prob,
        "pinnacle_leagues_with_matchups": len(league_ids),
        "pinnacle_leagues_transport": pin_leagues_transport,
        "pinnacle_leagues_bridge_requests": pin_leagues_requests,
        "pinnacle_leagues_failure": pin_leagues_failure,
        "pinnacle_league_transports": {kind:sum(i['transport']==kind for i in league_items.values())
                                      for kind in ('direct','browser_bridge_fallback','failed')},
        "pinnacle_direct_leagues_recovered": sum(not a['direct_complete'] and not a['final_error'] for a in transport_audit),
        "pinnacle_open_moneylines": len(pin_matchups),
        "market_matching": match_summary,
        "joined": joined,
        "unmatched_model": unmatched,
        "manual_review": len(manual),
        "analysis_eligible": sum(1 for r in ledger if r["analysis_eligible"] == 1),
        "duplicates_in_run": len(ledger) - len({r["match_id"] for r in ledger}) if ledger else 0,
        "duplicate_rows_skipped": skipped,
        "prob_sum_check_failures": sum(1 for r in ledger if abs((r["p_home_normalized"] + r["p_draw_normalized"] + r["p_away_normalized"]) - 1) > 1e-6),
        "note": "No result fields were filled. Bet365 prices on the FootInsights page are stored as page_odds_* and are not Pinnacle.",
    }
    qp = os.path.join(ROOT, "reports", f"daily_quality_{run_id}.json")
    save(qp, json.dumps(quality, indent=2).encode(), "quality")

    manifest = {
        "manifest_id": f"manifest_{run_id}",
        "created_at_utc": utcnow().isoformat(),
        "run_started_utc": now.isoformat(),
        "strategy_set_sha256": strategy_hash,
        "strategy_file": "strategies_preregistered.json",
        "sources": {
            "model": FI_INDEXES,
            "odds": PIN_LEAGUES,
            "odds_note": "Pinnacle public guest feed (Arcadia), soccer sport 29, full-match moneyline key s;0;m, American odds converted to decimal, overround removed. Snapshot time is before kickoff.",
        },
        "counts": quality,
        "files": [
            {"path": os.path.relpath(p, ROOT), "sha256": digest, "bytes": size, "role": role}
            for p, digest, role, size in created
        ],
    }
    man_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
    man_path = os.path.join(ROOT, "manifests", f"manifest_{run_id}.json")
    save(man_path, man_bytes, "manifest")
    man_hash = sha256_bytes(man_bytes)
    # daily manifest only if absent (first seal of the UTC day). Later runs do not touch it.
    daily = os.path.join(ROOT, "manifests", f"manifest_{day}.json")
    if not os.path.exists(daily):
        save(daily, man_bytes, "manifest-day-first")
    append_text(
        os.path.join(ROOT, "manifests", "manifest_index.jsonl"),
        json.dumps({"run_id": run_id, "manifest_sha256": man_hash, "at": utcnow().isoformat(), "eligible": quality["analysis_eligible"]}) + "\n",
    )
    seal = os.path.join(ROOT, "manifests", f"manifest_{run_id}.sha256")
    save(seal, (man_hash + "  manifest_" + run_id + ".json\n").encode(), "manifest-seal")

    db_snapshots = guarded(upsert_snapshots, ledger)
    db_state = guarded(set_runtime_state, "collector", {
        "run_id": run_id,
        "collected_at_utc": now.isoformat(),
        "manifest_sha256": man_hash,
        "eligible": quality["analysis_eligible"],
        "valid_prematch": len(ledger),
        "errors": len(errors),
        "pinnacle_leagues_transport": pin_leagues_transport,
        "pinnacle_leagues_bridge_requests": pin_leagues_requests,
        "pinnacle_leagues_failure": pin_leagues_failure,
        "pinnacle_league_transports": quality['pinnacle_league_transports'],
        "market_matching": match_summary,
    })

    print(json.dumps({
        "run_id": run_id,
        "collected_at_utc": now.isoformat(),
        "manifest_sha256": man_hash,
        "manifest_path": man_path,
        "fi_pages": n_fi_pages,
        "valid_prematch": len(ledger),
        "joined_pinnacle": joined,
        "unmatched": unmatched,
        "manual_review": len(manual),
        "eligible": quality["analysis_eligible"],
        "invalid": len(invalid_rows),
        "pinnacle_moneylines": len(pin_matchups),
        "market_matching": match_summary,
        "pinnacle_leagues_transport": pin_leagues_transport,
        "errors": len(errors),
        "database": {
            "snapshots": db_snapshots,
            "runtime_state": db_state,
        },
    }, indent=2))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"FATAL {type(e).__name__}: {e}\n")
        raise
