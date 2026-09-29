#!/usr/bin/env python3
"""Forebet via Browser Use CDP only. No Browser Use agent. Staging only."""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.browser-use.com/api/v4/browsers"
TARGET = "https://www.forebet.com/en/football-tips-and-predictions-for-today"
OUT_ROOT = os.path.dirname(os.path.abspath(__file__))


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
            raw = resp.read()
            return resp.status, json.loads(raw.decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:2000]}
        return exc.code, parsed


def public_session(payload):
    keep = (
        "id",
        "status",
        "timeoutAt",
        "startedAt",
        "finishedAt",
        "liveUrl",
        "proxyUsedMb",
        "proxyCost",
        "browserCost",
    )
    out = {k: payload.get(k) for k in keep}
    cdp = payload.get("cdpUrl") or ""
    out["cdpUrlPresent"] = bool(cdp)
    out["cdpScheme"] = cdp.split(":", 1)[0] if cdp else None
    return out


def extract_triplets(html: str):
    rows = []
    # Forebet probability cell: three integer spans inside fprc.
    blocks = re.findall(
        r'class="fprc"[^>]*>(.*?)</div>',
        html,
        flags=re.I | re.S,
    )
    if not blocks:
        blocks = re.findall(
            r'class="[^"]*fprc[^"]*"[^>]*>(.*?)</(?:div|td|span)>',
            html,
            flags=re.I | re.S,
        )
    for block in blocks:
        nums = [int(n) for n in re.findall(r">(\d{1,3})<", block)]
        if len(nums) >= 3 and 95 <= sum(nums[:3]) <= 105:
            rows.append({"p1": nums[0], "px": nums[1], "p2": nums[2], "source": "fprc"})
    if rows:
        return rows[:8], "fprc"
    # Fallback: visible "1 X 2" percentage triplets only when explicitly grouped.
    return [], "none"


def main():
    key = os.environ.get("BROWSER_USE_API_KEY", "").strip()
    if not key:
        raise SystemExit("missing BROWSER_USE_API_KEY")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(OUT_ROOT, "runs", run_id)
    os.makedirs(out, exist_ok=True)
    started = time.time()
    started_utc = utc_now()
    session_id = None
    report = {
        "run_id": run_id,
        "started_utc": started_utc,
        "target": TARGET,
        "agent_used": False,
        "ledger_touched": False,
    }
    xhr = []

    status, created = api(
        "POST",
        API,
        key,
        {
            "proxyCountryCode": "it",
            "timeout": 4,
            "solveCaptchas": False,
            "enableRecording": False,
            "browserScreenWidth": 1366,
            "browserScreenHeight": 768,
            "metadata": {"purpose": "forebet-cdp-staging"},
        },
    )
    report["create_http"] = status
    report["create"] = public_session(created) if isinstance(created, dict) else {"error": str(created)[:500]}
    if status != 200 and status != 201:
        report["create_error"] = created if isinstance(created, dict) else str(created)[:1000]
        # drop any secret-looking fields
        if isinstance(report["create_error"], dict):
            report["create_error"].pop("cdpUrl", None)
        path = os.path.join(out, "report.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(json.dumps({"ok": False, "phase": "create", "http": status, "out": out}))
        return 1
    session_id = created["id"]
    cdp = created.get("cdpUrl")
    if not cdp:
        report["error"] = "no cdpUrl"
        with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        return 1

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(resp):
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                    if "json" not in ctype:
                        return
                    if len(xhr) >= 15:
                        return
                    body = resp.body()
                    if len(body) > 400_000:
                        return
                    name = f"xhr_{len(xhr):02d}.json"
                    blob = body if body[:1] in (b"{", b"[") else body
                    path = os.path.join(out, name)
                    with open(path, "wb") as fh:
                        fh.write(blob)
                    xhr.append(
                        {
                            "file": name,
                            "url": resp.url[:300],
                            "status": resp.status,
                            "sha256": sha256_bytes(blob),
                            "bytes": len(blob),
                        }
                    )
                except Exception as exc:
                    xhr.append({"error": type(exc).__name__})

            page.on("response", on_response)
            nav_started = utc_now()
            response = page.goto(TARGET, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)
            html = page.content()
            html_bytes = html.encode("utf-8")
            html_path = os.path.join(out, "page.html")
            with open(html_path, "wb") as fh:
                fh.write(html_bytes)
            shot_path = os.path.join(out, "page.png")
            page.screenshot(path=shot_path, full_page=False)
            title = page.title()
            final_url = page.url
            nav_status = response.status if response is not None else None
            headers = {}
            if response is not None:
                for hk in ("server", "cf-ray", "cf-mitigated", "content-type"):
                    val = response.headers.get(hk)
                    if val:
                        headers[hk] = val
            browser.close()
        triplets, parser = extract_triplets(html)
        low = html.lower()
        challenge = any(
            token in low
            for token in (
                "just a moment",
                "attention required",
                "cf-browser-verification",
                "challenge-platform",
                "enable javascript and cookies",
                "sorry, you have been blocked",
            )
        )
        report.update(
            {
                "nav_started_utc": nav_started,
                "nav_http": nav_status,
                "response_headers": headers,
                "final_url": final_url,
                "title": title,
                "html_bytes": len(html_bytes),
                "html_sha256": sha256_bytes(html_bytes),
                "screenshot": shot_path,
                "screenshot_sha256": sha256_bytes(open(shot_path, "rb").read()),
                "cloudflare_or_captcha": challenge or nav_status == 403,
                "triplet_parser": parser,
                "triplet_count": len(triplets),
                "triplets_sample": triplets[:5],
                "xhr": xhr,
            }
        )
    except Exception as exc:
        report["playwright_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if session_id:
            stop_status, stopped = api(
                "PATCH",
                f"{API}/{session_id}",
                key,
                {"action": "stop"},
            )
            get_status, fetched = api("GET", f"{API}/{session_id}", key)
            report["stop_http"] = stop_status
            report["stop"] = public_session(stopped) if isinstance(stopped, dict) else {}
            report["get_http"] = get_status
            report["final_session"] = public_session(fetched) if isinstance(fetched, dict) else {}
        report["finished_utc"] = utc_now()
        report["duration_seconds"] = round(time.time() - started, 2)
        with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(json.dumps({
            "ok": "playwright_error" not in report and report.get("create_http") in (200, 201),
            "out": out,
            "nav_http": report.get("nav_http"),
            "title": report.get("title"),
            "challenge": report.get("cloudflare_or_captcha"),
            "triplets": report.get("triplet_count"),
            "html_sha256": report.get("html_sha256"),
            "duration_seconds": report.get("duration_seconds"),
            "browserCost": (report.get("final_session") or {}).get("browserCost"),
            "status": (report.get("final_session") or {}).get("status"),
            "error": report.get("playwright_error"),
        }))
    return 0 if "playwright_error" not in report else 1


if __name__ == "__main__":
    raise SystemExit(main())
