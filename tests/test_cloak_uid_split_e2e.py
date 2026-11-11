"""#189 uid-split end to end on a REAL Cloak (or full Chrome) binary: the production create_cloak path
through deploy/cloak-uid-split/cloak-launch, then a typical session (navigate, read, click, type,
screenshot), SSRF refusals, the isolation proofs, crash recovery and cleanup.

Runs only in the lab set up by scripts/cloak_uid_split_lab.sh (FCB_CLOAK_UID_SPLIT=1): the test
process is the bridge uid holding CAP_SETUID/SETGID/KILL as ambient capabilities, like the image.
"""
from __future__ import annotations

import asyncio
import os
import pwd
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from bridge import browser_use, session_leases
from bridge.browser_use import BrowserRefusal, BrowserUseBridge
from bridge.security import UnsafeURL

pytestmark = pytest.mark.skipif(os.environ.get("FCB_CLOAK_UID_SPLIT") != "1",
                                reason="uid-split lab only (scripts/cloak_uid_split_lab.sh)")
MARK = "FCB-MOCK-UIDSPLIT-51"
PAGE = (b"<html><head><title>lab</title></head><body><h1>public</h1>"
        b"<input id=q><button id=b onclick=\"document.title='clicked:'+document.getElementById('q').value\">go"
        b"</button><a id=r href='/redirect'>r</a></body></html>")


class _Servers:
    def __init__(self):
        self.internal_hits = []
        internal = self

        class Internal(BaseHTTPRequestHandler):
            def do_GET(self):
                internal.internal_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"INTERNAL-SECRET")

            def log_message(self, *a):
                pass

        self.internal = ThreadingHTTPServer(("127.0.0.2", 0), Internal)
        target = f"http://127.0.0.2:{self.internal.server_address[1]}/meta"

        class Public(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.end_headers()
                    return
                if self.path == "/frame":
                    body = f"<iframe src='{target}'></iframe><img src='{target}/img'>".encode()
                else:
                    body = PAGE
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.public = ThreadingHTTPServer(("127.0.0.1", 0), Public)
        for srv in (self.public, self.internal):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.public.server_address[1]}"

    def close(self):
        for srv in (self.public, self.internal):
            srv.shutdown()
            srv.server_close()


@pytest.fixture()
def lab(monkeypatch):
    servers = _Servers()
    public_port = servers.public.server_address[1]

    def validate(url):  # the lab's only "public" origin is the 127.0.0.1 server
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.hostname == "127.0.0.1" and parts.port == public_port:
            return url
        raise UnsafeURL("lab: not public", "URL_NON_PUBLIC_DESTINATION")
    monkeypatch.setattr(browser_use, "validate_public_http_url", validate)
    monkeypatch.setattr(browser_use, "_verdict_cache", {})
    for name in ("register", "renew", "renew_stop", "finish"):
        monkeypatch.setattr(session_leases, name, lambda *a, **k: None)
    monkeypatch.setattr(session_leases, "claim_stop", lambda *a, **k: True)
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "true")
    monkeypatch.setenv("CLOAKBROWSER_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", f"postgres://u:{MARK}@db/x")
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", MARK)
    binary = os.environ["FCB_CLOAK_CHROMIUM"]
    monkeypatch.setitem(browser_use.CLOAK_PROFILES, "pinned-146", browser_use.CloakProfile(
        "pinned-146", "146.0.7680.177.5", browser_use._sha256_file(binary), False))
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", binary)
    monkeypatch.setenv("CLOAKBROWSER_AUTO_UPDATE", "false")
    # The lab host is not a 512 MB container: its own cgroup figure would trip the watchdog. The
    # watchdog itself is exercised against real Chromium in test_memory_watchdog_kills_real_chromium.
    monkeypatch.setattr(browser_use, "container_working_set_bytes", lambda files=None: 0)
    bridge = BrowserUseBridge()
    asyncio.run(bridge.prepare_cloak_runtime())
    assert bridge._cloak_ready, bridge._cloak_failure
    yield bridge, servers
    servers.close()


def _chromium_pids():
    return [p for p in browser_use._descendants(os.getpid())
            if (browser_use._proc_read(p, "comm") or b"").strip() == b"chrome"]


def _as_cloak(*cmd):
    return subprocess.run(["setpriv", "--reuid=cloak", "--regid=cloak", "--clear-groups", "--inh-caps=-all",
                           "--ambient-caps=-all", "--", *cmd], capture_output=True, text=True, timeout=30)


def test_uid_split_session_end_to_end(lab, tmp_path):
    bridge, servers = lab
    secret_file = tmp_path / "bridge-secret"
    secret_file.write_text(MARK)
    secret_file.chmod(0o600)

    async def scenario():
        created = await bridge.create_cloak()
        sid = created["session_id"]
        out = {"audit": bridge.cloak_audit, "pids": _chromium_pids()}
        try:
            opened = await bridge.open_url(sid, servers.base + "/")
            out["title"] = opened["title"]
            out["dom"] = (await bridge.dom_text(sid, "h1"))
            await bridge.type_text(sid, "#q", "abc")
            await bridge.click(sid, "#b")
            out["after_click"] = await bridge._sessions[sid].page.title()
            out["shot"] = len((await bridge.screenshot(sid, False))["base64"])
            for path, expected in (("/redirect", "NAVIGATION_REDIRECT_BLOCKED"),):
                try:
                    await bridge.open_url(sid, servers.base + path)
                    out[path] = "NOT REFUSED"
                except UnsafeURL as exc:
                    out[path] = exc.code
            await bridge.open_url(sid, servers.base + "/frame")
            await asyncio.sleep(1.0)
            # Isolation, seen from the Chromium uid: nothing of the bridge is reachable.
            uid = pwd.getpwnam("cloak").pw_uid
            out["pids"] = _chromium_pids()
            out["uids"] = sorted({int(f["Uid"][0]) for f in map(browser_use._status_fields, out["pids"]) if "Uid" in f})
            out["bridge_environ_as_cloak"] = _as_cloak("cat", f"/proc/{os.getpid()}/environ").returncode
            out["secret_file_as_cloak"] = _as_cloak("cat", str(secret_file)).returncode
            out["write_repo_as_cloak"] = _as_cloak("sh", "-c", f"echo x >> {browser_use.__file__}").returncode
            out["chrome_environ"] = b"".join(
                (_as_cloak("cat", f"/proc/{p}/environ").stdout or "").encode() for p in out["pids"])
            out["cloak_uid"] = uid
        finally:
            await bridge.stop(sid)
        out["left"] = _chromium_pids()
        return out

    out = asyncio.run(scenario())
    assert out["audit"]["separate_uid"] is True and out["audit"]["renderers"] >= 1
    assert out["title"] == "lab" and "public" in out["dom"]["text"]
    assert out["after_click"] == "clicked:abc"
    assert out["shot"] > 1000
    assert out["/redirect"] == "NAVIGATION_REDIRECT_BLOCKED"
    assert servers.internal_hits == []
    assert out["uids"] == [out["cloak_uid"]]
    assert out["bridge_environ_as_cloak"] != 0
    assert out["secret_file_as_cloak"] != 0
    assert out["write_repo_as_cloak"] != 0
    assert MARK.encode() not in out["chrome_environ"]
    assert b"DATABASE_URL" not in out["chrome_environ"]
    assert out["left"] == []
    # The launcher removed its per-launch profile.
    assert _as_cloak("sh", "-c", "ls -A /var/lib/cloak").stdout.strip() == ""


def test_crash_recovery_and_a_second_session(lab):
    """kill -9 of the Chromium browser process: the session reports a fixed code, stop cleans up,
    the single slot is free again and a new session works."""
    bridge, servers = lab

    async def scenario():
        sid = (await bridge.create_cloak())["session_id"]
        browser_pid = [p for p in _chromium_pids()
                       if b"--type=" not in (browser_use._proc_read(p, "cmdline") or b"")][0]
        os.kill(browser_pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while bridge._sessions[sid].browser.is_connected() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        try:
            await bridge.open_url(sid, servers.base + "/")
            crashed = "NOT REFUSED"
        except Exception as exc:
            crashed = browser_use.browser_error_code(exc)
        try:
            await bridge.create_cloak()
            capacity = "NOT REFUSED"
        except BrowserRefusal as exc:
            capacity = exc.code
        await bridge.stop(sid)
        second = (await bridge.create_cloak())["session_id"]
        title = (await bridge.open_url(second, servers.base + "/"))["title"]
        await bridge.stop(second)
        return crashed, capacity, title, _chromium_pids()

    crashed, capacity, title, left = asyncio.run(scenario())
    assert crashed in {"CLOAK_SESSION_DISCONNECTED", "BROWSER_OPERATION_FAILED"}
    assert capacity == "CLOAK_CAPACITY_REACHED"
    assert title == "lab" and left == []


def test_launcher_refuses_no_sandbox_and_unverified_binaries():
    launcher = os.environ.get("CLOAKBROWSER_LAUNCHER", "/usr/local/bin/cloak-launch")
    env = {"PATH": "/usr/bin:/bin", "FCB_CLOAK_BINARY": os.environ["FCB_CLOAK_CHROMIUM"]}
    refused = subprocess.run([launcher, "--headless", "--no-sandbox"], env=env, capture_output=True, timeout=30)
    assert refused.returncode == 66
    outside = subprocess.run([launcher, "--version"], env={**env, "FCB_CLOAK_BINARY": "/bin/true"},
                             capture_output=True, timeout=30)
    assert outside.returncode == 64
    missing = subprocess.run([launcher, "--version"], env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=30)
    assert missing.returncode == 64


def test_memory_watchdog_kills_real_chromium_and_keeps_the_bridge(lab, monkeypatch):
    bridge, servers = lab
    monkeypatch.setattr(browser_use, "CLOAK_MEMORY_POLL_SECONDS", 0.05)

    async def scenario():
        sid = (await bridge.create_cloak())["session_id"]
        await bridge.open_url(sid, servers.base + "/")
        monkeypatch.setattr(browser_use, "container_working_set_bytes", lambda files=None: 10 * 2**30)
        deadline = time.monotonic() + 10
        while _chromium_pids() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        left = _chromium_pids()
        monkeypatch.setattr(browser_use, "container_working_set_bytes", lambda files=None: 0)
        try:
            await bridge.open_url(sid, servers.base + "/")
            code = "NOT REFUSED"
        except Exception as exc:
            code = browser_use.browser_error_code(exc)
        await bridge.stop(sid)
        return left, code, bridge.cloak_memory_kills
    left, code, kills = asyncio.run(scenario())
    assert left == [] and kills >= 1
    assert code in {"CLOAK_SESSION_DISCONNECTED", "BROWSER_OPERATION_FAILED"}
