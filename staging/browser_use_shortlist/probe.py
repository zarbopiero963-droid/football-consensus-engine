#!/usr/bin/env python3
"""Sequential Browser Use CDP probe. Does not touch the Forebet pilot."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
VAR = os.path.join(ROOT, "var")
API = "https://api.browser-use.com/api/v4/browsers"
KEY_FILE = os.path.abspath(os.path.join(ROOT, "..", "forebet", "pilot48", "var", "api_key"))
LEDGER = os.path.abspath(os.path.join(ROOT, "..", "..", "prospective", "prospective_ledger.csv"))

SITES = [
    {"id": "xg_football", "url": "https://xg.football/"},
    {"id": "dratings", "url": "https://www.dratings.com/"},
    {"id": "predictlix", "url": "https://predictlix.com/predictions/"},
    {"id": "sports4cast", "url": "https://sports4cast.com/4casts/football4cast/"},
    {"id": "statsultra", "url": "https://statsultra.com/forecasts/"},
    {"id": "betstudy", "url": "https://www.betstudy.com/"},
    {"id": "prosoccer", "url": "https://prosoccer.eu/"},
    {"id": "elofoot", "url": "https://www.elofoot.com/en"},
]


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(dt=None):
    return (dt or utcnow()).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_key():
    key = os.environ.get("BROWSER_USE_API_KEY", "").strip()
    if key:
        return key
    return open(KEY_FILE, encoding="utf-8").read().strip()


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
    keep = ("id", "status", "timeoutAt", "startedAt", "finishedAt", "proxyUsedMb", "proxyCost", "browserCost")
    out = {k: payload.get(k) for k in keep}
    for key, value in payload.items():
        if key in out or key == "cdpUrl":
            continue
        low = key.lower()
        if any(token in low for token in ("cost", "traffic", "captcha", "price", "mb")):
            out[key] = value
    return out


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def probe_site(key, site, run_id):
    out = os.path.join(ROOT, site["id"], run_id)
    if os.path.exists(out):
        raise SystemExit(f"refusing to reuse {out}")
    os.makedirs(out)
    started = time.time()
    report = {
        "source": site["id"],
        "run_id": run_id,
        "started_utc": iso(),
        "entry_url": site["url"],
        "agent_used": False,
        "isolated_from": "forebet-48h-20260929T153629Z",
    }
    status, created = api("POST", API, key, {
        "proxyCountryCode": "it",
        "timeout": 4,
        "solveCaptchas": False,
        "enableRecording": False,
        "browserScreenWidth": 1366,
        "browserScreenHeight": 900,
        "metadata": {"purpose": "shortlist-" + site["id"]},
    })
    report["create_http"] = status
    report["create"] = public_session(created)
    if status not in (200, 201) or not created.get("cdpUrl"):
        report["create_error"] = created
        atomic_write(os.path.join(out, "report.json"), report)
        return report
    session_id = created["id"]
    cdp = created["cdpUrl"]
    net = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page_errors = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)[:240]) if len(page_errors) < 12 else None)

            def on_response(resp):
                try:
                    req = resp.request
                    if req.resource_type not in {"document", "xhr", "fetch"}:
                        return
                    item = {"url": resp.url[:400], "status": resp.status, "type": req.resource_type}
                    ctype = (resp.headers or {}).get("content-type", "")
                    item["content_type"] = ctype[:80]
                    if req.resource_type != "document" and ("json" in ctype or "javascript" not in ctype) and len(net) < 25:
                        body = resp.body()
                        if 40 < len(body) <= 1_500_000 and (b"{" in body[:20] or b"[" in body[:20] or "json" in ctype):
                            name = f"net_{len(net):02d}.bin"
                            open(os.path.join(out, name), "wb").write(body)
                            item["file"] = name
                            item["sha256"] = sha256_bytes(body)
                            item["bytes"] = len(body)
                    net.append(item)
                except Exception as exc:
                    if len(net) < 30:
                        net.append({"error": type(exc).__name__})

            page.on("response", on_response)
            response = page.goto(site["url"], wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2500)
            try:
                page.evaluate("""() => {
                  const btn = [...document.querySelectorAll('button, a')].find(el => {
                    const t = (el.innerText || '').trim();
                    return t.length > 0 && t.length < 40 && /accept all|accetta tutti|i agree|agree|consent|allow all/i.test(t);
                  });
                  if (btn) btn.click();
                }""")
                page.wait_for_timeout(800)
            except Exception:
                pass
            html = page.content()
            open(os.path.join(out, "page.html"), "w", encoding="utf-8").write(html)
            shot = os.path.join(out, "page.png")
            shot_full = True
            try:
                page.screenshot(path=shot, full_page=True, timeout=25000)
            except Exception:
                page.screenshot(path=shot, full_page=False)
                shot_full = False
            title = page.title()
            final_url = page.url
            headers = {}
            if response is not None:
                for hk in ("server", "cf-ray", "cf-mitigated", "content-type"):
                    val = response.headers.get(hk)
                    if val:
                        headers[hk] = val
            signals = page.evaluate("""() => {
              const text = document.body ? document.body.innerText.slice(0, 20000) : '';
              const links = [...document.querySelectorAll('a[href]')].map(a => ({
                href: a.href,
                text: (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 90)
              })).filter(a => a.text && a.href.startsWith('http')).slice(0, 40);
              return {text, links};
            }""")
            low = (html or "").lower()
            text_low = (signals.get("text") or "").lower()
            challenge = any(token in low for token in ("just a moment", "attention required", "sorry, you have been blocked", "cf-browser-verification"))
            captcha = any(token in low for token in ("g-recaptcha", "hcaptcha", "cf-turnstile", "recaptcha"))
            paywall = any(token in text_low for token in ("subscribe to see", "upgrade to premium", "premium only", "unlock this", "credit card", "start your free trial"))
            login_wall = any(token in text_low for token in ("log in to view", "sign in to see", "login to view predictions", "create an account to"))
            cookie = any(token in text_low for token in ("cookie", "consent"))
            browser.close()
        report.update({
            "nav_http": response.status if response else None,
            "final_url": final_url,
            "title": title,
            "response_headers": headers,
            "cf_ray": headers.get("cf-ray"),
            "redirected": final_url.rstrip("/") != site["url"].rstrip("/"),
            "challenge_wall": challenge,
            "captcha": captcha,
            "paywall_language": paywall,
            "login_wall_language": login_wall,
            "cookie_banner_language": cookie,
            "screenshot_full_page": shot_full,
            "html_bytes": len(html.encode("utf-8")),
            "html_sha256": sha256_bytes(html.encode("utf-8")),
            "screenshot_sha256": sha256_file(shot) if os.path.exists(shot) else None,
            "page_errors": page_errors,
            "network": net,
            "visible_text_sample": (signals.get("text") or "")[:2500],
            "links_sample": signals.get("links") or [],
        })
    except Exception as exc:
        report["playwright_error"] = re.sub(r"wss://\S+", "wss://redacted", f"{type(exc).__name__}: {exc}")
    finally:
        stop_status, stopped = api("PATCH", f"{API}/{session_id}", key, {"action": "stop"})
        get_status, fetched = api("GET", f"{API}/{session_id}", key)
        report["stop_http"] = stop_status
        report["session"] = public_session(fetched if get_status == 200 else stopped)
        report["duration_seconds"] = round(time.time() - started, 2)
        report["finished_utc"] = iso()
        atomic_write(os.path.join(out, "report.json"), report)
    return report


def main():
    os.makedirs(VAR, exist_ok=True)
    lock_path = os.path.join(VAR, "probe.lock")
    import fcntl
    lockf = open(lock_path, "a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("shortlist probe already running")
    key = load_key()
    run_id = stamp()
    index = os.path.join(ROOT, "index.jsonl")
    summaries = []
    for site in SITES:
        print(iso(), "start", site["id"], flush=True)
        report = probe_site(key, site, run_id)
        summary = {
            "source": site["id"],
            "run_id": run_id,
            "nav_http": report.get("nav_http"),
            "final_url": report.get("final_url"),
            "title": report.get("title"),
            "challenge_wall": report.get("challenge_wall"),
            "captcha": report.get("captcha"),
            "paywall_language": report.get("paywall_language"),
            "login_wall_language": report.get("login_wall_language"),
            "html_bytes": report.get("html_bytes"),
            "html_sha256": report.get("html_sha256"),
            "duration_seconds": report.get("duration_seconds"),
            "session": report.get("session"),
            "error": report.get("playwright_error") or report.get("create_error"),
            "cf_ray": report.get("cf_ray"),
        }
        summaries.append(summary)
        with open(index, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, default=str) + "\n")
        print(iso(), "done", json.dumps(summary, default=str)[:500], flush=True)
    atomic_write(os.path.join(ROOT, f"pass1_{run_id}.json"), {"run_id": run_id, "finished_utc": iso(), "sites": summaries})
    print("PASS1", run_id, "ledger", sha256_file(LEDGER))


if __name__ == "__main__":
    main()
