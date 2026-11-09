"""Review of #177 P0, locked with REAL Chromium: the consent page's security headers must not
block the 302 from the consent POST to the client's cross-origin callback.

Chromium applies CSP `form-action` to the redirect that follows a form submission, so
`form-action 'self'` stopped every new Claude.ai / ChatGPT authorization on the consent page.
The control case proves the test is not vacuous: with the old policy the callback is blocked.
Offline only: two loopback servers play the bridge and the client callback.
"""
from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bridge import oauth

playwright_api = pytest.importorskip("playwright.sync_api")

OLD_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"


def _servers(csp: str):
    hits = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            body = b"<title>CALLBACK_REACHED</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    cb = ThreadingHTTPServer(("127.0.0.2", 0), Callback)
    callback_url = f"http://127.0.0.2:{cb.server_address[1]}/api/mcp/auth_callback"

    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _headers(self):
            for k, v in {**oauth.CONSENT_HEADERS, "Content-Security-Policy": csp}.items():
                self.send_header(k, v)

        def do_GET(self):
            body = (b'<form method="post"><input name="password" value="x">'
                    b'<button id="go" type="submit">Autorizza</button></form>')
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self._headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            # Exactly what authorize_post returns on success: 302 to the client's callback.
            self.send_response(302)
            self.send_header("Location", callback_url + "?code=c&state=s&iss=x")
            self.send_header("Content-Length", "0")
            self.end_headers()

    br = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    for s in (cb, br):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return br, cb, hits


def _run(csp: str) -> list[str]:
    br, cb, hits = _servers(csp)
    try:
        with playwright_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # pragma: no cover
                if os.environ.get("FCB_REQUIRE_CHROMIUM") == "1":
                    raise
                pytest.skip(f"Chromium not available: {type(exc).__name__}")
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{br.server_address[1]}/oauth/authorize")
            page.click("#go")
            page.wait_for_timeout(1500)
            browser.close()
    finally:
        br.shutdown(); cb.shutdown(); br.server_close(); cb.server_close()
    return hits


def test_consent_post_reaches_the_cross_origin_client_callback():
    hits = _run(oauth.CONSENT_HEADERS["Content-Security-Policy"])
    assert len(hits) == 1 and hits[0].startswith("/api/mcp/auth_callback?code=")


def test_control_form_action_self_blocks_the_callback():
    """RED control: the policy shipped in the first head of #177 blocks the redirect."""
    assert _run(OLD_CSP) == []
