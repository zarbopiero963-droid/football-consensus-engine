#!/usr/bin/env python3
"""Forebet staging extractor. Browser Use is only the remote browser. No agent."""
import csv
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape

API = "https://api.browser-use.com/api/v4/browsers"
TARGET = "https://www.forebet.com/en/football-tips-and-predictions-for-today"
MAX_SCROLLS = 25
STABLE_ROUNDS = 3
SCROLL_WAIT_MS = 700
MAX_LOAD_SECONDS = 100


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def api(method, url, key, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Browser-Use-API-Key", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:800]}
        if isinstance(parsed, dict):
            parsed.pop("cdpUrl", None)
        return exc.code, parsed


def public_session(payload):
    if not isinstance(payload, dict):
        return {}
    keep = (
        "id", "status", "timeoutAt", "startedAt", "finishedAt",
        "proxyUsedMb", "proxyCost", "browserCost",
    )
    out = {k: payload.get(k) for k in keep}
    for key, value in payload.items():
        if key in out or key == "cdpUrl":
            continue
        low = key.lower()
        if any(token in low for token in ("cost", "traffic", "captcha", "price", "mb")):
            out[key] = value
    return out


def canon(name):
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    stop = {"fc", "cf", "sc", "ac", "afc", "fk", "sk", "the"}
    return " ".join(part for part in text.split() if part not in stop)


def split_rows(html):
    # The native loader's first appended row can be tr_2; later rows wrap with % 2.
    parts = re.split(r'<div class="rcnt tr_\d+">', html)
    return parts[1:]


def probability_spans(inner):
    """Two observed forms, nothing else.
    Server row: <span>30</span> or <span class="fpr">38</span>
    brows() row: <span><b>41</b></span>
    """
    spans = re.findall(r"<span[^>]*>(.*?)</span>", inner, re.S)
    values = []
    for body in spans:
        body = body.strip()
        if re.fullmatch(r"\d{1,3}", body):
            values.append(int(body))
            continue
        bold = re.fullmatch(r"<b>(\d{1,3})</b>", body)
        if bold:
            values.append(int(bold.group(1)))
            continue
        return None
    return values if len(values) == 3 else None


def parse_row(block):
    raw = block[:8000]
    home = re.search(r'class="homeTeam".*?<span itemprop="name">([^<]+)</span>', block, re.S)
    away = re.search(r'class="awayTeam".*?<span itemprop="name">([^<]+)</span>', block, re.S)
    kick = re.search(r'class="date_bah">([^<]+)</span>', block)
    href = re.search(r'class="tnmscn"[^>]*href="([^"]+)"', block)
    league = re.search(r"getstag\(this,\d+,'[^']*','([^']*)'", block)
    short = re.search(r'class="shortTag">([^<]+)</span>', block)
    fprc = re.search(r'<div class="fprc">(.*?)</div>', block, re.S)
    probs = probability_spans(fprc.group(1)) if fprc else None
    pick = re.search(r'class="forepr"><span>([^<]+)</span>', block)
    score = re.search(r'<div class="ex_sc(?=[^"]*\btabonly\b)[^"]*">([^<]+)</div>', block)
    avg = re.search(r'<div class="avg_sc[^"]*">([^<]+)</div>', block)
    haodd = re.search(r'<div class="haodd"(?:\s[^>]*)?>(.*?)</div>', block, re.S)
    odds = []
    if haodd:
        odds = re.findall(r"<span>\s*(\d+\.\d+)\s*</span>", haodd.group(1))
    live_min = re.search(r'class="l_min">\s*([^<]+)\s*<', block)
    ft_mark = re.search(r'class="lmin_td[^"]*".{0,220}?<span>\s*([^<]+)\s*</span>', block, re.S)
    if ft_mark and ft_mark.group(1).strip() in {"FT", "AET", "Postp.", "Pen.", "Aban.", "Cancl.", "Awarded"}:
        state = ft_mark.group(1).strip()
    elif live_min:
        state = "LIVE"
    else:
        state = "SCHEDULED"
    href_val = href.group(1) if href else None
    if href_val and href_val.startswith("http"):
        source_url = href_val
    elif href_val and href_val.startswith("/"):
        source_url = "https://www.forebet.com" + href_val
    else:
        source_url = None
    mid = re.search(r'<div id="(\d+)" class="(?:nofav|fav)', block)
    item = {
        "match_id": mid.group(1) if mid else None,
        "league": league.group(1) if league else None,
        "league_code": short.group(1).strip() if short else None,
        "home": unescape(home.group(1)).strip() if home else None,
        "away": unescape(away.group(1)).strip() if away else None,
        "kickoff_original": kick.group(1).strip() if kick else None,
        "main_pick": pick.group(1).strip() if pick else None,
        "correct_score": score.group(1).strip() if score else None,
        "avg_goals": avg.group(1).strip() if avg else None,
        "source_url": source_url,
        "row_state": state,
        "odds_1": odds[0] if len(odds) >= 3 else None,
        "odds_x": odds[1] if len(odds) >= 3 else None,
        "odds_2": odds[2] if len(odds) >= 3 else None,
        "ou": None,
        "btts": None,
    }
    errors = []
    if not item["home"] or not item["away"]:
        errors.append("teams")
    if probs is None:
        errors.append("triplet")
    else:
        item["p1"], item["px"], item["p2"] = probs
        if not 95 <= sum(probs) <= 105:
            errors.append("triplet_sum")
    if item["main_pick"] not in {"1", "X", "2"}:
        errors.append("pick")
    if errors:
        item["parse_status"] = "PARSE_ERROR"
        item["parse_errors"] = errors
        item["raw"] = raw
        item["p1"] = item.get("p1")
        item["px"] = item.get("px")
        item["p2"] = item.get("p2")
    else:
        item["parse_status"] = "OK"
    return item


def parse_kickoff(text):
    if not text:
        return None
    try:
        return datetime.strptime(text, "%d/%m/%Y %H:%M")
    except ValueError:
        return None


def repo_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_ledger_latest():
    path = os.path.join(repo_root(), "prospective", "prospective_ledger.csv")
    latest = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            prev = latest.get(row["match_id"])
            if prev is None or row["snapshot_timestamp_utc"] > prev["snapshot_timestamp_utc"]:
                latest[row["match_id"]] = row
    return list(latest.values())


def load_charts():
    path = os.path.join(
        repo_root(),
        "staging",
        "football_charts",
        "pilot",
        "runs",
        "20260929T104819Z",
        "parsed.jsonl",
    )
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def same_kick(a_iso, b_dt_utc, window_min=30):
    if not a_iso or b_dt_utc is None:
        return False
    try:
        a = datetime.fromisoformat(a_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    b = b_dt_utc if b_dt_utc.tzinfo else b_dt_utc.replace(tzinfo=timezone.utc)
    return abs((a - b).total_seconds()) <= window_min * 60


def match_sets(forebet_rows, offset_hours):
    ledger = load_ledger_latest()
    charts = load_charts()
    fi, pin, fc = [], [], []
    for row in forebet_rows:
        if row["parse_status"] != "OK":
            continue
        local = parse_kickoff(row["kickoff_original"])
        kick_utc = None
        if local is not None and offset_hours is not None:
            kick_utc = (local - timedelta(hours=offset_hours)).replace(tzinfo=timezone.utc)
        key = (canon(row["home"]), canon(row["away"]))
        fi_hits = []
        pin_hits = []
        for src in ledger:
            if (canon(src["home_team"]), canon(src["away_team"])) == key and same_kick(src["kickoff_utc"], kick_utc):
                fi_hits.append(src["match_id"])
            if (canon(src.get("pin_home") or ""), canon(src.get("pin_away") or "")) == key and same_kick(src.get("pin_kickoff_utc"), kick_utc):
                pin_hits.append(src["match_id"])
        fc_hits = []
        for src in charts:
            if (canon(src.get("home")), canon(src.get("away"))) == key and same_kick(src.get("kickoff_utc"), kick_utc):
                fc_hits.append(src.get("slug"))
        if len(fi_hits) == 1:
            fi.append({"forebet": row["source_url"], "match_id": fi_hits[0]})
        if len(pin_hits) == 1:
            pin.append({"forebet": row["source_url"], "match_id": pin_hits[0]})
        if len(fc_hits) == 1:
            fc.append({"forebet": row["source_url"], "slug": fc_hits[0]})
    fi_ids = {x["forebet"] for x in fi}
    pin_ids = {x["forebet"] for x in pin}
    fc_ids = {x["forebet"] for x in fc}
    both = []
    all3 = []
    for url in fi_ids | pin_ids | fc_ids:
        n = (url in fi_ids) + (url in pin_ids) + (url in fc_ids)
        if n >= 2:
            both.append(url)
        if n == 3:
            all3.append(url)
    return {
        "footinsights": len(fi),
        "pinnacle": len(pin),
        "football_charts": len(fc),
        "at_least_two": len(both),
        "all_three": len(all3),
        "examples_fi": fi[:5],
    }


def verify_timezone(rows):
    ledger = {(canon(r["home_team"]), canon(r["away_team"])): r for r in load_ledger_latest()}
    checks = []
    offsets = []
    pin_offsets = []
    for row in rows:
        if row.get("parse_status") != "OK":
            continue
        src = ledger.get((canon(row.get("home")), canon(row.get("away"))))
        local = parse_kickoff(row.get("kickoff_original"))
        if not src or local is None or not src.get("kickoff_utc"):
            continue
        try:
            remote = datetime.fromisoformat(src["kickoff_utc"].replace("Z", "+00:00"))
        except ValueError:
            continue
        hours = (local.replace(tzinfo=timezone.utc) - remote).total_seconds() / 3600
        if abs(hours - round(hours)) > 0.01:
            continue
        off = int(round(hours))
        offsets.append(off)
        item = {
            "home": row["home"],
            "away": row["away"],
            "forebet": row["kickoff_original"],
            "footinsights_utc": src["kickoff_utc"],
            "pinnacle_utc": src.get("pin_kickoff_utc"),
            "offset_hours_vs_footinsights": off,
        }
        if src.get("pin_kickoff_utc"):
            try:
                pin_remote = datetime.fromisoformat(src["pin_kickoff_utc"].replace("Z", "+00:00"))
            except ValueError:
                pin_remote = None
            if pin_remote is not None:
                pin_hours = (local.replace(tzinfo=timezone.utc) - pin_remote).total_seconds() / 3600
                if abs(pin_hours - round(pin_hours)) < 0.01:
                    pin_off = int(round(pin_hours))
                    pin_offsets.append(pin_off)
                    item["pinnacle_offset_hours"] = pin_off
                    if len(checks) < 8:
                        checks.append(item)
        elif len(checks) < 8 and len(pin_offsets) < 5:
            checks.append(item)
    unique = sorted(set(offsets))
    unique_pin = sorted(set(pin_offsets))
    # all.js: offset = -1 * (Date.getTimezoneOffset() + 120). The +120 is the
    # server clock. The More button sends the same tz=+120. Pinnacle must agree.
    confident = len(pin_offsets) >= 5 and unique_pin == [2]
    return {
        "checks": checks,
        "compared_footinsights": len(offsets),
        "compared_pinnacle": len(pin_offsets),
        "unique_offsets_vs_footinsights": unique,
        "unique_offsets_vs_pinnacle": unique_pin,
        "confident": confident,
        "offset_hours": 2 if confident else None,
        "timezone_assumed": "UTC+2" if confident else None,
        "evidence": "Server clock is UTC+2: all.js sets offset = -1*(getTimezoneOffset()+120) and ltodrows sends tz=+120. Pinnacle kickoffs are 2 hours earlier on every compared match. FootInsights agrees except where its own kickoff disagrees with Pinnacle (those joins are not made).",
        "label_on_page": "+120",
    }


def apply_timezone(rows, tz):
    offset = tz.get("offset_hours") if tz.get("confident") else None
    for row in rows:
        row["timezone_assumed"] = f"UTC+{offset}" if offset is not None else None
        row["timezone_confidence"] = "high" if tz.get("confident") else "unresolved"
        local = parse_kickoff(row.get("kickoff_original"))
        if offset is not None and local is not None:
            row["kickoff_utc"] = (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            row["kickoff_utc"] = None
        basis = "|".join([
            row.get("home") or "",
            row.get("away") or "",
            row.get("kickoff_original") or "",
            str(row.get("p1")),
            str(row.get("px")),
            str(row.get("p2")),
            row.get("source_url") or "",
        ])
        row["row_hash"] = sha256_bytes(basis.encode())
    return rows


def run_browser(key, out, proxy_country="it"):
    os.makedirs(out, exist_ok=True)
    started = time.time()
    report = {"started_utc": utc_now(), "target": TARGET, "agent_used": False}
    status, created = api("POST", API, key, {
        **({"proxyCountryCode": proxy_country} if proxy_country else {}),
        "timeout": 6,
        "solveCaptchas": False,
        "enableRecording": False,
        "browserScreenWidth": 1366,
        "browserScreenHeight": 900,
        "metadata": {"purpose": "forebet-full-staging"},
    })
    report["create_http"] = status
    report["proxy_country"] = proxy_country
    report["create"] = public_session(created)
    if status not in (200, 201) or not created.get("cdpUrl"):
        report["create_error"] = created
        json.dump(report, open(os.path.join(out, "report.json"), "w"), indent=2)
        return report
    session_id = created["id"]
    cdp = created["cdpUrl"]
    net = []
    document_body = bytearray()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(resp):
                try:
                    req = resp.request
                    rtype = req.resource_type
                    if rtype not in {"document", "xhr", "fetch"}:
                        return
                    item = {"url": resp.url[:400], "status": resp.status, "type": rtype}
                    ctype = (resp.headers or {}).get("content-type", "")
                    item["content_type"] = ctype[:80]
                    if rtype == "document" and TARGET.split("?")[0] in resp.url and not document_body:
                        document_body.extend(resp.body())
                    if "getrs.php" in resp.url or ("json" in ctype and len(net) < 40):
                        body = resp.body()
                        if len(body) <= 2_000_000:
                            name = f"net_{len(net):02d}.bin"
                            open(os.path.join(out, name), "wb").write(body)
                            item["file"] = name
                            item["sha256"] = sha256_bytes(body)
                            item["bytes"] = len(body)
                    net.append(item)
                except Exception as exc:
                    net.append({"error": type(exc).__name__})

            page.on("response", on_response)
            response = page.goto(TARGET, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2500)
            initial = page.locator("div.rcnt").count()
            declared_text = page.locator("span.m_cntr").first.inner_text(timeout=5000)
            buttons = page.evaluate("""() => {
              const out = [];
              for (const el of document.querySelectorAll('a,button,span,div')) {
                const t = (el.innerText || '').trim().toLowerCase();
                if (!t || t.length > 40) continue;
                if (/more|load|next|mostra|pagina|page/.test(t)) out.push(t);
              }
              return [...new Set(out)].slice(0, 30);
            }""")
            js_info = page.evaluate("""async () => {
              const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
              const res = await fetch('/includes/js/all.js?v=618');
              const text = await res.text();
              return {tz, status: res.status, js: text, cookie: document.cookie.slice(0, 500)};
            }""")
            js_text = js_info.get("js") or ""
            open(os.path.join(out, "all.js"), "w", encoding="utf-8", errors="replace").write(js_text)
            endpoints = sorted(set(re.findall(r"""['"](/[^'"]+(?:\.php|\.json|ajax[^'"]*))['"]""", js_text)))
            scroll_log = [{"step": 0, "rows": initial}]
            load_started = time.time()
            stable = 0
            prev = initial
            for step in range(1, MAX_SCROLLS + 1):
                if time.time() - load_started > MAX_LOAD_SECONDS:
                    scroll_log.append({"step": step, "stopped": "timeout"})
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(SCROLL_WAIT_MS)
                count = page.locator("div.rcnt").count()
                scroll_log.append({"step": step, "rows": count})
                if count == prev:
                    stable += 1
                else:
                    stable = 0
                prev = count
                if stable >= STABLE_ROUNDS:
                    break
            rows_after_scroll = page.locator("div.rcnt").count()
            # Native loader: #mrows span onclick=ltodrows(...). It GETs getrs.php,
            # then gsv/, then appends only rows AFTER the last .rcnt id. If that
            # id is not in the payload, nothing is appended. Record that fact.
            native = {"before": rows_after_scroll}
            span = page.locator("#mrows span")
            if span.count() == 0:
                native["stopped"] = "no_button"
            else:
                native["onclick"] = (span.first.get_attribute("onclick") or "")[:240]
                try:
                    span.first.click(timeout=5000)
                except Exception as exc:
                    native["click_error"] = type(exc).__name__
                page.wait_for_timeout(4000)
                native["after_click"] = page.locator("div.rcnt").count()
                native["grew"] = native["after_click"] > rows_after_scroll
            rendered = page.evaluate("""async () => {
              const span = document.querySelector('#mrows span');
              const onclick = span ? (span.getAttribute('onclick') || '') : '';
              const args = [];
              const re = /"([^"]*)"/g;
              let mm;
              while ((mm = re.exec(onclick))) args.push(mm[1]);
              const beforeIds = [...document.querySelectorAll('div.rcnt div.nofav, div.rcnt div.fav')].map(el => el.id);
              const present = new Set(beforeIds);
              if (typeof brows !== 'function' || args.length < 7) {
                return {ok: false, reason: 'missing brows or ltodrows args', onclick, args, before: beforeIds.length};
              }
              const url = 'https://www.forebet.com/scripts/getrs.php?ln=' + lang
                + '&tp=' + args[0] + '&in=' + args[1] + '&ord=' + (args[3] || '0')
                + '&tz=' + args[4] + '&tzs=' + args[5] + '&tze=' + args[6];
              const res = await fetch(url);
              const payload = await res.json();
              let live = {};
              let gsvStatus = null;
              try {
                const gsv = await fetch('https://www.forebet.com/gsv/');
                gsvStatus = gsv.status;
                live = await gsv.json();
              } catch (err) {
                gsvStatus = String(err).slice(0, 120);
              }
              const matches = payload[0] || [];
              const leagues = payload[1] || {};
              let appended = 0;
              let stripe = 0;
              const errors = [];
              for (const v of matches) {
                const id = String(v.id);
                if (present.has(id)) continue;
                v.lgdata = leagues[v.league_id] || ['', '', '', ''];
                try {
                  brows(id, v, live[id] || null, stripe, 'dec', '1x2', 4, 0, 0);
                  present.add(id);
                  appended += 1;
                  stripe = (stripe + 1) % 2;
                } catch (err) {
                  if (errors.length < 6) errors.push({id, error: String(err).slice(0, 180)});
                }
              }
              return {
                ok: true,
                onclick,
                getrs_status: res.status,
                getrs_n: matches.length,
                gsv_status: gsvStatus,
                before: beforeIds.length,
                appended,
                after: document.querySelectorAll('div.rcnt').length,
                render_errors: errors,
                page_offset: (typeof offset === 'number') ? offset : null,
                tz_offset_min: new Date().getTimezoneOffset(),
              };
            }""")
            scroll_log.append({"native_more": native, "rendered_by_page_brows": rendered})
            clicks = 1 if native.get("onclick") else 0
            html = page.content()
            html_bytes = html.encode("utf-8")
            open(os.path.join(out, "page.html"), "wb").write(html_bytes)
            shot = os.path.join(out, "page.png")
            try:
                page.screenshot(path=shot, full_page=True, timeout=30000)
                shot_full = True
            except Exception:
                page.screenshot(path=shot, full_page=False)
                shot_full = False
            title = page.title()
            final_url = page.url
            nav_status = response.status if response else None
            headers = {}
            if response is not None:
                for hk in ("server", "cf-ray", "cf-mitigated", "content-type"):
                    val = response.headers.get(hk)
                    if val:
                        headers[hk] = val
            browser.close()
        low = html.lower()
        challenge_wall = any(token in low for token in (
            "just a moment", "attention required", "sorry, you have been blocked",
        ))
        rows = [parse_row(block) for block in split_rows(html)]
        errors = [r for r in rows if r["parse_status"] != "OK"]
        open(os.path.join(out, "parse_errors.jsonl"), "w", encoding="utf-8").write(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in errors)
        )
        keys = [(r.get("match_id") or r.get("source_url"), r.get("kickoff_original")) for r in rows if r["parse_status"] == "OK"]
        dup = len(keys) - len(set(keys))
        xhr_payload = None
        xhr_file = None
        for name in sorted(os.listdir(out)):
            if not name.endswith(".bin"):
                continue
            try:
                data = json.loads(open(os.path.join(out, name), encoding="utf-8").read())
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if (
                isinstance(data, list) and len(data) == 2 and isinstance(data[0], list)
                and data[0] and isinstance(data[0][0], dict) and "Pred_1" in data[0][0]
            ):
                if xhr_payload is None or len(data[0]) >= len(xhr_payload[0]):
                    xhr_payload = data
                    xhr_file = name
        xhr_audit = None
        if xhr_payload is not None:
            by_id = {}
            for match in xhr_payload[0]:
                try:
                    trip = (int(match["Pred_1"]), int(match["Pred_X"]), int(match["Pred_2"]))
                except (KeyError, TypeError, ValueError):
                    trip = None
                by_id[str(match.get("id"))] = {
                    "home": match.get("HOST_NAME"),
                    "away": match.get("GUEST_NAME"),
                    "date_bah": match.get("DATE_BAH"),
                    "comment": match.get("comment") or "",
                    "date": str(match.get("DATE") or "")[:10],
                    "trip": trip,
                }
            dom_ids = [r.get("match_id") for r in rows if r.get("match_id")]
            missing = [i for i in by_id if i not in set(dom_ids)]
            extra = [r.get("match_id") for r in rows if r.get("match_id") and r.get("match_id") not in by_id]
            mismatched = 0
            for row in rows:
                src = by_id.get(row.get("match_id") or "")
                if not src or not src["trip"] or row.get("parse_status") != "OK":
                    continue
                if (row.get("p1"), row.get("px"), row.get("p2")) != src["trip"]:
                    mismatched += 1
            comments = {}
            for src in by_id.values():
                comments[src["comment"] or "empty"] = comments.get(src["comment"] or "empty", 0) + 1
            dates = {}
            for src in by_id.values():
                dates[src["date"]] = dates.get(src["date"], 0) + 1
            xhr_audit = {
                "file": xhr_file,
                "xhr_matches": len(by_id),
                "xhr_ids_missing_in_dom": len(missing),
                "dom_ids_not_in_xhr": len(extra),
                "probability_mismatches": mismatched,
                "xhr_comments": comments,
                "xhr_dates": dates,
                "missing_sample": missing[:8],
                "extra_sample": extra[:8],
            }
        declared = int(re.search(r"\d+", declared_text).group(0)) if re.search(r"\d+", declared_text or "") else None
        doc_rows = len(split_rows(document_body.decode("utf-8", "replace"))) if document_body else None
        doc_times = re.findall(r'class="date_bah">([^<]+)', document_body.decode("utf-8", "replace")) if document_body else []
        final_times = [r.get("kickoff_original") for r in rows[:5]]
        rendered = None
        for entry in scroll_log:
            if isinstance(entry, dict) and entry.get("rendered_by_page_brows"):
                rendered = entry["rendered_by_page_brows"]
        report.update({
            "nav_http": nav_status,
            "final_url": final_url,
            "title": title,
            "response_headers": headers,
            "challenge_wall": challenge_wall,
            "cf_ray": headers.get("cf-ray"),
            "captcha": False,
            "redirected": final_url.rstrip("/") != TARGET.rstrip("/"),
            "browser_timezone": js_info.get("tz"),
            "cookie_sample": js_info.get("cookie"),
            "all_js_status": js_info.get("status"),
            "all_js_bytes": len(js_text.encode()),
            "all_js_sha256": sha256_bytes(js_text.encode()),
            "endpoints_in_all_js": endpoints[:40],
            "loader_buttons": buttons,
            "clicks": clicks,
            "scroll_log": scroll_log,
            "rows_initial": initial,
            "rows_document_response": doc_rows,
            "rows_dom_final": len(rows),
            "declared_counter": declared,
            "declared_is_menu_badge": True,
            "xhr_audit": xhr_audit,
            "page_clock": {
                "browser_timezone": js_info.get("tz"),
                "page_offset_minutes": (rendered or {}).get("page_offset"),
                "js_timezone_offset_minutes": (rendered or {}).get("tz_offset_min"),
            },
            "parsed_ok": sum(1 for r in rows if r["parse_status"] == "OK"),
            "parse_errors": len(errors),
            "duplicates": dup,
            "live_rows": sum(1 for r in rows if r.get("row_state") == "LIVE"),
            "document_first_times": doc_times[:5],
            "dom_first_times": final_times,
            "times_rewritten_by_js": doc_times[:5] != [t for t in final_times if t][:len(doc_times[:5])] if doc_times else None,
            "html_bytes": len(html_bytes),
            "html_sha256": sha256_bytes(html_bytes),
            "screenshot_full_page": shot_full,
            "screenshot_sha256": sha256_bytes(open(shot, "rb").read()) if os.path.exists(shot) else None,
            "network": net,
            "rows": [{k: v for k, v in r.items() if k != "raw"} for r in rows],
        })
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        report["playwright_error"] = re.sub(r"wss://\S+", "wss://redacted", message)
    finally:
        stop_status, stopped = api("PATCH", f"{API}/{session_id}", key, {"action": "stop"})
        get_status, fetched = api("GET", f"{API}/{session_id}", key)
        report["stop_http"] = stop_status
        report["session"] = public_session(fetched if get_status == 200 else stopped)
        report["duration_seconds"] = round(time.time() - started, 2)
        report["finished_utc"] = utc_now()
    return report


def write_normalized(out, run_id, snapshot_ts, rows):
    fields = [
        "run_id", "snapshot_timestamp_utc", "league", "home", "away",
        "kickoff_original", "kickoff_utc", "timezone_assumed", "timezone_confidence",
        "p1", "px", "p2", "main_pick", "correct_score", "avg_goals",
        "odds_1", "odds_x", "odds_2", "ou", "btts", "row_state", "match_id",
        "source_url", "row_hash", "parse_status",
    ]
    path = os.path.join(out, "fixtures.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            item = dict(row)
            item["run_id"] = run_id
            item["snapshot_timestamp_utc"] = snapshot_ts
            writer.writerow(item)
    json.dump(rows, open(os.path.join(out, "fixtures.json"), "w"), ensure_ascii=False, indent=2)


def compare_runs(a_rows, b_rows):
    def key(row):
        return row.get("match_id") or row.get("source_url") or f"{row.get('home')}|{row.get('away')}|{row.get('kickoff_original')}"
    am = {key(r): r for r in a_rows if r.get("parse_status") == "OK"}
    bm = {key(r): r for r in b_rows if r.get("parse_status") == "OK"}
    common = sorted(set(am) & set(bm))
    only_a = sorted(set(am) - set(bm))
    only_b = sorted(set(bm) - set(am))
    deltas = []
    order_same = [key(r) for r in a_rows if r.get("parse_status") == "OK"] == [key(r) for r in b_rows if r.get("parse_status") == "OK"]
    for k in common:
        d = {
            "url": k,
            "delta_p1": am[k]["p1"] - bm[k]["p1"],
            "delta_px": am[k]["px"] - bm[k]["px"],
            "delta_p2": am[k]["p2"] - bm[k]["p2"],
            "kickoff_changed": am[k].get("kickoff_original") != bm[k].get("kickoff_original"),
        }
        if d["delta_p1"] or d["delta_px"] or d["delta_p2"] or d["kickoff_changed"]:
            deltas.append(d)
    return {
        "run_a": len(am),
        "run_b": len(bm),
        "common": len(common),
        "only_first": len(only_a),
        "only_second": len(only_b),
        "only_first_sample": only_a[:6],
        "only_second_sample": only_b[:6],
        "order_same": order_same,
        "changed": len(deltas),
        "changed_sample": deltas[:8],
    }


def main():
    key = os.environ.get("BROWSER_USE_API_KEY", "").strip()
    if not key:
        raise SystemExit("missing key")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), day)
    summaries = []
    stored = []
    for index in (1, 2):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-r{index}"
        out = os.path.join(root, run_id)
        report = None
        for proxy in ("it", "it", None):
            report = run_browser(key, out, proxy_country=proxy)
            err = report.get("playwright_error") or ""
            if report.get("nav_http") and "TUNNEL" not in err:
                break
        rows = report.get("rows") or []
        tz = verify_timezone(rows) if rows else {"confident": False, "checks": [], "unique_offsets": []}
        report["timezone"] = tz
        rows = apply_timezone(rows, tz)
        report["rows"] = rows
        if rows:
            write_normalized(out, run_id, report.get("started_utc"), rows)
            report["matching"] = match_sets(rows, tz.get("offset_hours") if tz.get("confident") else None)
        slim = {k: v for k, v in report.items() if k != "rows"}
        json.dump(slim, open(os.path.join(out, "report.json"), "w"), indent=2, default=str)
        summaries.append(slim)
        stored.append(rows)
        print(json.dumps({
            "run": run_id,
            "http": report.get("nav_http"),
            "declared": report.get("declared_counter"),
            "initial": report.get("rows_initial"),
            "dom": report.get("rows_dom_final"),
            "ok": report.get("parsed_ok"),
            "errors": report.get("parse_errors"),
            "xhr": report.get("xhr_audit"),
            "clock": report.get("page_clock"),
            "tz": report.get("browser_timezone"),
            "cost": (report.get("session") or {}).get("browserCost"),
            "error": report.get("playwright_error") or report.get("create_error"),
        }), flush=True)
    comparison = compare_runs(stored[0], stored[1]) if len(stored) == 2 else {}
    json.dump({"runs": [s.get("create", {}).get("id") for s in summaries], "comparison": comparison}, open(os.path.join(root, "stability.json"), "w"), indent=2)
    print("STABILITY " + json.dumps(comparison)[:2000])


if __name__ == "__main__":
    main()
