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
- WebRTC (TURN over TCP, STUN over UDP) and WebTransport from a sandboxed srcdoc frame on the
  remote path: no CSP directive governs WebRTC, and WebTransport's https: URLs match connect-src;
- DNS rebinding: the bridge's resolver and Chromium's can disagree.

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


REGRESSION_RESPONSES = {
    # (status, headers, body) for the re-serve regression matrix (review item 5)
    "redir": lambda sv: (302, [("Location", "/normal")], b""),
    "cookie": lambda sv: (200, [("Content-Type", "text/html"), ("Set-Cookie", "fcb=1; Path=/"),
                                ("Set-Cookie", "fcb2=2; Path=/")],
                          b"<script>document.title='cookie-ok'</script>"),
    # The site's own CSP must still be enforced next to ours: its inline script must NOT run.
    "site_csp": lambda sv: (200, [("Content-Type", "text/html"), ("Content-Security-Policy", "script-src 'none'")],
                            b"<title>site-csp</title><script>document.title='script-ran'</script>"),
    "latin1": lambda sv: (200, [("Content-Type", "text/html; charset=iso-8859-1")],
                          "<title>caf\u00e9</title>".encode("latin-1")),
    "status404": lambda sv: (404, [("Content-Type", "text/html")], b"<title>nf</title>"),
    "cached": lambda sv: (200, [("Content-Type", "text/html"), ("Cache-Control", "max-age=600")],
                          b"<title>cached</title><iframe sandbox=allow-scripts srcdoc=\"<script>try{new WebSocket('ws://"
                          + sv.internal.encode() + b"/cached')}catch(e){}</script>\"></iframe>"),
    "big": lambda sv: (200, [("Content-Type", "text/html")],
                       b"<title>big</title>" + b"x" * (browser_use.DOCUMENT_BODY_MAX_BYTES + 1)),
}


class _Servers:
    def __init__(self):
        self.lines: list[bytes] = []
        self.hits: dict[str, int] = {}
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
                servers.hits[name] = servers.hits.get(name, 0) + 1
                extra = REGRESSION_RESPONSES.get(name)
                if extra is not None:
                    status, headers, body = extra(servers)
                    self.send_response(status)
                    for k, v in headers:
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if name == "slowbody":  # a frame whose document body takes seconds to arrive
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", "20")
                    self.end_headers()
                    self.wfile.write(b"<p>")
                    self.wfile.flush()
                    time.sleep(3)
                    try:
                        self.wfile.write(b"slow body here </p>"[:17])
                    except OSError:
                        pass
                    return
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
    "slow_frame": "<iframe src='/slowbody'></iframe>",
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
        if parts.scheme not in ("http", "https") or parts.hostname not in (PUB, REBIND_HOST):
            raise UnsafeURL("URL is not public")
        return url

    monkeypatch.setattr(browser_use, "validate_public_http_url", validate)
    monkeypatch.setattr(browser_use, "_verdict_cache", {})
    monkeypatch.setattr(session_leases, "renew", lambda *a, **k: None)
    monkeypatch.setattr(session_leases, "register", lambda *a, **k: None)
    monkeypatch.delenv("BROWSER_TOOLS_ENABLED", raising=False)


# Two Chromium profiles, both run in CI (TEST ENVIRONMENT ONLY; production flags are never changed):
#   lna_off (default)  Chromium's own Local Network Access checks are OFF, so the bridge's guards are
#                      proven without relying on a browser policy a provider can change: the RED
#                      controls and the residual xfails run here.
#   native             normal Chromium configuration: the GREEN tests must hold as well. RED controls
#                      and residual xfails are skipped (native LNA can mask a vector; that is the
#                      browser, not the bridge) — the residuals stay OPEN regardless.
PROFILE = os.environ.get("FCB_CHROMIUM_PROFILE", "lna_off")
assert PROFILE in {"lna_off", "native"}, PROFILE
REBIND_HOST = "rebind.test"  # the bridge's validator sees it as public; Chromium resolves it to INT
CHROMIUM_TEST_ARGS = [f"--host-resolver-rules=MAP {REBIND_HOST} {INT}"] + (
    ["--disable-features=LocalNetworkAccessChecks"] if PROFILE == "lna_off" else [])
red_profile_only = pytest.mark.skipif(PROFILE != "lna_off", reason="RED controls and residuals run in the "
                                      "lna_off profile (native LNA can mask a vector)")


async def _launch(p):
    try:
        return await p.chromium.launch(args=CHROMIUM_TEST_ARGS)
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
                             "--remote-debugging-address=127.0.0.1", *CHROMIUM_TEST_ARGS, "about:blank"],
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


def _visit(servers, provider, names, settle=2.0, await_leak=0.0):
    """Open each page in a fresh session; return {name: (error or None, title, internal lines)}.

    `await_leak` (RED controls only): keep the page open up to that many seconds until its
    vector reaches the internal listener, so a slow runner cannot attribute the leak to the
    next page or miss it (the earlier fixed settle made the RED control flaky under load).
    GREEN tests never pass it: they assert that nothing arrives within the settle window."""
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
                    deadline = time.monotonic() + await_leak
                    while len(servers.lines) == before and time.monotonic() < deadline:
                        await asyncio.sleep(0.2)
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


@red_profile_only
@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_control_every_worker_vector_leaks_without_the_guards(servers, provider, monkeypatch):
    """Review P2: not vacuous. With both layers off every vector reaches the internal host."""
    _guards_off(monkeypatch, init_script=True, csp=True)
    results = _visit(servers, provider, list(WORKER_PAGES), await_leak=8.0)
    expected = {name: True for name in WORKER_PAGES}
    if provider == "remote":
        # Full Chrome's popup blocker refuses window.open without a user gesture (window.open
        # returns null, verified): not a vector on that path, and the test says so explicitly.
        expected["popup_worker"] = False
    assert {name: bool(lines) for name, (_, _, lines) in results.items()} == expected


@red_profile_only
def test_control_sandboxed_srcdoc_escapes_the_init_script_on_the_remote_path(servers, monkeypatch):
    """Review P0 RED: init script ON, document CSP OFF -> a sandboxed srcdoc frame on the remote
    (Browser Use / CDP) path still reaches the internal host. Only the CSP closes it."""
    _guards_off(monkeypatch, init_script=False, csp=True)
    results = _visit(servers, "remote", list(SANDBOX_PAGES), await_leak=8.0)
    assert {name: bool(lines) for name, (_, _, lines) in results.items()} == {name: True for name in SANDBOX_PAGES}


@red_profile_only
@pytest.mark.xfail(strict=True, reason="WP-002b residual (P1, documented): speculation-rules "
                   "prefetch/prerender is issued by the browser process and cannot be blocked over CDP; "
                   "needs an egress proxy. Browser stays behind the kill switch. A pass here means the "
                   "residual is gone: update the registry and remove the xfail.")
def test_speculation_rules_residual_is_still_open(servers):
    _visit(servers, "local", ["static_inline"])
    assert servers.lines == []


@red_profile_only
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


class _Udp:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((INT, 0))
        self.port = self.sock.getsockname()[1]
        self.packets = 0
        threading.Thread(target=self._rx, daemon=True).start()

    def _rx(self):
        while True:
            try:
                self.sock.recvfrom(65535)
            except OSError:
                return
            self.packets += 1


@pytest.fixture()
def udp():
    u = _Udp()
    yield u
    u.sock.close()


def _wait_for_packets(udp, seconds=10.0):
    """Review P3-4: wait for the residual to show up before asserting, so load cannot turn a
    strict xfail into a false 'residual closed'."""
    deadline = time.monotonic() + seconds
    while udp.packets == 0 and time.monotonic() < deadline:
        time.sleep(0.2)


def _rtc_udp_page(udp):
    js = (f"try{{const pc=new RTCPeerConnection({{iceServers:[{{urls:'stun:{INT}:{udp.port}'}}]}});"
          "pc.createDataChannel('x');pc.createOffer().then(o=>pc.setLocalDescription(o))}catch(e){}")
    POSITIVE["rtc_udp_sandbox"] = f"<iframe sandbox=allow-scripts srcdoc=\"<script>{js}</script>\"></iframe>"
    return "rtc_udp_sandbox"


def _webtransport_page(udp):
    js = f"try{{new WebTransport('https://{INT}:{udp.port}/x').ready.catch(()=>{{}})}}catch(e){{}}"
    POSITIVE["wt_sandbox"] = f"<iframe sandbox=allow-scripts srcdoc=\"<script>{js}</script>\"></iframe>"
    return "wt_sandbox"


@red_profile_only
@pytest.mark.xfail(strict=True, reason="WP-002b residual (P1, documented): a sandboxed srcdoc frame on "
                   "the remote path keeps WebTransport (the init script cannot reach it) and its https: URL "
                   "matches connect-src like fetch: QUIC packets reach an internal host. Needs an egress "
                   "proxy. A pass means the residual is gone: update the registry.")
def test_webtransport_from_a_sandboxed_srcdoc_residual_is_still_open(servers, udp):
    _visit(servers, "remote", [_webtransport_page(udp)], settle=3.0)
    _wait_for_packets(udp)
    assert udp.packets == 0


@red_profile_only
@pytest.mark.xfail(strict=True, reason="WP-002b residual (P1, documented): WebRTC STUN over UDP from a "
                   "sandboxed srcdoc frame on the remote path reaches an internal host (no CSP directive "
                   "governs WebRTC). Needs an egress proxy. A pass means the residual is gone.")
def test_webrtc_udp_from_a_sandboxed_srcdoc_residual_is_still_open(servers, udp):
    _visit(servers, "remote", [_rtc_udp_page(udp)], settle=3.0)
    _wait_for_packets(udp)
    assert udp.packets == 0


@red_profile_only
@pytest.mark.xfail(strict=True, reason="WP-002b residual (P3, documented): DNS rebinding. The bridge "
                   "validates the name with its own resolver while Chromium connects to whatever its "
                   "resolver returns (here mapped to the internal host). Needs an egress proxy that "
                   "validates the connected IP. A pass means the residual is gone.")
@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_dns_rebinding_residual_is_still_open(servers, provider):
    port = servers.internal.split(":")[1]
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await PROVIDERS[provider](p)
            try:
                try:
                    await bridge.open_url(session.id, f"http://{REBIND_HOST}:{port}/rebind", timeout_ms=4000)
                except Exception:
                    pass
            finally:
                await close()
    asyncio.run(scenario())
    assert servers.lines == []


def test_webtransport_and_webrtc_from_a_sandboxed_srcdoc_are_blocked_on_the_local_path(servers, udp):
    _visit(servers, "local", [_webtransport_page(udp), "rtc_turn", _rtc_udp_page(udp)], settle=3.0)
    assert udp.packets == 0 and servers.lines == []


@red_profile_only
def test_control_webtransport_and_webrtc_fire_on_the_local_path_without_the_guards(servers, udp, monkeypatch):
    """Review P3: the local GREEN above is not vacuous."""
    _guards_off(monkeypatch, init_script=True, csp=True)
    counts = {}
    for name in (_webtransport_page(udp), _rtc_udp_page(udp)):
        before = udp.packets
        _visit(servers, "local", [name], settle=3.0)
        counts[name] = udp.packets - before
    results = _visit(servers, "local", ["rtc_turn"], settle=3.0)
    assert all(counts.values()) and results["rtc_turn"][2], counts


def test_a_cancelled_frame_is_not_reported_as_a_redirect_into_the_next_navigation(servers):
    """Review P2 regression: a frame whose re-serve is cancelled by the next navigation must not
    make that next, fully public navigation fail with REDIRECT_BLOCKED."""
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await PROVIDERS["local"](p)
            try:
                await bridge.open_url(session.id, servers.url("slow_frame"), wait_until="commit")
                result = await bridge.open_url(session.id, servers.url("normal"), wait_until="load")
                return result, await session.page.title()
            finally:
                await close()
    result, title = asyncio.run(scenario())
    assert title == "scripts-run" and result["blocked_requests"] == 0


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_re_serving_documents_keeps_navigation_semantics(servers, provider):
    """Review item 5: redirects, cookies, the site's own CSP, charset, status, HTTP cache, cap."""
    async def scenario():
        out = {}
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await PROVIDERS[provider](p)
            try:
                async def visit(name):
                    try:
                        result = await bridge.open_url(session.id, servers.url(name), wait_until="load")
                    except UnsafeURL as exc:
                        return ("refused", str(exc))
                    await asyncio.sleep(0.3)
                    return (result["final_url"].rsplit("/", 1)[-1], result["http_status"],
                            await session.page.title(), result["documents_refused"])
                for name in ("redir", "cookie", "site_csp", "latin1", "status404", "cached", "cached"):
                    out.setdefault(name, []).append(await visit(name))
                    if name == "cookie":
                        # document.cookie: Playwright's context.cookies() is empty for the default
                        # context over CDP whether or not documents are re-served (verified).
                        out["cookies"] = sorted(c.split("=")[0].strip() for c in
                                                (await session.page.evaluate("document.cookie")).split(";"))
                out["big"] = await visit("big")
                out["after_big"] = await visit("normal")
            finally:
                await close()
        return out
    out = asyncio.run(scenario())
    assert out["redir"] == [("normal", 200, "scripts-run", 0)]
    assert out["cookie"] == [("cookie", 200, "cookie-ok", 0)] and out["cookies"] == ["fcb", "fcb2"]
    assert out["site_csp"] == [("site_csp", 200, "site-csp", 0)]  # the site's script-src 'none' held
    assert out["latin1"] == [("latin1", 200, "caf\u00e9", 0)]
    assert out["status404"] == [("status404", 404, "nf", 0)]
    assert out["cached"] == [("cached", 200, "cached", 0)] * 2
    # Review P3-6, measured: with request interception every navigation reaches the site again,
    # with or without the document re-serve (pass-through also gives 2 hits). No cache regression
    # from the re-serve, and a cache-served document is not claimed here.
    assert servers.hits["cached"] == 2
    assert out["big"] == ("refused", browser_use.DOCUMENT_REFUSED)  # fixed code, not REDIRECT_BLOCKED
    assert out["after_big"] == ("normal", 200, "scripts-run", 0)
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
    # Fail closed, but not an SSRF verdict (review P2): no REDIRECT_BLOCKED spill-over.
    assert session.document_csp_failures == 1
    assert session.redirect_blocked is False and session.blocked_requests == 0


def test_document_over_the_cap_fails_closed(monkeypatch):
    monkeypatch.setattr(browser_use, "DOCUMENT_BODY_MAX_BYTES", 4)
    cdp, session = _respond({"responseStatusCode": 200, "responseHeaders": []}, chunks=[b"abc", b"de"])
    assert "Fetch.fulfillRequest" not in [m for m, _ in cdp.sent]
    assert cdp.sent[-1][0] == "Fetch.failRequest" and session.document_csp_failures == 1
    assert session.redirect_blocked is False and browser_use._document_buffered_bytes == 0


def test_documents_buffered_at_once_share_a_global_budget(monkeypatch):
    """Review P2: many large frames cannot exhaust the bridge's memory. The raw body AND its
    base64 copy (3 + 4 bytes here) are counted until fulfillRequest returns."""
    monkeypatch.setattr(browser_use, "DOCUMENT_BUFFER_BUDGET_BYTES", 7)
    monkeypatch.setattr(browser_use, "_document_buffered_bytes", 3)  # another document in flight
    cdp, session = _respond({"responseStatusCode": 200, "responseHeaders": []}, chunks=[b"abc"])
    assert cdp.sent[-1][0] == "Fetch.failRequest" and session.document_csp_failures == 1
    assert browser_use._document_buffered_bytes == 3 and session.document_buffered_bytes == 0
    monkeypatch.setattr(browser_use, "_document_buffered_bytes", 0)
    cdp, session = _respond({"responseStatusCode": 200, "responseHeaders": []}, chunks=[b"abc"])
    assert "Fetch.fulfillRequest" in [m for m, _ in cdp.sent]
    assert browser_use._document_buffered_bytes == 0 and session.document_buffered_bytes == 0


def test_one_session_cannot_starve_the_others(monkeypatch):
    """Review P2-1 regression: a session holding its whole share is refused, another is served."""
    monkeypatch.setattr(browser_use, "DOCUMENT_SESSION_BUDGET_BYTES", 10)
    monkeypatch.setattr(browser_use, "_document_buffered_bytes", 0)
    hog = Session(id="a", cdp_url="local", live_url=None, provider="cloak")
    hog.document_buffered_bytes = 10  # e.g. frames stalled mid-body
    browser_use._document_buffered_bytes = 10
    cdp = _FakeCDP(chunks=[b"abc"])
    asyncio.run(BrowserUseBridge()._document_response(cdp, hog, {"requestId": "r1", "responseStatusCode": 200}))
    assert cdp.sent[-1][0] == "Fetch.failRequest"
    cdp, other = _respond({"responseStatusCode": 200, "responseHeaders": []}, chunks=[b"abc"])
    assert "Fetch.fulfillRequest" in [m for m, _ in cdp.sent] and other.document_csp_failures == 0
    assert hog.document_buffered_bytes == 10 and browser_use._document_buffered_bytes == 10


def test_a_stalled_body_read_times_out_closed_and_releases_its_share(monkeypatch):
    monkeypatch.setattr(browser_use, "DOCUMENT_READ_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(browser_use, "_document_buffered_bytes", 0)

    class Stalling(_FakeCDP):
        async def send(self, method, params=None):
            if method == "IO.read" and len([m for m, _ in self.sent if m == "IO.read"]) >= 1:
                self.sent.append((method, params))
                await asyncio.sleep(5)
            return await super().send(method, params)

    cdp = Stalling(chunks=[b"abc", b"def"])
    session = Session(id="x", cdp_url="local", live_url=None, provider="cloak")
    asyncio.run(BrowserUseBridge()._document_response(cdp, session, {"requestId": "r1", "responseStatusCode": 200}))
    assert cdp.sent[-1][0] == "Fetch.failRequest" and session.document_csp_failures == 1
    assert session.document_buffered_bytes == 0 and browser_use._document_buffered_bytes == 0


def test_redirect_hop_and_network_error_are_not_re_served():
    cdp, _ = _respond({"responseStatusCode": 302, "responseHeaders": [{"name": "Location", "value": "/n"}]})
    assert cdp.sent == [("Fetch.continueRequest", {"requestId": "r1"})]
    cdp, _ = _respond({"responseErrorReason": "Failed"})
    assert cdp.sent == [("Fetch.continueRequest", {"requestId": "r1"})]


def test_a_3xx_without_location_renders_and_is_re_served():
    cdp, _ = _respond({"responseStatusCode": 302, "responseHeaders": []})
    assert "Fetch.fulfillRequest" in [m for m, _ in cdp.sent]
