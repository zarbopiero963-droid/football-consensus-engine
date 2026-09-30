#!/usr/bin/env python3
"""Append-only prospective snapshot of FootInsights pre-match 1X2 and Pinnacle moneylines.

Never overwrites an existing file. Never writes a result into a model snapshot.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))
from app.persist import guarded, set_runtime_state, upsert_snapshots  # noqa: E402

SCRAPER_VERSION = "prospective-ledger-1"
STRATEGY_FILE = os.path.join(ROOT, "strategies_preregistered.json")
UA = "Mozilla/5.0 (compatible; prospective-ledger/1.0; prematch-audit)"
FI_INDEXES = [
    "https://footinsights.com/predictions/",
    "https://footinsights.com/predictions/tomorrow/",
    "https://footinsights.com/predictions/weekend/",
]
PIN_LEAGUES = "https://guest.api.arcadia.pinnacle.com/0.1/sports/29/leagues?brandId=0"

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


def fetch_pinnacle_leagues_via_bridge():
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
    with urlopen(req, timeout=75) as r:
        payload = json.loads(r.read().decode("utf-8"))
    if payload.get("http_status") != 200 or payload.get("truncated"):
        raise RuntimeError(
            f"bridge pinnacle leagues failed status={payload.get('http_status')} truncated={payload.get('truncated')}"
        )
    body = str(payload.get("body") or "").encode("utf-8")
    if not body:
        raise RuntimeError("bridge pinnacle leagues returned empty body")
    return 200, body

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
    n = canon(s)
    flags = []
    if re.search(r"\b(women|woman|ladies|femenino|femminile|frauen)\b", n):
        flags.append("women")
    if re.search(r"\b(u19|u20|u21|u23|u18|reserves|reserve|ii|b)\b", n):
        flags.append("youth_or_b")
    return ",".join(flags)

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
        dt = dt.replace(tzinfo=timezone.utc)
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
    ev = events[0] if events else {}
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
            return slug, url, code, body, None
        except Exception as e:
            return slug, url, 0, b"", str(e)

    pages = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_match, s) for s in future_slugs]
        for fut in as_completed(futs):
            pages.append(fut.result())

    model_rows = []
    n_fi_pages = 0
    n_invalid = 0
    n_no_prob = 0
    for slug, url, code, body, err in sorted(pages, key=lambda x: x[0]):
        if err or code != 200 or not body:
            errors.append({"source": "footinsights", "url": url, "status": code, "error": err, "at": utcnow().isoformat()})
            if body:
                save(os.path.join(run_dir_fi, slug + ".error.html"), body, "fi-match-error")
            continue
        digest = save(os.path.join(run_dir_fi, slug + ".html"), body, "fi-match")
        n_fi_pages += 1
        parsed = parse_fi_html(body.decode("utf-8", "replace"), url)
        kick = parsed["kickoff"]
        valid = bool(kick and kick > now and parsed["raw"])
        reason = ""
        if not parsed["raw"]:
            reason = "NO_PROBABILITY"
            n_no_prob += 1
            valid = False
        elif not kick:
            reason = "NO_KICKOFF"
            valid = False
        elif kick <= now:
            reason = "INVALID_PREMATCH"
            n_invalid += 1
            valid = False
        minutes = (kick - now).total_seconds() / 60.0 if kick else None
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
            bridge_code, bridge_body = fetch_pinnacle_leagues_via_bridge()
            leagues = json.loads(bridge_body.decode())
            if not isinstance(leagues, list) or not leagues:
                raise RuntimeError("bridge leagues payload is not a non-empty list")
            code, body = bridge_code, bridge_body
            pin_leagues_transport = "browser_bridge_fallback"
        except Exception as exc:
            pin_leagues_transport = "failed"
            errors.append({
                "source": "pinnacle",
                "url": PIN_LEAGUES,
                "status": code,
                "error": f"{direct_error}; bridge fallback failed: {type(exc).__name__}: {exc}",
                "at": utcnow().isoformat(),
            })
            leagues = []

    if leagues and body:
        save(os.path.join(run_dir_pin, "leagues.json"), body, "pin-leagues")

    league_ids = [L["id"] for L in leagues if L.get("matchupCount")]

    def fetch_league(lid):
        out = {"id": lid, "matchups": None, "markets": None, "m_code": None, "k_code": None, "err": None}
        mu = f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{lid}/matchups?brandId=0"
        mk = f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{lid}/markets/straight"
        try:
            c1, b1, _ = http_get(mu, headers=pin_headers)
            c2, b2, _ = http_get(mk, headers=pin_headers)
            out.update(matchups=(c1, b1), markets=(c2, b2))
        except Exception as e:
            out["err"] = str(e)
        return out

    pin_matchups = []  # dicts ready to match
    league_name = {L["id"]: L.get("name") for L in leagues}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_league, lid) for lid in league_ids]
        for fut in as_completed(futs):
            item = fut.result()
            lid = item["id"]
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
            for m in mks:
                if m.get("type") != "moneyline" or m.get("period") != 0 or m.get("isAlternate"):
                    continue
                if m.get("key") not in (None, "s;0;m") and not str(m.get("key", "")).endswith(";m"):
                    continue
                prices = {p.get("designation"): p.get("price") for p in m.get("prices") or []}
                if not all(k in prices for k in ("home", "draw", "away")):
                    continue
                ml[m.get("matchupId")] = m
            for mu in mus:
                if mu.get("type") not in (None, "matchup"):
                    continue
                if mu.get("parentId") or mu.get("parent"):
                    continue
                start = parse_iso(mu.get("startTime"))
                if not start or start <= now:
                    continue
                parts = mu.get("participants") or []
                home = next((p.get("name") for p in parts if p.get("alignment") == "home"), None)
                away = next((p.get("name") for p in parts if p.get("alignment") == "away"), None)
                if not home or not away:
                    continue
                market = ml.get(mu.get("id"))
                if not market or market.get("status") != "open":
                    continue
                prices = {p["designation"]: p["price"] for p in market["prices"]}
                dec = [american_to_decimal(prices["home"]), american_to_decimal(prices["draw"]), american_to_decimal(prices["away"])]
                if not all(dec):
                    continue
                fair, over = fair_from_odds(*dec)
                pin_matchups.append({
                    "matchup_id": mu.get("id"),
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
                })

    # --- match ---
    manual = []
    joined = 0
    unmatched = 0
    ledger = []
    odds_now = utcnow()  # odds were fetched during this run; stamp is the run start for the batch.
    # Use a single odds batch timestamp: end of pinnacle pull is more honest.
    odds_stamp = utcnow()

    for row in model_rows:
        if not row["valid"]:
            continue
        p = row["parsed"]
        kick = p["kickoff"]
        cands = []
        for j, o in enumerate(pin_matchups):
            if abs((o["start"] - kick).total_seconds()) > 6 * 3600:
                continue
            if women_or_youth(p["home"]) != women_or_youth(o["home"]) and women_or_youth(p["home"] + " " + p["away"]) != women_or_youth(o["home"] + " " + o["away"]):
                # flag mismatch only when one side is explicitly women and the other is not
                fw = women_or_youth(p["home"] + " " + p["away"])
                ow = women_or_youth(o["home"] + " " + o["away"])
                if ("women" in fw) != ("women" in ow):
                    continue
            hs = name_score(p["home"], o["home"])
            as_ = name_score(p["away"], o["away"])
            if hs == 0 or as_ == 0:
                continue
            cands.append((hs + as_, hs, as_, j))
        cands.sort(reverse=True)
        chosen = None
        if cands:
            top = cands[0][0]
            tied = [c for c in cands if c[0] == top]
            if len(tied) > 1 or top < 4:
                manual.append({
                    "slug": row["slug"],
                    "fi": f"{p['home']} vs {p['away']}",
                    "kickoff": kick.isoformat(),
                    "reason": "ambiguous" if len(tied) > 1 else "weak_name",
                    "cands": [f"{pin_matchups[c[3]]['home']} vs {pin_matchups[c[3]]['away']} ({pin_matchups[c[3]]['league']}) score={c[0]}" for c in tied[:4]],
                })
            else:
                chosen = pin_matchups[tied[0][3]]
                joined += 1
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
            "home_team": p["home"],
            "away_team": p["away"],
            "home_canon": canon(p["home"]),
            "away_canon": canon(p["away"]),
            "team_flags": women_or_youth(p["home"] + " " + p["away"]),
            "snapshot_timestamp_utc": now.isoformat(),
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
                "pin_snapshot_timestamp_utc": odds_stamp.isoformat(),
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
                "name_score_note": "auto",
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
            "snapshot_timestamp_utc": now.isoformat(),
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
    if os.path.exists(grow) and os.path.getsize(grow):
        with open(grow, newline="", encoding="utf-8") as f:
            for prev in csv.DictReader(f):
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
        w = csv.DictWriter(f, fieldnames=fields)
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
        "pinnacle_open_moneylines": len(pin_matchups),
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
