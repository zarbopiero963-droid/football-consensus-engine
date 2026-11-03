"""FCB-WP-002b — browser capabilities outside the CDP request guard, locked with REAL Chromium.

Reproduced on main 25a1d8e on both provider paths, then fixed fail-closed: a WebSocket or
WebSocketStream opened INSIDE a dedicated or shared worker reached an internal host. The
Fetch request stage and route_web_socket only cover page contexts. Two layers now:
1. a context init script makes Worker, SharedWorker, RTCPeerConnection, RTCDataChannel,
   WebTransport and WebSocketStream undefined in every realm the script reaches;
2. a browser-enforced document CSP (worker-src 'none', no ws:/wss: in connect-src), appended at
   the Fetch response stage and inherited by srcdoc/about:blank/blob/data documents. It closes
   the review P0: on the remote path a sandboxed srcdoc frame commits in its own process before
   any DevTools client attaches, so layer 1 never reaches it.

NOT mitigated, and kept honest as a strict xfail:
- speculation-rules prefetch/prerender sends browser-process GET requests that CDP Fetch never
  pauses;
- Page.setPrerenderingAllowed, a data-saver override and Chromium launch flags did not stop it;
- text/DOM filtering was bypassed (character references, split writes, blob: documents);
- WebRTC (TURN over TCP) from a sandboxed srcdoc frame on the remote path: no CSP directive
  governs WebRTC in Chromium.

It needs an egress proxy that validates resolved IPs. The browser stays behind the kill switch.

The tests run offline only. 127.0.0.2 plays the public site and 127.0.0.1 the internal service,
which records every TCP connection with its first request line.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
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

PUB, INT = "127.0.0.2", "127.0.0.1"
WS_BLOB = "URL.createObjectURL(new Blob([\"new WebSocket('ws://INT/')\"]))"

WORKER_PAGES = {
    "worker": f"<script>try{{new Worker({WS_BLOB})}}catch(e){{}}</script>",
    "shared_worker": f"<script>try{{new SharedWorker({WS_BLOB})}}catch(e){{}}</script>",
    "iframe_worker": f"<iframe id=f></iframe><script>try{{new (document.getElementById('f').contentWindow.Worker)({WS_BLOB})}}catch(e){{}}</script>",
    "late_iframe_worker": f"<script>const f=document.createElement('iframe');document.body.appendChild(f);try{{new f.contentWindow.Worker({WS_BLOB})}}catch(e){{}}</script>",
    "srcdoc_worker": "<iframe srcdoc=\"<script>try{new Worker(URL.createObjectURL(new Blob([&quot;new WebSocket('ws://INT/')&quot;])))}catch(e){}</script>\"></iframe>",
    "popup_worker": f"<script>const w=window.open('about:blank');try{{new w.Worker({WS_BLOB})}}catch(e){{}}</script>",
    "websocketstream": "<script>try{new WebSocketStream('ws://INT/wss')}catch(e){}</script>",
    "worker_websocketstream": "<script>try{new Worker(URL.createObjectURL(new Blob([\"new WebSocketStream('ws://INT/')\"])))}catch(e){}</script>",
    "blob_document_worker": "<script>const html='<script>try{new Worker(URL.createObjectURL(new Blob([\\\"new WebSocket(\\'ws://INT/\\')\\\"])))}catch(e){}<'+'/script>';const f=document.createElement('iframe');f.src=URL.createObjectURL(new Blob([html],{type:'text/html'}));document.body.appendChild(f)</script>",
}
SPECULATION = {
    "static_inline": "<script type=speculationrules>{\"prefetch\":[{\"source\":\"list\",\"urls\":[\"http://INT/s\"]}]}</script>",
}
# A sandboxed srcdoc frame runs in its own process on the remote path, committed before any
# DevTools client can attach: the init script never reaches it (review P0). Only the
# browser-enforced document CSP does.
SANDBOX_PAGES = {
    "sandbox_srcdoc_worker": "<iframe sandbox=allow-scripts srcdoc=\"<script>try{new Worker(URL.createObjectURL(new Blob([&quot;new WebSocket('ws://INT/sbw')&quot;])))}catch(e){}</script>\"></iframe>",
    "sandbox_srcdoc_websocket": "<iframe sandbox=allow-scripts srcdoc=\"<script>try{new WebSocket('ws://INT/sbws')}catch(e){}</script>\"></iframe>",
    "sandbox_srcdoc_websocketstream": "<iframe sandbox=allow-scripts srcdoc=\"<script>try{new WebSocketStream('ws://INT/sbwss')}catch(e){}</script>\"></iframe>",
}
RTC_TURN = ("<iframe sandbox=allow-scripts srcdoc=\"<script>try{const pc=new RTCPeerConnection({iceServers:"
            "[{urls:'turn:INT?transport=tcp',username:'u',credential:'c'}],iceTransportPolicy:'relay'});"
            "pc.createDataChannel('x');pc.createOffer().then(o=>pc.setLocalDescription(o))}catch(e){}</script>\"></iframe>")


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
                page = {**WORKER_PAGES, **SANDBOX_PAGES, **SPECULATION, **POSITIVE}.get(name, "")
                body = (f"<html><head></head><body><p id=ok>public</p>{page}</body></html>"
                        .replace("INT", servers.internal).encode())
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                if name == "gzip_page":  # the re-served body is decoded: framing headers are dropped
                    body = gzip.compress(body)
                    self.send_header("Content-Encoding", "gzip")
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
    "rtc_turn": RTC_TURN,
    "gzip_page": "<script>document.title='gz-ok'</script>",
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
    """Open each page in a fresh session; return {name: (error or None, title, internal lines)}."""
    async def scenario():
        out = {}
        async with playwright_api.async_playwright() as p:
            for name in names:
                bridge, session, close = await PROVIDERS[provider](p)
                try:
                    before = len(servers.lines)
                    error = None
                    try:
                        await bridge.open_url(session.id, servers.url(name))
                    except UnsafeURL as exc:
                        error = str(exc)
                    await asyncio.sleep(settle)
                    title = await session.page.title() if session.page else ""
                    out[name] = (error, title, servers.lines[before:])
                finally:
                    await close()
        return out
    return asyncio.run(scenario())


def _guards_off(monkeypatch, *, init_script: bool, csp: bool):
    """RED controls only: switch a WP-002b layer off to prove the vectors are real."""
    if init_script:
        monkeypatch.setattr(browser_use, "CAPABILITY_GUARD_JS", "/* WP-002b control: guard off */")
    if csp:
        monkeypatch.setattr(BrowserUseBridge, "_document_csp_headers",
                            staticmethod(lambda headers: list(headers or [])))


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_worker_websockets_never_reach_internal(servers, provider):
    results = _visit(servers, provider, list(WORKER_PAGES) + list(SANDBOX_PAGES))
    assert servers.lines == []
    # Every page really loaded (no refusal); the RED controls below prove the vectors fire.
    assert all(error is None for error, _, _ in results.values()), results


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_control_every_worker_vector_leaks_without_the_guards(servers, provider, monkeypatch):
    """Review P2: not vacuous. With both layers off every vector reaches the internal host."""
    _guards_off(monkeypatch, init_script=True, csp=True)
    results = _visit(servers, provider, list(WORKER_PAGES))
    expected = {name: True for name in WORKER_PAGES}
    if provider == "remote":
        # Full Chrome's popup blocker refuses window.open without a user gesture (window.open
        # returns null, verified): not a vector on that path, and the test says so explicitly.
        expected["popup_worker"] = False
    assert {name: bool(lines) for name, (_, _, lines) in results.items()} == expected


def test_control_sandboxed_srcdoc_escapes_the_init_script_on_the_remote_path(servers, monkeypatch):
    """Review P0 RED: init script ON, document CSP OFF -> a sandboxed srcdoc frame on the remote
    (Browser Use / CDP) path still reaches the internal host. Only the CSP closes it."""
    _guards_off(monkeypatch, init_script=False, csp=True)
    results = _visit(servers, "remote", list(SANDBOX_PAGES))
    assert {name: bool(lines) for name, (_, _, lines) in results.items()} == {name: True for name in SANDBOX_PAGES}


@pytest.mark.xfail(strict=True, reason="WP-002b residual (P1, documented): speculation-rules "
                   "prefetch/prerender is issued by the browser process and cannot be blocked over CDP; "
                   "needs an egress proxy. Browser stays behind the kill switch. A pass here means the "
                   "residual is gone: update the registry and remove the xfail.")
def test_speculation_rules_residual_is_still_open(servers):
    _visit(servers, "local", ["static_inline"])
    assert servers.lines == []


@pytest.mark.xfail(strict=True, reason="WP-002b residual (P1, documented): a sandboxed srcdoc frame on "
                   "the remote path keeps RTCPeerConnection (the init script cannot reach it) and no CSP "
                   "directive governs WebRTC in Chromium: a TURN-over-TCP allocation reaches an internal "
                   "host. Needs an egress proxy. A pass means the residual is gone: update the registry.")
def test_webrtc_from_a_sandboxed_srcdoc_residual_is_still_open(servers):
    _visit(servers, "remote", ["rtc_turn"])
    assert servers.lines == []


def test_webrtc_from_a_sandboxed_srcdoc_is_blocked_on_the_local_path(servers):
    _visit(servers, "local", ["rtc_turn"])
    assert servers.lines == []


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_positive_controls(servers, provider):
    """Legitimate pages still load and run scripts; the disabled capabilities are explicit."""
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await PROVIDERS[provider](p)
            try:
                await bridge.open_url(session.id, servers.url("gzip_page"))
                gz_title = await session.page.title()
                result = await bridge.open_url(session.id, servers.url("normal"))
                title = await session.page.title()
                probe = await session.page.evaluate("""async (wsUrl) => {
                    const out = {};
                    for (const n of ['Worker', 'SharedWorker', 'RTCPeerConnection', 'webkitRTCPeerConnection',
                                     'RTCDataChannel', 'WebTransport', 'WebSocketStream']) {
                        // Ordinary feature detection must not throw.
                        out[n] = (typeof globalThis[n] === 'undefined' && !globalThis[n]) ? 'disabled' : 'available';
                    }
                    try { globalThis.Worker = function () {}; } catch (e) {}
                    out.redefined = typeof globalThis.Worker;
                    out.fetch = typeof fetch; out.websocket = typeof WebSocket;
                    // Behaviour change (documented): the document CSP refuses every ws:/wss: socket.
                    const violations = [];
                    document.addEventListener('securitypolicyviolation', e => violations.push(e.effectiveDirective));
                    try { new WebSocket(wsUrl); } catch (e) { violations.push('threw:' + e.name); }
                    await new Promise(r => setTimeout(r, 300));
                    out.websocket_refused_by = violations.filter(v => v === 'connect-src').length ? 'connect-src' : violations.join(',');
                    return out;
                }""", servers.url("x").replace("http://", "ws://"))
                return result, title, probe, gz_title
            finally:
                await close()
    result, title, probe, gz_title = asyncio.run(scenario())
    assert result["final_url"].startswith(f"http://{PUB}") and title == "scripts-run" and gz_title == "gz-ok"
    assert probe == {"Worker": "disabled", "SharedWorker": "disabled", "RTCPeerConnection": "disabled",
                     "webkitRTCPeerConnection": "disabled", "RTCDataChannel": "disabled",
                     "WebTransport": "disabled", "WebSocketStream": "disabled", "redefined": "undefined",
                     "fetch": "function", "websocket": "function", "websocket_refused_by": "connect-src"}
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


class _FakeCDP:
    def __init__(self, fail=(), chunks=(b"<p>x</p>",)):
        self.sent, self.fail, self.chunks = [], set(fail), list(chunks)

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method in self.fail:
            raise RuntimeError("not supported")
        if method == "Fetch.takeResponseBodyAsStream":
            return {"stream": "s1"}
        if method == "IO.read":
            data = self.chunks.pop(0) if self.chunks else b""
            return {"data": base64.b64encode(data).decode(), "base64Encoded": True, "eof": not self.chunks}
        return {}


def _respond(event, **kw):
    cdp = _FakeCDP(**kw)
    session = Session(id="x", cdp_url="local", live_url=None, provider="cloak")
    asyncio.run(BrowserUseBridge()._document_response(cdp, session, {"requestId": "r1", **event}))
    return cdp, session


def test_document_csp_is_appended_and_site_policies_are_kept():
    headers = [{"name": "Content-Type", "value": "text/html"},
               {"name": "Content-Security-Policy", "value": "default-src 'self'"},
               {"name": "Content-Encoding", "value": "gzip"}, {"name": "content-length", "value": "9"}]
    out = BrowserUseBridge._document_csp_headers(headers)
    assert out == headers[:2] + [{"name": "Content-Security-Policy", "value": browser_use.DOCUMENT_CSP}]
    assert "worker-src 'none'" in browser_use.DOCUMENT_CSP
    connect = next(d for d in browser_use.DOCUMENT_CSP.split(";") if d.strip().startswith("connect-src"))
    assert "ws:" not in connect and "wss:" not in connect and "*" not in connect and "'self'" not in connect


def test_document_is_re_served_with_the_csp_never_continued():
    cdp, session = _respond({"responseStatusCode": 200, "responseStatusText": "OK",
                             "responseHeaders": [{"name": "Content-Type", "value": "text/html"}]},
                            chunks=[b"<p>a", b"b</p>"])
    methods = [m for m, _ in cdp.sent]
    assert "Fetch.continueResponse" not in methods  # Chromium would not enforce an appended CSP
    fulfil = dict(cdp.sent)["Fetch.fulfillRequest"]
    assert base64.b64decode(fulfil["body"]) == b"<p>ab</p>" and fulfil["responseCode"] == 200
    assert fulfil["responseHeaders"][-1] == {"name": "Content-Security-Policy", "value": browser_use.DOCUMENT_CSP}
    assert ("IO.close", {"handle": "s1"}) in cdp.sent and session.redirect_blocked is False


@pytest.mark.parametrize("kw", [{"fail": {"Fetch.takeResponseBodyAsStream"}}, {"fail": {"IO.read"}},
                                {"fail": {"Fetch.fulfillRequest"}}])
def test_document_response_fails_closed_when_the_csp_cannot_be_applied(kw):
    cdp, session = _respond({"responseStatusCode": 200, "responseHeaders": []}, **kw)
    assert cdp.sent[-1] == ("Fetch.failRequest", {"requestId": "r1", "errorReason": "BlockedByClient"})
    assert session.redirect_blocked is True and session.blocked_requests == 1


def test_document_over_the_cap_fails_closed(monkeypatch):
    monkeypatch.setattr(browser_use, "DOCUMENT_BODY_MAX_BYTES", 4)
    cdp, session = _respond({"responseStatusCode": 200, "responseHeaders": []}, chunks=[b"abc", b"de"])
    assert "Fetch.fulfillRequest" not in [m for m, _ in cdp.sent]
    assert cdp.sent[-1][0] == "Fetch.failRequest" and session.redirect_blocked is True


def test_redirect_hop_and_network_error_are_not_re_served():
    cdp, _ = _respond({"responseStatusCode": 302, "responseHeaders": [{"name": "Location", "value": "/n"}]})
    assert cdp.sent == [("Fetch.continueRequest", {"requestId": "r1"})]
    cdp, _ = _respond({"responseErrorReason": "Failed"})
    assert cdp.sent == [("Fetch.continueRequest", {"requestId": "r1"})]


def test_a_3xx_without_location_renders_and_is_re_served():
    cdp, _ = _respond({"responseStatusCode": 302, "responseHeaders": []})
    assert "Fetch.fulfillRequest" in [m for m, _ in cdp.sent]
