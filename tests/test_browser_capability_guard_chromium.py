"""FCB-WP-002b — browser capabilities outside the CDP request guard, locked with REAL Chromium.

Reproduced on main 25a1d8e (both provider paths) and fixed fail-closed:
- a WebSocket opened INSIDE a dedicated or shared worker reached an internal host (the Fetch
  request stage and route_web_socket only cover page contexts) -> Worker, SharedWorker,
  RTCPeerConnection and WebTransport are removed from every realm by an init script;
- speculation-rules prefetch/prerender sent GET requests to an internal host from the
  browser process (no CDP Fetch pause; Page.setPrerenderingAllowed and a data-saver override
  did not stop it) -> documents declaring speculation rules (Speculation-Rules header or a
  speculationrules script in the body) are refused at the response stage, and every DOM path
  that can set a script's type to speculationrules at runtime throws.

Offline only: 127.0.0.2 plays the public site, 127.0.0.1 the internal service, which records
every TCP connection and its first request line. The validator is wrapped to classify exactly
that pair like the real one does.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from bridge import browser_use, session_leases
from bridge.browser_use import CAPABILITY_BLOCKED, BrowserUseBridge, Session
from bridge.security import UnsafeURL

playwright_api = pytest.importorskip("playwright.async_api")

PUB, INT = "127.0.0.2", "127.0.0.1"
RULES = "JSON.stringify({prefetch:[{source:'list',urls:['http://INT/r']}],prerender:[{source:'list',urls:['http://INT/p']}]})"
T = "'specula'+'tionrules'"  # obfuscated so the static body scan does NOT see it: tests the runtime guard
WS_BLOB = "URL.createObjectURL(new Blob([\"new WebSocket('ws://INT/')\"]))"

WORKER_PAGES = {
    "worker": f"<script>try{{new Worker({WS_BLOB})}}catch(e){{}}</script>",
    "shared_worker": f"<script>try{{new SharedWorker({WS_BLOB})}}catch(e){{}}</script>",
    "iframe_worker": f"<iframe id=f></iframe><script>try{{new (document.getElementById('f').contentWindow.Worker)({WS_BLOB})}}catch(e){{}}</script>",
    "late_iframe_worker": f"<script>const f=document.createElement('iframe');document.body.appendChild(f);try{{new f.contentWindow.Worker({WS_BLOB})}}catch(e){{}}</script>",
    "srcdoc_worker": "<iframe srcdoc=\"<script>try{new Worker(URL.createObjectURL(new Blob([&quot;new WebSocket('ws://INT/')&quot;])))}catch(e){}</script>\"></iframe>",
    "popup_worker": f"<script>const w=window.open('about:blank');try{{new w.Worker({WS_BLOB})}}catch(e){{}}</script>",
}
DYNAMIC_RULES = {
    "dyn_type": f"<script>const s=document.createElement('script');try{{s.type={T}}}catch(e){{}};s.textContent={RULES};document.head.appendChild(s)</script>",
    "dyn_setattr": f"<script>const s=document.createElement('script');try{{s.setAttribute('type',{T})}}catch(e){{}};s.textContent={RULES};document.head.appendChild(s)</script>",
    "dyn_setattrns": f"<script>const s=document.createElement('script');try{{s.setAttributeNS(null,'type',{T})}}catch(e){{}};s.textContent={RULES};document.head.appendChild(s)</script>",
    "dyn_attrnode": f"<script>const s=document.createElement('script');const a=document.createAttribute('type');a.value={T};try{{s.setAttributeNode(a)}}catch(e){{}};s.textContent={RULES};document.head.appendChild(s)</script>",
    "dyn_attrvalue": f"<script>const s=document.createElement('script');s.setAttribute('type','x');try{{s.getAttributeNode('type').value={T}}}catch(e){{}};s.textContent={RULES};document.head.appendChild(s)</script>",
    "dyn_namednodemap": f"<script>const s=document.createElement('script');const a=document.createAttribute('type');a.value={T};try{{s.attributes.setNamedItem(a)}}catch(e){{}};s.textContent={RULES};document.head.appendChild(s)</script>",
    "dyn_late_text": f"<script>const s=document.createElement('script');s.type='x';document.head.appendChild(s);try{{s.getAttributeNode('type').value={T}}}catch(e){{}};s.textContent={RULES}</script>",
    "dyn_fragment": f"<script>try{{document.body.appendChild(document.createRange().createContextualFragment('<script type='+{T}+'>'+{RULES}+'<\\/script>'))}}catch(e){{}}</script>",
    "dyn_docwrite": f"<script>try{{document.write('<script type='+{T}+'>'+{RULES}+'<\\/script>')}}catch(e){{}}</script>",
}
STATIC_RULES = {
    "static_inline": "<script type=speculationrules>{\"prefetch\":[{\"source\":\"list\",\"urls\":[\"http://INT/s\"]}]}</script>",
    "static_prerender": "<script type=\"SpeculationRules\">{\"prerender\":[{\"source\":\"list\",\"urls\":[\"http://INT/s2\"]}]}</script>",
    "header_rules": "",
}


class _Servers:
    def __init__(self):
        self.lines: list[bytes] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((INT, 0))
        self.sock.listen(32)
        self.internal = f"{INT}:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._accept, daemon=True).start()
        servers = self

        class Public(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                name = urlsplit(self.path).path.strip("/")
                if name == "rules.json":
                    body = json.dumps({"prefetch": [{"source": "list", "urls": [f"http://{servers.internal}/h"]}]}).encode()
                    ctype = "application/speculationrules+json"
                else:
                    page = {**WORKER_PAGES, **DYNAMIC_RULES, **STATIC_RULES, **POSITIVE}.get(name, "")
                    body = (f"<html><head></head><body><p id=ok>public</p>{page}</body></html>"
                            .replace("INT", servers.internal).encode())
                    ctype = "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                if name == "header_rules":
                    self.send_header("Speculation-Rules", '"/rules.json"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.public = ThreadingHTTPServer((PUB, 0), Public)
        threading.Thread(target=self.public.serve_forever, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(1.5)
            try:
                data = conn.recv(200)
            except OSError:
                data = b""
            self.lines.append(data.split(b"\r\n")[0][:80])
            conn.close()

    def url(self, name):
        return f"http://{PUB}:{self.public.server_address[1]}/{name}"

    def close(self):
        self.public.shutdown()
        self.public.server_close()
        self.sock.close()


POSITIVE = {
    "normal": "<script>document.title='scripts-run'</script>",
    "mentions_rules_in_text": "",
}


@pytest.fixture()
def servers():
    s = _Servers()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _offline_policy(monkeypatch):
    def validate(url):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != PUB:
            raise UnsafeURL("URL is not public")
        return url

    monkeypatch.setattr(browser_use, "validate_public_http_url", validate)
    monkeypatch.setattr(browser_use, "_verdict_cache", {})
    monkeypatch.setattr(session_leases, "renew", lambda *a, **k: None)
    monkeypatch.setattr(session_leases, "register", lambda *a, **k: None)
    monkeypatch.delenv("BROWSER_TOOLS_ENABLED", raising=False)


async def _launch(p):
    try:
        return await p.chromium.launch()
    except Exception as exc:  # pragma: no cover
        if os.environ.get("FCB_REQUIRE_CHROMIUM") == "1":
            raise
        pytest.skip(f"Chromium not available: {type(exc).__name__}")


async def _local_session(p):
    browser = await _launch(p)
    bridge = BrowserUseBridge()
    session = Session(id="cloak-c", cdp_url="local-cloak-owned", live_url=None, provider="cloak", browser=browser)
    session.context = await browser.new_context()
    session.page = await session.context.new_page()
    await bridge._install_guards(session)
    bridge._sessions[session.id] = session
    return bridge, session, browser.close


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _remote_session(p):
    """Chromium reached only over CDP through the bridge's Browser Use code path."""
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
    session = Session(id="bu-c", cdp_url=f"http://127.0.0.1:{port}", live_url=None, provider="browser_use")
    bridge._sessions[session.id] = session
    await bridge._page(session)

    async def close():
        try:
            await bridge._drop_local(session)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    return bridge, session, close


PROVIDERS = {"local": _local_session, "remote": _remote_session}


def _visit(servers, provider, names, settle=2.0):
    """Open each page in a fresh session; return {name: (error message or None, title)}."""
    async def scenario():
        out = {}
        async with playwright_api.async_playwright() as p:
            for name in names:
                bridge, session, close = await PROVIDERS[provider](p)
                try:
                    error = None
                    try:
                        await bridge.open_url(session.id, servers.url(name))
                    except UnsafeURL as exc:
                        error = str(exc)
                    await asyncio.sleep(settle)
                    title = await session.page.title() if session.page else ""
                    out[name] = (error, title)
                finally:
                    await close()
        return out
    return asyncio.run(scenario())


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_worker_websockets_never_reach_internal(servers, provider):
    _visit(servers, provider, list(WORKER_PAGES))
    assert servers.lines == []


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_static_speculation_rules_are_refused_and_never_fetched(servers, provider):
    results = _visit(servers, provider, list(STATIC_RULES))
    assert servers.lines == []
    assert all(error == CAPABILITY_BLOCKED for error, _ in results.values()), results


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_runtime_speculation_rules_are_blocked_on_every_dom_path(servers, provider):
    _visit(servers, provider, list(DYNAMIC_RULES))
    assert servers.lines == []


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_positive_controls(servers, provider):
    """Legitimate pages still load and run scripts; the disabled capabilities are explicit."""
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await PROVIDERS[provider](p)
            try:
                result = await bridge.open_url(session.id, servers.url("normal"))
                title = await session.page.title()
                probe = await session.page.evaluate("""() => {
                    const out = {};
                    for (const n of ['Worker', 'SharedWorker', 'RTCPeerConnection', 'WebTransport']) {
                        try { void globalThis[n]; out[n] = 'available'; } catch (e) { out[n] = 'disabled'; }
                    }
                    out.fetch = typeof fetch; out.websocket = typeof WebSocket;
                    return out;
                }""")
                return result, title, probe
            finally:
                await close()
    result, title, probe = asyncio.run(scenario())
    assert result["final_url"].startswith(f"http://{PUB}") and title == "scripts-run"
    assert probe == {"Worker": "disabled", "SharedWorker": "disabled", "RTCPeerConnection": "disabled",
                     "WebTransport": "disabled", "fetch": "function", "websocket": "function"}
    assert servers.lines == []


def test_session_without_init_scripts_fails_closed():
    async def go():
        bridge = BrowserUseBridge()
        session = Session(id="x", cdp_url="local", live_url=None, provider="cloak")
        session.redirect_guard_active = True
        session.context = type("C", (), {})()  # no add_init_script
        await bridge._arm_capability_guard(session)
        return session
    session = asyncio.run(go())
    assert session.capability_guard_installed is False and session.redirect_guard_active is False
    with pytest.raises(UnsafeURL):
        BrowserUseBridge()._require_guard(session)
