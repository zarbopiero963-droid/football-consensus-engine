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
import json
import os
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import pytest

from bridge import browser_use, session_leases
from bridge.browser_use import BrowserUseBridge, Session
from bridge.security import UnsafeURL
from bridge.security import validate_public_http_url as _real_validate

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
# DevTools client can attach: the init script normally never reaches it (review P0). Only the
# browser-enforced document CSP does. "Normally": the init script sometimes wins that race, so
# each sandboxed payload first reports `typeof Worker` to the public server (_mark). A run that
# reports "function" is one where the init script missed the frame; the remote RED controls and
# residual xfails judge only such runs (_remote_runs_until_missed), never a race the init script won.
def _mark(name):
    return f"new Image().src='/report?v={name}&t='+typeof Worker;"


SANDBOX_PAGES = {
    "sandbox_srcdoc_worker": "<iframe sandbox=allow-scripts srcdoc=\"<script>" + _mark("sandbox_srcdoc_worker") + "try{new Worker(URL.createObjectURL(new Blob([&quot;new WebSocket('ws://INT/sbw')&quot;])))}catch(e){}</script>\"></iframe>",
    "sandbox_srcdoc_websocket": "<iframe sandbox=allow-scripts srcdoc=\"<script>" + _mark("sandbox_srcdoc_websocket") + "try{new WebSocket('ws://INT/sbws')}catch(e){}</script>\"></iframe>",
    "sandbox_srcdoc_websocketstream": "<iframe sandbox=allow-scripts srcdoc=\"<script>" + _mark("sandbox_srcdoc_websocketstream") + "try{new WebSocketStream('ws://INT/sbwss')}catch(e){}</script>\"></iframe>",
    # wss: (TLS) from the same frame: the CSP must refuse it too (no route guard reaches the frame).
    "sandbox_srcdoc_wss": "<iframe sandbox=allow-scripts srcdoc=\"<script>" + _mark("sandbox_srcdoc_wss") + "try{new WebSocket('wss://INT/sbtls')}catch(e){}</script>\"></iframe>",
}
RTC_TURN = ("<iframe sandbox=allow-scripts srcdoc=\"<script>" + _mark("rtc_turn") + "try{const pc=new RTCPeerConnection({iceServers:"
            "[{urls:'turn:INT?transport=tcp',username:'u',credential:'c'}],iceTransportPolicy:'relay'});"
            "pc.createDataChannel('x');pc.createOffer().then(o=>pc.setLocalDescription(o))}catch(e){}</script>\"></iframe>")
# WebSocket pages. The document CSP refuses ws: and wss: on both providers (owner decision: wss:
# stays blocked); page-realm sockets are also checked by the route_web_socket IP guard.
# WSSHOST is the public-like TLS WebSocket server.
WS_PAGES = {
    "wss_public": ("<script>const s=new WebSocket('wss://WSSHOST/echo');"
                   "s.onmessage=e=>{document.getElementById('ok').textContent='ws:'+e.data}</script>"),
    # Each internal vector reports first that its script ran (_mark), so the RED control below
    # judges only runs where the vector really executed.
    "wss_internal_main": "<script>" + _mark("wss_internal_main") + "try{new WebSocket('wss://INT/wm')}catch(e){}</script>",
    "ws_internal_main": "<script>try{new WebSocket('ws://INT/pm')}catch(e){}</script>",
    "wss_internal_iframe": "<iframe srcdoc=\"<script>" + _mark("wss_internal_iframe") + "try{new WebSocket('wss://INT/wf')}catch(e){}</script>\"></iframe>",
    "wss_internal_blob_iframe": ("<script>const html='<script>new Image().src=location.origin+\\'/report?v=wss_internal_blob_iframe&t=\\'+typeof Worker;"
                                 "try{new WebSocket(\\'wss://INT/wb\\')}catch(e){}<'+'/script>';"
                                 "const f=document.createElement('iframe');f.src=URL.createObjectURL(new Blob([html],"
                                 "{type:'text/html'}));document.body.appendChild(f)</script>"),
    # A live-odds-like page whose numbers would arrive over a public wss: socket.
    "live_odds": ("<table><tr><td id=match>Home v Away</td><td id=odds-home data-seq=0>-</td></tr></table>"
                  "<button id=refresh>refresh</button><script>const s=new WebSocket('wss://WSSHOST/odds');"
                  "s.onmessage=e=>{const d=JSON.parse(e.data);const c=document.getElementById('odds-home');"
                  "c.textContent=d.home;c.dataset.seq=String(d.seq)};"
                  "document.getElementById('refresh').onclick=()=>s.send('refresh')</script>"),
    # The tool-flow page: a click counter and a text input mirrored into the DOM.
    "flow_form": ("<form onsubmit='return false'><input id=q name=q placeholder=team>"
                  "<button id=go type=button>go</button><a href='/normal' id=lnk>next</a></form>"
                  "<p id=clicks>0</p><p id=echo></p><script>"
                  "document.getElementById('go').onclick=()=>{const c=document.getElementById('clicks');"
                  "c.textContent=String(Number(c.textContent)+1)};"
                  "document.getElementById('q').oninput=e=>{document.getElementById('echo').textContent=e.target.value}"
                  "</script>"),
}


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
        self.reports: list[tuple[str, str]] = []  # (vector, typeof Worker) from _mark
        self.wss_host = "wss-server-not-started"
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
                if name == "report":
                    query = dict(parse_qsl(urlsplit(self.path).query))
                    servers.reports.append((query.get("v", ""), query.get("t", "")))
                    self.send_response(204)
                    self.end_headers()
                    return
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
                page = {**WORKER_PAGES, **SANDBOX_PAGES, **SPECULATION, **POSITIVE, **WS_PAGES}.get(name, "")
                body = (f"<html><head></head><body><p id=ok>public</p>{page}</body></html>"
                        .replace("INT", servers.internal).replace("WSSHOST", servers.wss_host).encode())
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
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "true")  # the kill switch is fail-closed (F-03)


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


async def _launch(p, extra_args=()):
    try:
        return await p.chromium.launch(args=[*CHROMIUM_TEST_ARGS, *extra_args])
    except Exception as exc:  # pragma: no cover
        if os.environ.get("FCB_REQUIRE_CHROMIUM") == "1":
            raise
        pytest.skip(f"Chromium not available: {type(exc).__name__}")


async def _local_session(p, extra_args=()):
    browser = await _launch(p, extra_args)
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


async def _cdp_chromium(p, extra_args=()):
    """A Chromium process reachable only over CDP, like a Browser Use cloud browser."""
    executable = p.chromium.executable_path
    if not executable or not os.path.exists(executable):  # pragma: no cover
        if os.environ.get("FCB_REQUIRE_CHROMIUM") == "1":
            raise RuntimeError("Chromium executable not found")
        pytest.skip("Chromium executable not found")
    port = _free_port()
    proc = subprocess.Popen([executable, "--headless=new", "--no-sandbox", f"--remote-debugging-port={port}",
                             "--remote-debugging-address=127.0.0.1", *CHROMIUM_TEST_ARGS, *extra_args,
                             "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            await asyncio.sleep(0.2)
    return port, proc


async def _remote_session(p, extra_args=()):
    """Chromium reached only over CDP through the bridge's Browser Use code path."""
    port, proc = await _cdp_chromium(p, extra_args)
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


def _visit(servers, provider, names, settle=2.0, await_leak=0.0, extra_args=()):
    """Open each page in a fresh session; return {name: (error or None, title, internal lines)}.

    `await_leak` (RED controls only): keep the page open up to that many seconds until its
    vector reaches the internal listener, so a slow runner cannot attribute the leak to the
    next page or miss it (the earlier fixed settle made the RED control flaky under load).
    GREEN tests never pass it: they assert that nothing arrives within the settle window."""
    async def scenario():
        out = {}
        async with playwright_api.async_playwright() as p:
            for name in names:
                bridge, session, close = await PROVIDERS[provider](p, extra_args)
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


MARKER_ATTEMPTS = 6  # per vector: runs until one where the init script missed the frame


def _remote_runs_until_missed(servers, name, leaks, await_leak=8.0, extra_args=(), judged=("function",)):
    """Remote path, sandboxed srcdoc vectors: open `name` in a fresh session up to
    MARKER_ATTEMPTS times, stopping at the first run whose frame reports typeof Worker ==
    "function" (the init script missed it: the vector is really exercised). Returns
    [(marker, leaked)], marker being the reported typeof (None if no report arrived) and leaked
    whether `leaks()` grew during the run. A judged run waits up to `await_leak` seconds.
    `judged` (default: "function") is the set of markers that make a run count."""
    runs = []
    for _ in range(MARKER_ATTEMPTS):
        async def scenario():
            async with playwright_api.async_playwright() as p:
                bridge, session, close = await _remote_session(p, extra_args)
                try:
                    before, reports_before = leaks(), len(servers.reports)
                    await bridge.open_url(session.id, servers.url(name))
                    marker, deadline = None, time.monotonic() + 8.0
                    while marker is None and time.monotonic() < deadline:
                        marker = next((t for v, t in servers.reports[reports_before:] if v == name), None)
                        await asyncio.sleep(0.1)
                    if marker in judged:
                        deadline = time.monotonic() + await_leak
                        while leaks() == before and time.monotonic() < deadline:
                            await asyncio.sleep(0.2)
                    else:
                        await asyncio.sleep(1.0)
                    return marker, leaks() > before
                finally:
                    await close()
        runs.append(asyncio.run(scenario()))
        if runs[-1][0] in judged:
            break
    return runs


class ResidualNotExercised(RuntimeError):
    """The init script won the race in every run: the residual was not exercised. Not an
    AssertionError, so a strict xfail(raises=AssertionError) reports it as a real failure."""


def _require_missed_run(runs):
    if not any(marker == "function" for marker, _ in runs):
        raise ResidualNotExercised(f"no run escaped the init script in {MARKER_ATTEMPTS} attempts: {runs}")


@red_profile_only
@pytest.mark.parametrize("name", sorted(SANDBOX_PAGES))
def test_control_sandboxed_srcdoc_escapes_the_init_script_on_the_remote_path(servers, monkeypatch, name):
    """Review P0 RED: init script ON, document CSP OFF -> a sandboxed srcdoc frame on the remote
    (Browser Use / CDP) path still reaches the internal host. Only the CSP closes it.

    Deterministic: the init script sometimes wins the race into the frame (it then reports
    typeof Worker "undefined" and cannot leak). Every run reporting "function" must leak, and at
    least one such run must be observed within MARKER_ATTEMPTS."""
    _guards_off(monkeypatch, init_script=False, csp=True)
    runs = _remote_runs_until_missed(servers, name, lambda: len(servers.lines))
    assert any(marker == "function" for marker, _ in runs), runs
    assert all(leaked for marker, leaked in runs if marker == "function"), runs


@red_profile_only
@pytest.mark.xfail(strict=True, reason="WP-002b residual (P1, documented): speculation-rules "
                   "prefetch/prerender is issued by the browser process and cannot be blocked over CDP; "
                   "needs an egress proxy. Browser stays behind the kill switch. A pass here means the "
                   "residual is gone: update the registry and remove the xfail.")
def test_speculation_rules_residual_is_still_open(servers):
    _visit(servers, "local", ["static_inline"])
    assert servers.lines == []


@red_profile_only
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="WP-002b residual (P1, documented): a sandboxed srcdoc frame on "
                   "the remote path keeps RTCPeerConnection (the init script cannot reach it) and no CSP "
                   "directive governs WebRTC in Chromium: a TURN-over-TCP allocation reaches an internal "
                   "host. Needs an egress proxy. A pass means the residual is gone: update the registry.")
def test_webrtc_from_a_sandboxed_srcdoc_residual_is_still_open(servers):
    # Judged only on a run where the init script missed the frame (see _mark); a run it won
    # proves nothing either way and used to flip this strict xfail to XPASS.
    runs = _remote_runs_until_missed(servers, "rtc_turn", lambda: len(servers.lines))
    _require_missed_run(runs)
    assert not any(leaked for marker, leaked in runs if marker == "function"), runs


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


def _rtc_udp_page(udp):
    js = (_mark("rtc_udp_sandbox") +
          f"try{{const pc=new RTCPeerConnection({{iceServers:[{{urls:'stun:{INT}:{udp.port}'}}]}});"
          "pc.createDataChannel('x');pc.createOffer().then(o=>pc.setLocalDescription(o))}catch(e){}")
    POSITIVE["rtc_udp_sandbox"] = f"<iframe sandbox=allow-scripts srcdoc=\"<script>{js}</script>\"></iframe>"
    return "rtc_udp_sandbox"


def _webtransport_page(udp):
    js = _mark("wt_sandbox") + f"try{{new WebTransport('https://{INT}:{udp.port}/x').ready.catch(()=>{{}})}}catch(e){{}}"
    POSITIVE["wt_sandbox"] = f"<iframe sandbox=allow-scripts srcdoc=\"<script>{js}</script>\"></iframe>"
    return "wt_sandbox"


@red_profile_only
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="WP-002b residual (P1, documented): a sandboxed srcdoc frame on "
                   "the remote path keeps WebTransport (the init script cannot reach it) and its https: URL "
                   "matches connect-src like fetch: QUIC packets reach an internal host. Needs an egress "
                   "proxy. A pass means the residual is gone: update the registry.")
def test_webtransport_from_a_sandboxed_srcdoc_residual_is_still_open(servers, udp):
    runs = _remote_runs_until_missed(servers, _webtransport_page(udp), lambda: udp.packets, await_leak=10.0)
    _require_missed_run(runs)
    assert not any(leaked for marker, leaked in runs if marker == "function"), runs


@red_profile_only
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="WP-002b residual (P1, documented): WebRTC STUN over UDP from a "
                   "sandboxed srcdoc frame on the remote path reaches an internal host (no CSP directive "
                   "governs WebRTC). Needs an egress proxy. A pass means the residual is gone.")
def test_webrtc_udp_from_a_sandboxed_srcdoc_residual_is_still_open(servers, udp):
    runs = _remote_runs_until_missed(servers, _rtc_udp_page(udp), lambda: udp.packets, await_leak=10.0)
    _require_missed_run(runs)
    assert not any(leaked for marker, leaked in runs if marker == "function"), runs


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
                    // The document CSP refuses every ws: and wss: socket, on both paths.
                    for (const [key, url] of [['ws', wsUrl], ['wss', wsUrl.replace('ws://', 'wss://')]]) {
                        const violations = [];
                        const listener = e => violations.push(e.effectiveDirective);
                        document.addEventListener('securitypolicyviolation', listener);
                        try { new WebSocket(url); } catch (e) { violations.push('threw:' + e.name); }
                        await new Promise(r => setTimeout(r, 300));
                        document.removeEventListener('securitypolicyviolation', listener);
                        out[key + '_refused_by'] = violations.filter(v => v === 'connect-src').length ? 'connect-src' : violations.join(',');
                    }
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
                     "fetch": "function", "websocket": "function", "ws_refused_by": "connect-src",
                     # Test extended (not a behaviour change): wss: is probed as well as ws:.
                     "wss_refused_by": "connect-src"}
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


@pytest.mark.parametrize("url", ["ws://127.0.0.1/", "wss://127.0.0.1:8443/x", "wss://10.0.0.5/", "wss://192.168.1.1/",
                                 "wss://172.16.0.1/", "wss://169.254.169.254/latest/meta-data", "wss://[::1]/",
                                 "wss://[fd00::1]/", "wss://[::ffff:127.0.0.1]/", "wss://100.64.0.1/",
                                 "wss://0.0.0.0/", "wss://user:pw@93.184.216.34/"])
def test_websocket_verdict_refuses_private_loopback_and_metadata(monkeypatch, url):
    """The route_web_socket IP guard's verdict, with the REAL validator (IP literals: no DNS)."""
    monkeypatch.setattr(browser_use, "validate_public_http_url", _real_validate)
    assert asyncio.run(browser_use.request_verdict(url)) is False
    assert asyncio.run(browser_use.request_verdict("wss://93.184.216.34/odds")) is True


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


# -------------------------------------------------------------------------------------------------
# Browser Use restored (owner decision "RESTORE BROWSER USE + CLOAKBROWSER", option B: wss: stays
# blocked): WebSocket refusal and the real tool flow, on the remote/CDP path. LOCAL EVIDENCE ONLY: a
# local Chromium reached
# over CDP stands in for a Browser Use cloud browser and the Browser Use REST API is a fake
# transport. No cloud call and no key: a real cloud smoke test is the owner's, after deploy.
# -------------------------------------------------------------------------------------------------
TLS_TEST_ARGS = ["--ignore-certificate-errors"]  # TEST ONLY: the public-like wss server is self-signed
# Public-wss refusal tests only, in BOTH profiles: the "public" wss server is really on loopback,
# and native Chromium's Local Network Access refuses that socket
# (net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS, measured), which a real public site would not
# trigger. With LNA off the refusal can only come from the bridge's CSP. Tests towards internal
# hosts never use this: they run with the profile's own LNA setting.
PUBLIC_WS_ARGS = TLS_TEST_ARGS + ["--disable-features=LocalNetworkAccessChecks"]
ODDS = ["1.85", "2.10", "2.25"]


class _Wss:
    """A public-like TLS WebSocket server on PUB. /echo greets; /odds pushes live-odds updates."""

    def __init__(self, workdir):
        try:
            from websockets.sync.server import serve
            cert, key = workdir / "wss-cert.pem", workdir / "wss-key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(cert), "-days", "1", "-subj", f"/CN={PUB}"],
                           check=True, capture_output=True, timeout=60)
        except (ImportError, OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            if os.environ.get("FCB_REQUIRE_CHROMIUM") == "1":
                raise
            pytest.skip(f"TLS WebSocket test server not available: {type(exc).__name__}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.paths: list[str] = []
        self.server = serve(self._handle, PUB, 0, ssl=context, compression=None)
        self.host = f"{PUB}:{self.server.socket.getsockname()[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handle(self, conn):
        path = conn.request.path
        self.paths.append(path)
        try:
            if path == "/odds":
                seq = 1
                conn.send(json.dumps({"seq": seq, "home": ODDS[0]}))
                for message in conn:
                    if message == "refresh" and seq < len(ODDS):
                        seq += 1
                        conn.send(json.dumps({"seq": seq, "home": ODDS[seq - 1]}))
            else:
                conn.send("hello")
                for _ in conn:
                    pass
        except Exception:
            pass

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def wss(servers, tmp_path):
    w = _Wss(tmp_path)
    servers.wss_host = w.host
    yield w
    w.close()


def _ws_scenario(servers, provider, name, read_js, timeout=10.0):
    """Open `name` on `provider` (PUBLIC_WS_ARGS); return (result of `read_js` polled until truthy or
    timeout, blocked_requests of the navigation)."""
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, close = await PROVIDERS[provider](p, PUBLIC_WS_ARGS)
            try:
                result = await bridge.open_url(session.id, servers.url(name), wait_until="load")
                deadline, value = time.monotonic() + timeout, None
                while time.monotonic() < deadline:
                    value = await session.page.evaluate(read_js)
                    if value:
                        break
                    await asyncio.sleep(0.2)
                return value, result["blocked_requests"]
            finally:
                await close()
    return asyncio.run(scenario())


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_public_wss_websocket_is_refused_on_both_paths(servers, wss, provider):
    """Owner decision (option B): wss: stays blocked on BOTH providers, as on main. A public wss:
    socket is refused by the document CSP (connect-src) and never reaches the server."""
    text, _ = _ws_scenario(servers, provider, "wss_public",
                           "document.getElementById('ok').textContent !== 'public' && document.getElementById('ok').textContent",
                           timeout=3.0)
    assert text is False and wss.paths == [] and servers.lines == []


WS_INTERNAL = ["wss_internal_main", "ws_internal_main", "wss_internal_iframe", "wss_internal_blob_iframe"]


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_websockets_to_internal_hosts_stay_refused(servers, wss, provider):
    """Negative: ws:/wss: sockets to private/loopback targets from the main frame, a srcdoc iframe
    and a blob: document never arrive (document CSP, and the route_web_socket IP guard behind it).
    The RED control below proves these vectors are real."""
    results = _visit(servers, provider, WS_INTERNAL, extra_args=TLS_TEST_ARGS)
    assert all(error is None for error, _, _ in results.values()), results
    assert servers.lines == []


@red_profile_only
def test_control_internal_wss_leaks_without_the_websocket_guards(servers, wss, monkeypatch):
    """RED: with the document CSP off and the route_web_socket IP guard disarmed, the wss: vectors
    above reach the internal host on the Browser Use path, so the negative test is not vacuous."""
    async def no_route(self, session):
        session.websocket_guard_installed = True
    monkeypatch.setattr(BrowserUseBridge, "_arm_websocket_guard", no_route)
    _guards_off(monkeypatch, init_script=False, csp=True)
    for name in ("wss_internal_main", "wss_internal_iframe", "wss_internal_blob_iframe"):
        # Judged on runs where the vector's script reported it ran (any typeof): every such run
        # must leak, and one must occur within MARKER_ATTEMPTS (a page whose script never ran
        # under load proves nothing either way).
        runs = _remote_runs_until_missed(servers, name, lambda: len(servers.lines),
                                         extra_args=TLS_TEST_ARGS, judged=("function", "undefined"))
        assert runs[-1][0] is not None and all(leaked for marker, leaked in runs if marker is not None), (name, runs)


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_wss_from_a_sandboxed_srcdoc_is_blocked(servers, wss, provider):
    """No residual with wss: blocked: the CSP inherited by the sandboxed srcdoc frame refuses it on
    both paths (its RED control is test_control_sandboxed_srcdoc_escapes_..., vector
    sandbox_srcdoc_wss)."""
    _visit(servers, provider, ["sandbox_srcdoc_wss"], extra_args=TLS_TEST_ARGS)
    assert servers.lines == []


class _FakeLeases:
    """In-memory stand-in for bridge.session_leases with the same state machine (no DB)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def _row(self, session_id, owner_id, states):
        row = self.rows.get(session_id)
        return row if row and row["owner_id"] == owner_id and row["state"] in states else None

    def register(self, session_id, owner_id, seconds=300):
        now = time.time()
        self.rows[session_id] = {"session_id": session_id, "owner_id": owner_id, "state": "ACTIVE",
                                 "created_at": now, "last_activity_at": now, "lease_until": now + seconds,
                                 "updated_at": now, "stop_error": None}

    def renew(self, session_id, owner_id, seconds=300):
        if not self._row(session_id, owner_id, {"ACTIVE"}):
            raise RuntimeError("Browser lease is no longer active or owned by this worker")
        self.rows[session_id]["last_activity_at"] = time.time()

    def claim_stop(self, session_id, owner_id):
        row = self._row(session_id, owner_id, {"ACTIVE", "STOP_FAILED"})
        if row:
            row["state"] = "STOPPING"
        return bool(row)

    def renew_stop(self, session_id, owner_id):
        if not self._row(session_id, owner_id, {"STOPPING"}):
            raise RuntimeError("Browser stop is no longer owned by this reaper")

    def finish(self, session_id, owner_id, success, error="", retry_seconds=60):
        row = self._row(session_id, owner_id, {"STOPPING"})
        if row is None:
            raise RuntimeError("Browser stop ownership changed before completion")
        row.update(state="STOPPED" if success else "STOP_FAILED", stop_error=error)

    def get_session(self, session_id):
        row = self.rows.get(session_id)
        return {k: v for k, v in row.items() if k != "owner_id"} if row else None

    def list_sessions(self, include_stopped=False, limit=200):
        return [self.get_session(s) for s, r in self.rows.items()
                if include_stopped or r["state"] != "STOPPED"][:limit]


@pytest.fixture()
def mcp_browser_use(monkeypatch):
    """The real MCP tool layer (bridge.server) on the Browser Use provider: fake Browser Use REST
    API (its cdpUrl points at a local CDP Chromium) and in-memory leases. Yields (call, api)."""
    import httpx

    from bridge import server as srv
    monkeypatch.setenv("BROWSER_USE_API_KEY", "fake-test-key-not-a-secret")
    monkeypatch.delenv("BROWSER_USE_EXPOSE_LIVE_URL", raising=False)
    leases = _FakeLeases()
    for fn in ("register", "renew", "claim_stop", "renew_stop", "finish", "get_session", "list_sessions"):
        monkeypatch.setattr(session_leases, fn, getattr(leases, fn))

    class Api:
        cdp_url = None
        requests: list = []

        def __call__(self, request):
            body = json.loads(request.content or b"{}")
            self.requests.append((request.method, request.url.path, body,
                                  request.headers.get("X-Browser-Use-API-Key") == "fake-test-key-not-a-secret"))
            if request.method == "POST" and request.url.path == "/api/v4/browsers":
                return httpx.Response(200, json={"id": "bu-flow-1", "status": "active",
                                                 "cdpUrl": self.cdp_url, "liveUrl": None})
            if request.method == "PATCH" and request.url.path == "/api/v4/browsers/bu-flow-1" and body == {"action": "stop"}:
                return httpx.Response(200, json={"id": "bu-flow-1", "status": "stopped"})
            return httpx.Response(404)

    api = Api()
    api.requests = []
    real = httpx.AsyncClient
    monkeypatch.setattr(browser_use.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(api), **k))

    async def call(name, **arguments):
        result = await srv.mcp.call_tool(name, arguments)
        assert not getattr(result, "is_error", False), (name, result)
        return result.structured_content

    yield call, api
    srv.bridge._sessions.pop("bu-flow-1", None)


def _run_with_cdp_chromium(api, flow):
    async def scenario():
        async with playwright_api.async_playwright() as p:
            port, proc = await _cdp_chromium(p, PUBLIC_WS_ARGS)
            api.cdp_url = f"http://127.0.0.1:{port}"
            try:
                return await flow(), port
            finally:
                proc.terminate()
                proc.wait(timeout=10)
    return asyncio.run(scenario())


def test_browser_use_tool_flow_end_to_end(servers, mcp_browser_use):
    """create -> open public page -> html, elements -> click -> type -> wait -> session list ->
    stop, through the registered MCP tools on the Browser Use (CDP) provider path."""
    call, api = mcp_browser_use

    async def flow():
        out = {"create": await call("browser_create")}
        sid = out["create"]["session_id"]
        out["open"] = await call("browser_open_url", session_id=sid, url=servers.url("flow_form"), wait_until="load")
        out["html"] = await call("browser_get_html", session_id=sid)
        out["clicks_html"] = await call("browser_get_html", session_id=sid, selector="#clicks")
        out["elements"] = await call("browser_get_elements", session_id=sid)
        out["click"] = await call("browser_click", session_id=sid, selector="#go")
        out["clicks"] = await call("browser_get_dom", session_id=sid, selector="#clicks")
        out["type"] = await call("browser_type", session_id=sid, selector="#q", text="Inter Milan")
        out["wait"] = await call("browser_wait", session_id=sid, milliseconds=200)
        out["echo"] = await call("browser_get_dom", session_id=sid, selector="#echo")
        out["listed"] = await call("browser_list_sessions")
        out["stop"] = await call("browser_stop", session_id=sid)
        out["listed_after"] = await call("browser_list_sessions")
        out["status_after"] = await call("browser_session_status", session_id=sid)
        try:
            await call("browser_get_html", session_id=sid)
            out["after_stop"] = "served"
        except Exception:
            out["after_stop"] = "refused"
        return out

    out, port = _run_with_cdp_chromium(api, flow)
    assert out["create"] == {"session_id": "bu-flow-1", "status": "active", "live_view_available": False}
    assert out["open"]["final_url"] == servers.url("flow_form") and out["open"]["http_status"] == 200
    assert out["open"]["blocked_requests"] == 0 and out["open"]["documents_refused"] == 0
    assert 'id="go"' in out["html"]["html"] and out["clicks_html"]["html"] == '<p id="clicks">0</p>'
    items = {(i["tag"], i["id"]): i for i in out["elements"]["items"]}
    assert items[("input", "q")]["placeholder"] == "team" and items[("button", "go")]["text"] == "go"
    assert items[("a", "lnk")]["href"] == "/normal"
    assert out["click"]["status"] == "clicked" and out["clicks"]["text"] == "1"
    assert out["type"]["status"] == "typed" and "Inter Milan" not in json.dumps(out["type"])  # never echoed
    assert out["wait"]["waited_ms"] == 200 and out["echo"]["text"] == "Inter Milan"
    listed = {s["session_id"]: s for s in out["listed"]["sessions"]}
    assert listed["bu-flow-1"]["state"] == "ACTIVE" and listed["bu-flow-1"]["provider"] == "browser_use"
    assert listed["bu-flow-1"]["local"] is True
    assert out["stop"] == {"session_id": "bu-flow-1", "status": "stopped"}
    assert out["listed_after"]["count"] == 0 and out["status_after"]["state"] == "STOPPED"
    assert out["after_stop"] == "refused"
    # The provider saw exactly one create and one stop, both with the API key.
    assert [(m, path, body, keyed) for m, path, body, keyed in api.requests] == [
        ("POST", "/api/v4/browsers", {}, True), ("PATCH", "/api/v4/browsers/bu-flow-1", {"action": "stop"}, True)]
    # The CDP endpoint is never returned to the client (CLAUDE.md §2.1).
    assert f"127.0.0.1:{port}" not in json.dumps(out) and "cdp" not in json.dumps(out).lower()
    assert servers.lines == []


def test_browser_use_live_odds_over_wss_stay_at_the_load_state(servers, wss, mcp_browser_use):
    """Declared limit (option B): a live-odds page fed over a public wss: socket shows only the state
    at load through the tools; the socket is refused and never reaches the server."""
    call, api = mcp_browser_use

    async def flow():
        sid = (await call("browser_create"))["session_id"]
        try:
            await call("browser_open_url", session_id=sid, url=servers.url("live_odds"), wait_until="load")
            await call("browser_wait", session_id=sid, milliseconds=2000)
            text = (await call("browser_get_dom", session_id=sid, selector="#odds-home"))["text"]
            html = (await call("browser_get_html", session_id=sid, selector="#odds-home"))["html"]
            return text, html
        finally:
            await call("browser_stop", session_id=sid)

    (text, html), _ = _run_with_cdp_chromium(api, flow)
    assert text == "-" and html == '<td id="odds-home" data-seq="0">-</td>'
    assert wss.paths == [] and servers.lines == []
