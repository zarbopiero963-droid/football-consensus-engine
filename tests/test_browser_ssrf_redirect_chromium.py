"""FCB-WP-001 — Browser SSRF class A, reproduced and locked with a REAL local Chromium.

Offline only: two loopback HTTP servers stand in for the internet and for an internal
service. 127.0.0.2 plays a *public* attacker site, 127.0.0.1 plays an *internal* address
(metadata / RFC1918). The bridge's validator is wrapped so that exactly that pair is
classified like the real validator classifies public vs private addresses; everything
else on the bridge code path is real (route guard, CDP redirect guard, navigation and
read tools).

RED on main 342fed6: a public 30x to the internal address was followed and every read
tool returned the internal page. GREEN: the hop is failed before Chromium follows it, the
internal server is never contacted and no read tool can return internal content.
"""
from __future__ import annotations

import asyncio
import base64
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from bridge import browser_use, session_leases
from bridge.browser_use import CONTENT_BLOCKED, REDIRECT_BLOCKED, TOOLS_DISABLED, BrowserUseBridge, Session
from bridge.security import UnsafeURL

playwright_api = pytest.importorskip("playwright.async_api")

SECRET = "INTERNAL_SECRET_c4f1_DO_NOT_LEAK"
INTERNAL_HOST, PUBLIC_HOST = "127.0.0.1", "127.0.0.2"


class _Servers:
    def __init__(self) -> None:
        self.internal_hits: list[str] = []
        servers = self

        class Internal(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                servers.internal_hits.append(self.path)
                body = f"<html><head><title>{SECRET}</title></head><body><h1>{SECRET}</h1></body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Public(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                q = parse_qs(url.query)
                if url.path == "/hop":
                    # /hop?n=K&code=C&final=internal|public[&rel=1]
                    n = int(q["n"][0])
                    code = int(q.get("code", ["302"])[0])
                    final = q["final"][0]
                    if n > 1:
                        loc = f"/hop?n={n - 1}&code={code}&final={final}"
                        if "rel" not in q:
                            loc = f"http://{PUBLIC_HOST}:{servers.public_port}{loc}"
                    elif final == "internal":
                        loc = f"http://{INTERNAL_HOST}:{servers.internal_port}/secret"
                    else:
                        loc = f"http://{PUBLIC_HOST}:{servers.public_port}/ok"
                    self.send_response(code)
                    self.send_header("Location", loc)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b"<html><head><title>public</title></head><body><p id=ok>public page</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.internal = ThreadingHTTPServer((INTERNAL_HOST, 0), Internal)
        self.public = ThreadingHTTPServer((PUBLIC_HOST, 0), Public)
        self.internal_port = self.internal.server_address[1]
        self.public_port = self.public.server_address[1]
        for srv in (self.internal, self.public):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    def public_url(self, path: str) -> str:
        return f"http://{PUBLIC_HOST}:{self.public_port}{path}"

    def internal_url(self, path: str = "/secret") -> str:
        return f"http://{INTERNAL_HOST}:{self.internal_port}{path}"

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
    """Exactly the real validator's public/private decision, mapped onto the loopback pair."""
    def validate(url):
        host = urlsplit(url).hostname
        if urlsplit(url).scheme not in ("http", "https") or host != PUBLIC_HOST:
            raise UnsafeURL("URL is not public")
        return url

    monkeypatch.setattr(browser_use, "validate_public_http_url", validate)
    monkeypatch.setattr(session_leases, "renew", lambda *a, **k: None)
    monkeypatch.setattr(session_leases, "register", lambda *a, **k: None)
    monkeypatch.delenv("BROWSER_TOOLS_ENABLED", raising=False)


async def _launch(p):
    try:
        return await p.chromium.launch()
    except Exception as exc:  # pragma: no cover - environment without Chromium
        if os.environ.get("FCB_REQUIRE_CHROMIUM") == "1":
            raise  # the dedicated CI job must never silently skip
        pytest.skip(f"Chromium not available: {type(exc).__name__}")


async def _bridge_with_chromium(p):
    browser = await _launch(p)
    bridge = BrowserUseBridge()
    session = Session(id="cloak-test", cdp_url="local-cloak-owned", live_url=None, provider="cloak", browser=browser)
    session.context = await browser.new_context()
    session.page = await session.context.new_page()
    await bridge._install_guards(session)
    bridge._sessions[session.id] = session
    return bridge, session, browser


async def _read_everything(bridge, sid):
    """Every read tool; returns the concatenated text each one would hand to the MCP client."""
    out = []
    for call in (lambda: bridge.html(sid), lambda: bridge.dom_text(sid),
                 lambda: bridge.elements(sid, selector="*"), lambda: bridge.list_pages(sid)):
        try:
            out.append(repr(await call()))
        except UnsafeURL as exc:
            out.append(f"UnsafeURL:{exc}")
    try:
        shot = await bridge.screenshot(sid)
        out.append(base64.b64decode(shot["base64"])[:0].decode() + shot["url"])
    except UnsafeURL as exc:
        out.append(f"UnsafeURL:{exc}")
    return "\n".join(out)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("hops", [1, 2, 5])
def test_redirect_chain_to_internal_is_blocked_before_any_request(servers, code, hops):
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, browser = await _bridge_with_chromium(p)
            try:
                with pytest.raises(UnsafeURL) as err:
                    await bridge.open_url(session.id, servers.public_url(f"/hop?n={hops}&code={code}&final=internal"))
                assert str(err.value) == REDIRECT_BLOCKED  # fixed message, no URL echoed
                text = await _read_everything(bridge, session.id)
            finally:
                await browser.close()
        assert servers.internal_hits == []  # the internal address was never contacted
        assert SECRET not in text
        assert str(servers.internal_port) not in text
    _run(scenario())


def test_relative_location_chain_and_new_page_are_guarded(servers):
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, browser = await _bridge_with_chromium(p)
            try:
                with pytest.raises(UnsafeURL):
                    await bridge.open_url(session.id, servers.public_url("/hop?n=3&rel=1&final=internal"))
                with pytest.raises(UnsafeURL):
                    await bridge.new_page(session.id, url=servers.public_url("/hop?n=2&final=internal"))
                text = await _read_everything(bridge, session.id)
            finally:
                await browser.close()
        assert servers.internal_hits == []
        assert SECRET not in text
    _run(scenario())


def test_public_redirect_chains_still_work(servers):
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, browser = await _bridge_with_chromium(p)
            try:
                await bridge.open_url(session.id, servers.public_url("/ok?first"))
                result = await bridge.open_url(session.id, servers.public_url("/hop?n=4&code=307&final=public"))
                html = await bridge.html(session.id)
                back = await bridge.back(session.id)
                reloaded = await bridge.reload(session.id)
            finally:
                await browser.close()
        assert result["final_url"] == servers.public_url("/ok")
        assert result["http_status"] == 200
        assert "public page" in html["html"]
        assert back["url"].startswith(f"http://{PUBLIC_HOST}")
        assert reloaded["url"].startswith(f"http://{PUBLIC_HOST}")
    _run(scenario())


@pytest.mark.parametrize("tool", ["html", "dom_text", "screenshot", "elements", "switch_page"])
def test_read_tools_fail_closed_on_a_page_that_is_not_public(servers, tool):
    """Defense in depth: even if internal content got loaded by any path, reads refuse it."""
    async def scenario():
        async with playwright_api.async_playwright() as p:
            browser = await _launch(p)
            try:
                bridge = BrowserUseBridge()
                session = Session(id="cloak-raw", cdp_url="local-cloak-owned", live_url=None,
                                  provider="cloak", browser=browser)
                session.context = await browser.new_context()
                session.page = await session.context.new_page()
                await session.page.goto(servers.internal_url())  # loaded BEFORE guards exist
                await bridge._install_guards(session)
                bridge._sessions[session.id] = session
                with pytest.raises(UnsafeURL) as err:
                    if tool == "switch_page":
                        await bridge.switch_page(session.id, 0)
                    else:
                        await getattr(bridge, tool)(session.id)
                assert str(err.value) == CONTENT_BLOCKED
                listing = await bridge.list_pages(session.id)
                # the internal page was discarded; nothing internal remains anywhere
                assert SECRET not in repr(listing)
                assert all(SECRET not in body for body in [await pg.content() for pg in session.context.pages])
            finally:
                await browser.close()
    _run(scenario())


def test_history_navigation_cannot_reach_internal(servers):
    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, browser = await _bridge_with_chromium(p)
            try:
                await bridge.open_url(session.id, servers.public_url("/ok"))
                with pytest.raises(UnsafeURL):
                    await bridge.open_url(session.id, servers.public_url("/hop?n=1&final=internal"))
                for call in (bridge.back, bridge.forward, bridge.reload):
                    try:
                        await call(session.id)
                    except (UnsafeURL, Exception):
                        pass
                text = await _read_everything(bridge, session.id)
            finally:
                await browser.close()
        assert servers.internal_hits == []
        assert SECRET not in text
    _run(scenario())


def test_fuzz_random_redirect_chains(servers):
    """Seeded fuzz: random code/length/relative/final. Internal is never hit nor readable;
    all-public chains always succeed."""
    rng = random.Random(20261102)
    cases = [(rng.choice([301, 302, 303, 307, 308]), rng.randint(1, 6), rng.random() < 0.5,
              rng.choice(["internal", "public"])) for _ in range(24)]

    async def scenario():
        async with playwright_api.async_playwright() as p:
            bridge, session, browser = await _bridge_with_chromium(p)
            try:
                for code, hops, rel, final in cases:
                    url = servers.public_url(f"/hop?n={hops}&code={code}&final={final}" + ("&rel=1" if rel else ""))
                    if final == "internal":
                        with pytest.raises(UnsafeURL):
                            await bridge.open_url(session.id, url)
                        assert SECRET not in await _read_everything(bridge, session.id)
                    else:
                        result = await bridge.open_url(session.id, url)
                        assert result["final_url"] == servers.public_url("/ok")
            finally:
                await browser.close()
        assert servers.internal_hits == []
    _run(scenario())


def test_kill_switch_blocks_every_entry_point(monkeypatch, servers):
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "false")
    bridge = BrowserUseBridge()
    for coro in (bridge.create(), bridge.create_cloak(), bridge.open_url("any", servers.public_url("/ok")),
                 bridge.html("any"), bridge.screenshot("any")):
        with pytest.raises(RuntimeError) as err:
            _run(coro)
        assert str(err.value) == TOOLS_DISABLED
    assert servers.internal_hits == []
