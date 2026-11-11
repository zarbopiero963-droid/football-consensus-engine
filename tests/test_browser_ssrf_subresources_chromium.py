"""FCB-WP-002 — Browser SSRF class B, reproduced and locked with a REAL Chromium.

Class B = everything a *public* page can make the browser request on its own, besides the
top-level navigation: fetch/XHR, scripts, stylesheets, images, fonts, beacons,
EventSource, prefetch, iframes, dedicated workers, service workers, sub-resource
redirects, form posts and WebSocket handshakes. Offline only: 127.0.0.2 plays the public
attacker site, 127.0.0.1 an internal service; the validator is wrapped to classify exactly
that pair like the real one classifies public/private addresses.

Both provider paths are exercised:
- ``local``  — Chromium launched in-process, as CloakBrowser does on Render;
- ``remote`` — Chromium reached over CDP through ``BrowserUseBridge._page()``, the exact
  code path used for Browser Use cloud sessions.

RED on main a642f3f: 15/15 vectors reached the internal service. GREEN: 0 internal
requests, the page itself still loads, and network_responses never lists an internal URL.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from bridge import browser_use, session_leases
from bridge.browser_use import BrowserUseBridge, Session
from bridge.security import UnsafeURL

playwright_api = pytest.importorskip("playwright.async_api")

INTERNAL_HOST, PUBLIC_HOST = "127.0.0.1", "127.0.0.2"
SECRET = "INTERNAL_SECRET_b2c9_DO_NOT_LEAK"

VECTORS = {
    "fetch": "<script>fetch('http://INT/fetch').then(r=>r.text()).then(t=>document.body.dataset.x=t).catch(()=>{})</script>",
    "xhr": "<script>var x=new XMLHttpRequest();x.open('GET','http://INT/xhr');x.send()</script>",
    "img": "<img src='http://INT/img'>",
    "script": "<script src='http://INT/script.js'></script>",
    "css": "<link rel=stylesheet href='http://INT/css'>",
    "font": "<style>@font-face{font-family:x;src:url(http://INT/font)}body{font-family:x}</style>x",
    "subresource_redirect": "<img src='/r302'>",
    "iframe": "<iframe src='http://INT/iframe'></iframe>",
    "websocket": "<script>try{new WebSocket('ws://INT/ws')}catch(e){}</script>",
    "beacon": "<script>navigator.sendBeacon('http://INT/beacon','x')</script>",
    "eventsource": "<script>try{new EventSource('http://INT/es')}catch(e){}</script>",
    "prefetch": "<link rel=prefetch href='http://INT/prefetch'>",
    "worker": "<script>new Worker('/worker.js')</script>",
    "service_worker": "<script>navigator.serviceWorker&&navigator.serviceWorker.register('/sw.js')</script>",
    "form_post": "<form id=f method=post action='http://INT/form'></form><script>f.submit()</script>",
}


class _Servers:
    def __init__(self) -> None:
        self.internal_hits: list[str] = []
        servers = self

        class Internal(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                servers.internal_hits.append(self.path)
                body = SECRET.encode()
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")  # worst case: readable cross-origin
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        class Public(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body: bytes, ctype: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path).path
                internal = f"{INTERNAL_HOST}:{servers.internal_port}"
                if path == "/r302":
                    self.send_response(302)
                    self.send_header("Location", f"http://{internal}/sub-redirect")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/worker.js":
                    return self._send(f"fetch('http://{internal}/worker').catch(()=>{{}})".encode(),
                                      "application/javascript")
                if path == "/sw.js":
                    return self._send(
                        (f"self.addEventListener('install',e=>self.skipWaiting());"
                         f"fetch('http://{internal}/sw').catch(()=>{{}})").encode(), "application/javascript")
                if path == "/public.png":
                    return self._send(b"\x89PNG\r\n\x1a\n", "image/png")
                name = path.strip("/")
                if name == "mixed":
                    body = ("<p id=ok>public page</p><img src='/public.png'>"
                            "<img src='data:image/gif;base64,R0lGODlhAQABAAAAACw='>"
                            f"<img src='http://{internal}/img'>")
                else:
                    body = VECTORS.get(name, "<p id=ok>public page</p>").replace("INT", internal)
                self._send(f"<html><body>{body}</body></html>".encode(), "text/html")

        self.internal = ThreadingHTTPServer((INTERNAL_HOST, 0), Internal)
        self.public = ThreadingHTTPServer((PUBLIC_HOST, 0), Public)
        self.internal_port = self.internal.server_address[1]
        self.public_port = self.public.server_address[1]
        for srv in (self.internal, self.public):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://{PUBLIC_HOST}:{self.public_port}{path}"

    def close(self) -> None:
        for srv in (self.internal, self.public):
            srv.shutdown()
            srv.server_close()


@pytest.fixture()
def servers():
    s = _Servers()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _offline_policy(monkeypatch):
    from bridge.security import UnsafeURL as _Unsafe

    def validate(url):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != PUBLIC_HOST:
            raise _Unsafe("URL is not public")
        return url

    monkeypatch.setattr(browser_use, "validate_public_http_url", validate)
    monkeypatch.setattr(browser_use, "_verdict_cache", {})
    monkeypatch.setattr(session_leases, "renew", lambda *a, **k: None)
    monkeypatch.setattr(session_leases, "register", lambda *a, **k: None)
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "true")  # the kill switch is fail-closed (F-03)


def _chromium_path() -> str | None:
    for path in ("/opt/pw-browsers/chromium",):
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                if "chrome" in files and os.access(os.path.join(root, "chrome"), os.X_OK):
                    return os.path.join(root, "chrome")
    return None


async def _launch(p):
    try:
        cloak = os.environ.get("FCB_CLOAK_CHROMIUM", "")
        if cloak:  # #189: the Cloak binary / full Chrome, launched with production Cloak options
            return await p.chromium.launch(
                executable_path=cloak, chromium_sandbox=True, env=browser_use.cloak_child_env(),
                args=browser_use.cloak_chromium_args(),
                ignore_default_args=["--enable-automation", "--enable-unsafe-swiftshader"])
        return await p.chromium.launch()
    except Exception as exc:  # pragma: no cover
        if os.environ.get("FCB_REQUIRE_CHROMIUM") == "1":
            raise
        pytest.skip(f"Chromium not available: {type(exc).__name__}")


async def _local_session(p):
    browser = await _launch(p)
    bridge = BrowserUseBridge()
    session = Session(id="cloak-b", cdp_url="local-cloak-owned", live_url=None, provider="cloak", browser=browser)
    session.context = await browser.new_context()
    session.page = await session.context.new_page()
    await bridge._install_guards(session)
    bridge._sessions[session.id] = session
    return bridge, session, browser.close


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _remote_session(p):
    """A Chromium reached only over CDP, through the bridge's Browser Use code path."""
    executable = p.chromium.executable_path
    if not executable or not os.path.exists(executable):  # pragma: no cover
        if os.environ.get("FCB_REQUIRE_CHROMIUM") == "1":
            raise RuntimeError("Chromium executable not found")
        pytest.skip("Chromium executable not found")
    port = _free_port()
    proc = subprocess.Popen([executable, "--headless=new", "--no-sandbox", f"--remote-debugging-port={port}",
                             "--remote-debugging-address=127.0.0.1", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            await asyncio.sleep(0.2)
    bridge = BrowserUseBridge()
    session = Session(id="bu-remote", cdp_url=f"http://127.0.0.1:{port}", live_url=None, provider="browser_use")
    bridge._sessions[session.id] = session
    await bridge._page(session)  # real connect_over_cdp + guard arming

    async def close():
        try:
            await bridge._drop_local(session)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    return bridge, session, close


PROVIDERS = {"local": _local_session, "remote": _remote_session}


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_every_class_b_vector_is_blocked(servers, provider):
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await PROVIDERS[provider](p)
            results = {}
            try:
                for name in VECTORS:
                    before = len(servers.internal_hits)
                    try:
                        result = await bridge.open_url(session.id, servers.url(f"/{name}"))
                    except UnsafeURL:
                        result = {"blocked_navigation": True}  # iframe/form documents fail closed
                    await asyncio.sleep(1.0)  # let late sub-resources, workers and sockets fire
                    results[name] = (servers.internal_hits[before:], result)
                responses = await bridge.network_responses(session.id)
                html = await bridge.html(session.id)
            finally:
                await close()
        leaked = {name: hits for name, (hits, _r) in results.items() if hits}
        assert leaked == {}, f"{provider}: internal reached by {leaked}"
        assert SECRET not in repr(responses) and SECRET not in html["html"]
        assert all(INTERNAL_HOST not in entry["url"] for entry in responses["responses"])
    asyncio.run(scenario())


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_public_page_still_loads_and_reports_blocked_count(servers, provider):
    """Blocked sub-resources do not break the page: public and data: resources load,
    the navigation succeeds, and only a count (no URL) of blocked requests is reported."""
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await PROVIDERS[provider](p)
            try:
                result = await bridge.open_url(session.id, servers.url("/mixed"))
                await asyncio.sleep(1.0)  # late sub-resources (remote CDP) are still guarded
                blocked_total = session.blocked_requests
                html = await bridge.html(session.id)
                images = await session.page.evaluate(
                    "() => [...document.images].map(i => [i.src.slice(0, 5), i.naturalWidth > 0 || i.complete])")
            finally:
                await close()
        assert result["final_url"] == servers.url("/mixed")
        assert isinstance(result["blocked_requests"], int)  # count only, never URLs
        assert blocked_total >= 1
        assert "public page" in html["html"]
        assert servers.internal_hits == []
        assert str(servers.internal_port) not in repr(result)
        assert any(src.startswith("data:") for src, _ in images)
    asyncio.run(scenario())


def test_websocket_guard_allows_public_and_closes_internal(servers):
    """The WebSocket route validates the ws:// handshake target like an http(s) URL."""
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await _local_session(p)
            try:
                await bridge.open_url(session.id, servers.url("/ok"))
                state = await session.page.evaluate(f"""() => new Promise(done => {{
                    const ws = new WebSocket('ws://{INTERNAL_HOST}:{servers.internal_port}/ws');
                    ws.onopen = () => done('open');
                    ws.onclose = e => done('closed:' + e.code);
                    ws.onerror = () => done('error');
                    setTimeout(() => done('timeout'), 4000);
                }})""")
            finally:
                await close()
        assert state != "open"
        assert servers.internal_hits == []
    asyncio.run(scenario())


def test_guard_inactive_when_websocket_routing_unavailable():
    """Fail closed: a context without route_web_socket cannot be guarded, so the session
    is marked guard-inactive and every navigation/read raises."""
    bridge = BrowserUseBridge()

    class Ctx:
        pages = []

        async def route(self, *a):
            pass

    class Cdp:
        def on(self, *a):
            pass

        async def send(self, *a, **k):
            return {}

    class Browser:
        async def new_browser_cdp_session(self):
            return Cdp()

    session = Session(id="cloak-x", cdp_url="local", live_url=None, provider="cloak", browser=Browser())
    session.context = Ctx()
    asyncio.run(bridge._install_guards(session))
    assert session.redirect_guard_active is False
    with pytest.raises(UnsafeURL):
        bridge._require_guard(session)


@pytest.mark.parametrize("url,expected", [
    ("data:image/png;base64,AAAA", True),
    ("blob:http://127.0.0.2/uuid", True),
    ("about:blank", True),
    ("file:///etc/passwd", False),
    ("ftp://127.0.0.2/x", False),
    ("ws://127.0.0.1:1/x", False),
    ("ws://127.0.0.2:1/x", True),
    ("wss://127.0.0.2/x", True),
    ("http://user:pw@127.0.0.2/", False),
    ("javascript:alert(1)", False),
])
def test_request_verdict_schemes(url, expected):
    assert asyncio.run(browser_use.request_verdict(url)) is expected
