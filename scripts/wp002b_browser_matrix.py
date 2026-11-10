"""FCB-WP-002b evidence matrix — offline, real Chromium, TEST ENVIRONMENT ONLY.

For every vector, provider (local / remote CDP) and guard state (bridge guards ON / OFF) it records
whether anything reached the internal listener (TCP request line or UDP packet). Run once per
Chromium profile:

    FCB_CHROMIUM_PROFILE=lna_off python scripts/wp002b_browser_matrix.py
    FCB_CHROMIUM_PROFILE=native  python scripts/wp002b_browser_matrix.py

`lna_off` disables Chromium's Local Network Access checks so the bridge's guards are measured
without relying on that browser policy; `native` is the unmodified browser. Production flags are
never changed by this script. Output: one JSON line per cell, then a Markdown table on stdout.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import test_browser_capability_guard_chromium as g  # noqa: E402
from bridge import browser_use, session_leases  # noqa: E402
from bridge.browser_use import BrowserUseBridge  # noqa: E402
from bridge.security import UnsafeURL  # noqa: E402

GUARD_JS = browser_use.CAPABILITY_GUARD_JS
CSP_HEADERS = BrowserUseBridge._document_csp_headers


def _validate(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname not in (g.PUB, g.REBIND_HOST):
        raise UnsafeURL("URL is not public")
    return url


def _guards(on: bool):
    browser_use.CAPABILITY_GUARD_JS = GUARD_JS if on else "/* off */"
    BrowserUseBridge._document_csp_headers = staticmethod(CSP_HEADERS if on else (lambda h: list(h or [])))


def main() -> None:
    browser_use.validate_public_http_url = _validate
    session_leases.renew = session_leases.register = lambda *a, **k: None
    os.environ["BROWSER_TOOLS_ENABLED"] = "true"  # the kill switch is fail-closed (F-03)
    servers, udp = g._Servers(), g._Udp()
    vectors = {
        "worker_ws": "worker", "shared_worker_ws": "shared_worker", "srcdoc_worker_ws": "srcdoc_worker",
        "websocketstream": "websocketstream", "worker_websocketstream": "worker_websocketstream",
        "sandbox_srcdoc_worker": "sandbox_srcdoc_worker", "sandbox_srcdoc_websocket": "sandbox_srcdoc_websocket",
        "sandbox_srcdoc_websocketstream": "sandbox_srcdoc_websocketstream",
        "webrtc_turn_tcp_sandbox": "rtc_turn", "webrtc_stun_udp_sandbox": g._rtc_udp_page(udp),
        "webtransport_sandbox": g._webtransport_page(udp), "speculation_rules": "static_inline",
        "dns_rebinding": None,
    }
    rows = []
    for provider in ("local", "remote"):
        for on in (True, False):
            _guards(on)
            for vector, page in vectors.items():
                lines_before, udp_before = len(servers.lines), udp.packets
                if page is None:
                    port = servers.internal.split(":")[1]
                    url = f"http://{g.REBIND_HOST}:{port}/rebind"
                    asyncio.run(_open(provider, url))
                else:
                    g._visit(servers, provider, [page], settle=3.0)
                reached = len(servers.lines) > lines_before or udp.packets > udp_before
                row = {"profile": g.PROFILE, "provider": provider, "guards": "on" if on else "off",
                       "vector": vector, "reached_internal": reached}
                print(json.dumps(row), flush=True)
                rows.append(row)
    _guards(True)
    print(f"\n| vector | local guards OFF | local guards ON | remote guards OFF | remote guards ON |  (profile {g.PROFILE})")
    print("|---|---|---|---|---|")
    cell = {(r["vector"], r["provider"], r["guards"]): ("LEAK" if r["reached_internal"] else "clean") for r in rows}
    for vector in vectors:
        print(f"| {vector} | " + " | ".join(cell[(vector, p, gs)] for p in ("local", "remote") for gs in ("off", "on")) + " |")


async def _open(provider, url):
    async with g.playwright_api.async_playwright() as p:
        bridge, session, close = await g.PROVIDERS[provider](p)
        try:
            try:
                await bridge.open_url(session.id, url, timeout_ms=4000)
            except Exception:
                pass
            await asyncio.sleep(1)
        finally:
            await close()


if __name__ == "__main__":
    main()
