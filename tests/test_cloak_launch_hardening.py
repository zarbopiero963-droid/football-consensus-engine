"""CloakBrowser hardening (#189): kill switch, pinned binary, licence handling, sandbox and uid-split
audit, one local session, fail-closed launch, environment scrubbing.

Offline tests always run. Real-binary tests need FCB_CLOAK_CHROMIUM=<Cloak binary or full Chrome>
and a NON-root uid (Chromium refuses its sandbox as root); the uid-split end-to-end test also needs
FCB_CLOAK_UID_SPLIT=1 and the lab layout of deploy/cloak-uid-split (see
docs/certification/areas/cloak_hardening.md).
"""
from __future__ import annotations

import asyncio
import importlib.machinery
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bridge import browser_use, session_leases
from bridge.browser_use import BrowserRefusal, BrowserUseBridge

MARK = "FCB-MOCK-7f3a"
SECRETS = {"DATABASE_URL": f"postgres://u:{MARK}@db/x", "MCP_OAUTH_PASSWORD": MARK,
           "APIFOOTBALL_API_KEY": MARK, "BROWSER_USE_API_KEY": MARK, "SXBET_API_KEY": MARK,
           "CLOAKBROWSER_LICENSE_KEY": MARK + "-licence"}
_REAL_WS = browser_use.container_working_set_bytes  # captured before the autouse stub
SWITCH_VALUES_OFF = [None, "", "false", "0", "no", "off", "disabled", "true;", "enabled"]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "true")
    monkeypatch.setenv("CLOAKBROWSER_ENABLED", "true")
    # Values prepare() rewrites in os.environ: set so monkeypatch restores them after each test.
    monkeypatch.setenv("CLOAKBROWSER_AUTO_UPDATE", "true")
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", "")
    for name in ("CLOAKBROWSER_LAUNCHER", "CLOAKBROWSER_RUNTIME_PROFILE", "CLOAKBROWSER_BINARY_SHA256",
                 "CLOAKBROWSER_PROXY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(browser_use, "_set_bridge_non_dumpable", lambda: None)  # own test below
    monkeypatch.setattr(browser_use, "_scrub_playwright_driver_env", lambda: None)  # own test below
    monkeypatch.setattr(browser_use, "container_working_set_bytes", lambda files=None: None)  # own tests below


def _fake_cloakbrowser(monkeypatch, tmp_path, *, version="0.5.11", cached=True, content=b"pinned"):
    """A fake `cloakbrowser` whose pinned binary is a temp file; the 146 pin is set to its hash."""
    binary = tmp_path / "chromium-146" / "chrome"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(content)
    monkeypatch.setitem(browser_use.CLOAK_PROFILES, "pinned-146", browser_use.CloakProfile(
        "pinned-146", "146.0.7680.177.5", browser_use._sha256_file(str(binary)), False))
    module = ModuleType("cloakbrowser")
    module.__spec__ = importlib.machinery.ModuleSpec("cloakbrowser", None)
    module.__version__ = version
    calls = []
    module.ensure_binary = lambda **kw: calls.append(kw) or str(binary)
    module.launch_async = AsyncMock()
    config = ModuleType("cloakbrowser.config")
    config.get_binary_path = lambda v: binary if cached else tmp_path / "missing" / "chrome"
    monkeypatch.setitem(sys.modules, "cloakbrowser", module)
    monkeypatch.setitem(sys.modules, "cloakbrowser.config", config)
    return module, binary, calls


# ---- kill switch: prepare honours BOTH flags and never raises ---------------------------------

@pytest.mark.parametrize("flag", ["BROWSER_TOOLS_ENABLED", "CLOAKBROWSER_ENABLED"])
@pytest.mark.parametrize("value", SWITCH_VALUES_OFF)
def test_prepare_does_nothing_unless_both_switches_are_explicitly_on(monkeypatch, tmp_path, flag, value):
    """RED on main: prepare_cloak_runtime ignored BROWSER_TOOLS_ENABLED and downloaded the binary."""
    module, _, calls = _fake_cloakbrowser(monkeypatch, tmp_path, cached=False)
    if value is None:
        monkeypatch.delenv(flag, raising=False)
    else:
        monkeypatch.setenv(flag, value)
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert calls == []
    assert bridge._cloak_ready is False
    assert os.environ["CLOAKBROWSER_LICENSE_KEY"] == MARK + "-licence"  # untouched when off
    assert os.environ["CLOAKBROWSER_AUTO_UPDATE"] == "true"


def test_prepare_never_raises_and_leaves_cloak_disabled_with_a_fixed_code(monkeypatch, tmp_path):
    """RED on main: an ensure_binary failure escaped prepare_cloak_runtime and so the lifespan,
    taking the whole MCP (OAuth, API-FOOTBALL, SX Bet, Browser Use) down with it."""
    module, _, _ = _fake_cloakbrowser(monkeypatch, tmp_path, cached=False)

    def boom(**kw):
        raise OSError(f"disk full at /secret/{MARK}")
    module.ensure_binary = boom
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())  # no exception
    assert bridge._cloak_ready is False
    assert bridge._cloak_failure == "CLOAK_RUNTIME_UNAVAILABLE"
    with pytest.raises(BrowserRefusal) as err:
        run(bridge.create_cloak())
    assert err.value.code == "CLOAK_RUNTIME_UNAVAILABLE"
    assert MARK not in repr(run(bridge.health()))


LIFESPAN_SCRIPT = r"""
import asyncio, json, sys, types
mod = types.ModuleType("cloakbrowser")
mod.__version__ = "0.5.11"
def boom(**kw):
    raise RuntimeError("no network FCB-MOCK-7f3a")
mod.ensure_binary = boom
cfg = types.ModuleType("cloakbrowser.config")
cfg.get_binary_path = lambda v: "/nonexistent/chrome"
sys.modules["cloakbrowser"] = mod
sys.modules["cloakbrowser.config"] = cfg
from bridge import browser_use
browser_use._set_bridge_non_dumpable = lambda: None
import bridge.server as srv
from starlette.testclient import TestClient
async def idle():
    await asyncio.Event().wait()
async def refresh():
    return {"state": "UNKNOWN", "missing_tables": None, "bridge_versions": None}
srv.bridge.reaper_loop = idle
srv.schema_readiness.refresh = refresh
with TestClient(srv.app) as c:
    r = c.get("/healthz")
    out = {"status": r.status_code, "ok": r.json()["ok"], "code": srv.bridge._cloak_failure,
           "tools": r.json()["mcp_tools_total"]}
print("RESULT_JSON=" + json.dumps(out))
"""


def test_mcp_starts_and_serves_other_components_when_cloak_cannot_prepare():
    """Real lifespan (subprocess: the MCP session manager runs once per process) with a broken Cloak
    runtime and both switches on: /healthz answers and Cloak is disabled with a fixed code. RED on
    main: the download error escaped prepare_cloak_runtime and aborted the startup."""
    import json
    env = {k: v for k, v in os.environ.items() if not k.startswith(("APIFOOTBALL_", "SXBET_"))}
    env.pop("DATABASE_URL", None)
    env.update({"PYTHONPATH": os.getcwd(), "BROWSER_TOOLS_ENABLED": "true", "CLOAKBROWSER_ENABLED": "true",
                "CLOAKBROWSER_BINARY_PATH": "", "SXBET_SIGNER_PROCESS": "1"})
    proc = subprocess.run([sys.executable, "-c", LIFESPAN_SCRIPT], env=env, capture_output=True,
                          text=True, timeout=120)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-2000:]
    out = json.loads(line[len("RESULT_JSON="):])
    assert out["status"] == 200 and out["ok"] is True and out["tools"] >= 34
    assert out["code"] == "CLOAK_RUNTIME_UNAVAILABLE"
    assert MARK not in proc.stdout + proc.stderr


# ---- pinned binary, licence, auto-update -------------------------------------------------------

def test_prepare_verifies_the_pinned_binary_and_forces_no_auto_update(monkeypatch, tmp_path):
    module, binary, calls = _fake_cloakbrowser(monkeypatch, tmp_path)
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert bridge._cloak_ready is True, bridge._cloak_failure
    assert calls == []  # cached pinned binary: no download, no network
    assert os.environ["CLOAKBROWSER_BINARY_PATH"] == str(binary.resolve())
    assert os.environ["CLOAKBROWSER_AUTO_UPDATE"] == "false"
    # Keyless profile: the licence is removed from the environment and never used.
    assert "CLOAKBROWSER_LICENSE_KEY" not in os.environ
    assert bridge._cloak_license is None
    assert "license_key" not in bridge._cloak_launch_kwargs()


def test_a_binary_that_does_not_match_its_pin_is_refused(monkeypatch, tmp_path):
    module, binary, _ = _fake_cloakbrowser(monkeypatch, tmp_path)
    binary.write_bytes(b"swapped binary")
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert bridge._cloak_ready is False and bridge._cloak_failure == "CLOAK_BINARY_UNVERIFIED"


def test_an_explicit_binary_path_is_verified_too(monkeypatch, tmp_path):
    _fake_cloakbrowser(monkeypatch, tmp_path)
    other = tmp_path / "other-chrome"
    other.write_bytes(b"not the pinned build")
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", str(other))
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert bridge._cloak_failure == "CLOAK_BINARY_UNVERIFIED"


def test_keyless_pin_is_never_downloaded_by_a_licence_only_wrapper(monkeypatch, tmp_path):
    """cloakbrowser 0.6+/0.7 download only with a licence (latest build): the 146 pin must come from
    the image or the cache, never from a silent licence claim."""
    _, _, calls = _fake_cloakbrowser(monkeypatch, tmp_path, version="0.7.2", cached=False)
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert calls == [] and bridge._cloak_failure == "CLOAK_RUNTIME_UNAVAILABLE"


@pytest.mark.parametrize("profile,sha,key,code", [
    ("nope", None, True, "CLOAK_PROFILE_INVALID"),
    ("licensed", None, True, "CLOAK_BINARY_UNPINNED"),
    ("licensed", "xyz", True, "CLOAK_BINARY_UNPINNED"),
    ("licensed", "0" * 64, False, "CLOAK_LICENSE_MISSING"),
    ("licensed", "0" * 64, True, "CLOAK_BINARY_UNVERIFIED"),
])
def test_licensed_profile_needs_an_owner_pin_and_a_key(monkeypatch, tmp_path, profile, sha, key, code):
    _fake_cloakbrowser(monkeypatch, tmp_path)
    monkeypatch.setenv("CLOAKBROWSER_RUNTIME_PROFILE", profile)
    if sha is not None:
        monkeypatch.setenv("CLOAKBROWSER_BINARY_SHA256", sha)
    if not key:
        monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY")
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert bridge._cloak_ready is False and bridge._cloak_failure == code


def test_licensed_profile_hands_the_key_to_cloakbrowser_only(monkeypatch, tmp_path):
    module, binary, calls = _fake_cloakbrowser(monkeypatch, tmp_path)
    monkeypatch.setenv("CLOAKBROWSER_RUNTIME_PROFILE", "licensed")
    monkeypatch.setenv("CLOAKBROWSER_BINARY_SHA256", browser_use._sha256_file(str(binary)))
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert bridge._cloak_ready is True, bridge._cloak_failure
    assert calls == [{"license_key": MARK + "-licence"}]
    assert "CLOAKBROWSER_LICENSE_KEY" not in os.environ  # no child inherits it
    kwargs = bridge._cloak_launch_kwargs()
    assert kwargs["license_key"] == MARK + "-licence"
    assert MARK not in repr(kwargs["env"])


# ---- launch options ----------------------------------------------------------------------------

def test_cloak_launch_kwargs_never_disable_the_sandbox_and_carry_no_secret():
    kwargs = BrowserUseBridge()._cloak_launch_kwargs()
    assert kwargs["chromium_sandbox"] is True
    assert kwargs["stealth_args"] is False  # cloakbrowser's stealth defaults carry --no-sandbox
    assert not any(a.startswith("--no-sandbox") for a in kwargs["args"])
    assert [a.split("=", 1)[0] for a in kwargs["args"]].count("--disable-features") == 1
    assert "IsolateSandboxedIframes" in next(a for a in kwargs["args"] if a.startswith("--disable-features="))
    assert "--renderer-process-limit=1" in kwargs["args"]
    assert any(re.fullmatch(r"--fingerprint=\d{5}", a) for a in kwargs["args"])
    assert set(kwargs["env"]) <= set(browser_use.CLOAK_ENV_ALLOWLIST)
    assert MARK not in repr(kwargs)


def test_cloakbrowser_library_does_not_reinject_no_sandbox_or_secrets(monkeypatch):
    """Through the REAL installed cloakbrowser.launch_async: the final Playwright launch call
    (mocked) has chromium_sandbox=True, no --no-sandbox and an environment without secrets."""
    cloak = pytest.importorskip("cloakbrowser.browser")
    monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY")  # keyless profile: prepare() removed it
    seen = {}

    class _Chromium:
        async def launch(self, **kw):
            seen.update(kw)
            return SimpleNamespace(close=None)

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            pass

    class _Starter:
        async def start(self):
            return _PW()

    import playwright.async_api as api
    monkeypatch.setattr(api, "async_playwright", lambda: _Starter())
    monkeypatch.setattr(cloak, "ensure_binary", lambda **kw: "/nonexistent/chrome")
    kw = BrowserUseBridge()._cloak_launch_kwargs()
    kw["humanize"] = False
    run(cloak.launch_async(**kw))
    assert seen["chromium_sandbox"] is True
    assert not any(a.startswith("--no-sandbox") for a in seen["args"])
    assert MARK not in repr(seen.get("env"))
    assert "IsolateSandboxedIframes" in [a for a in seen["args"] if a.startswith("--disable-features=")][-1]


def test_cloak_keeps_every_feature_playwright_itself_disables():
    """Drift guard: our combined --disable-features replaces Playwright's (last switch wins), so it
    must keep every feature the installed Playwright disables."""
    import playwright
    bundle = Path(playwright.__file__).parent / "driver/package/lib/coreBundle.js"
    if not bundle.exists():
        pytest.skip("Playwright driver bundle not found")
    text = bundle.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"disabledFeatures = \[(.*?)\]\.filter\(Boolean\)", text, re.S)
    assert block, "Playwright's disabledFeatures list moved: re-check the Cloak combined flag"
    theirs = set(re.findall(r'^\s*"([A-Za-z0-9]+)",?\s*$', block.group(1), re.M))
    assert theirs and theirs <= set(browser_use.CLOAK_DISABLED_FEATURES), theirs - set(browser_use.CLOAK_DISABLED_FEATURES)


def test_playwright_driver_env_is_scrubbed(monkeypatch):
    from playwright._impl import _transport
    monkeypatch.undo()  # the real scrubber, not the autouse stub
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(_transport, "get_driver_env", _transport.get_driver_env)  # restored after
    browser_use._scrub_playwright_driver_env()
    browser_use._scrub_playwright_driver_env()  # idempotent
    env = _transport.get_driver_env()
    assert MARK not in repr(env)
    assert env.get("PW_LANG_NAME") == "python"


def test_bridge_non_dumpable_hides_environ_from_its_own_uid():
    """In a child (the test process stays dumpable): after PR_SET_DUMPABLE=0 its /proc/<pid>/environ
    is root-owned, so a same-uid, non-root process cannot read it."""
    code = ("import os,sys;sys.path.insert(0,%r);from bridge import browser_use as b;"
            "b._set_bridge_non_dumpable();print(os.stat('/proc/self/environ').st_uid)") % os.getcwd()
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0"


# ---- sandbox / uid-split audit -----------------------------------------------------------------

def _fake_proc(monkeypatch, procs, bridge_uid=1000):
    """procs: pid -> (ppid, comm, cmdline bytes, status dict)."""
    me = os.getpid()
    table = {me: (0, b"python", b"python", {"Uid": str(bridge_uid)})}
    table.update(procs)

    def read(pid, name):
        if pid not in table:
            return None
        ppid, comm, cmdline, status = table[pid]
        if name == "stat":
            return f"{pid} (x) S {ppid} 0".encode()
        if name == "comm":
            return comm + b"\n"
        if name == "cmdline":
            return cmdline
        return "\n".join(f"{k}:\t{v}" for k, v in status.items()).encode()
    monkeypatch.setattr(browser_use, "_proc_read", read)
    monkeypatch.setattr(browser_use.os, "listdir", lambda path: [str(p) for p in table])
    monkeypatch.setattr(browser_use.os, "getuid", lambda: bridge_uid)


SANDBOXED = {"Uid": "10002\t10002\t10002\t10002", "Seccomp": "2", "NoNewPrivs": "1", "NSpid": "4721\t4\t1"}
BROWSER_ST = {"Uid": "10002\t10002\t10002\t10002", "Seccomp": "0", "NoNewPrivs": "0", "NSpid": "4700"}


def _tree(renderer_status=SANDBOXED, browser_cmd=b"/opt/cloak/chrome\0--remote-debugging-pipe\0",
          browser_status=BROWSER_ST):
    me = os.getpid()
    return {
        900: (me, b"node", b"node\0driver", {"Uid": "1000"}),
        901: (900, b"cloak-launch", b"/bin/bash\0cloak-launch\0--remote-debugging-pipe", {"Uid": "1000"}),
        902: (901, b"chrome", browser_cmd, browser_status),
        903: (902, b"chrome", b"/opt/cloak/chrome --type=zygote", SANDBOXED),
        904: (903, b"chrome", b"/opt/cloak/chrome --type=renderer --lang=en", renderer_status),
    }


def test_audit_accepts_a_sandboxed_uid_split_chromium(monkeypatch):
    _fake_proc(monkeypatch, _tree())
    audit = browser_use.cloak_process_audit()
    assert audit == {"browser_processes": 1, "renderers": 1, "chromium_processes": 3, "separate_uid": True}


@pytest.mark.parametrize("change,code", [
    ({"renderer_status": {**SANDBOXED, "Seccomp": "0"}}, "CLOAK_SANDBOX_UNAVAILABLE"),
    ({"renderer_status": {**SANDBOXED, "NoNewPrivs": "0"}}, "CLOAK_SANDBOX_UNAVAILABLE"),
    ({"renderer_status": {**SANDBOXED, "NSpid": "4721"}}, "CLOAK_SANDBOX_UNAVAILABLE"),
    ({"browser_cmd": b"/opt/cloak/chrome\0--no-sandbox\0--remote-debugging-pipe\0"}, "CLOAK_SANDBOX_UNAVAILABLE"),
    ({"browser_cmd": b"/opt/cloak/chrome --headless --no-sandbox --remote-debugging-pipe"}, "CLOAK_SANDBOX_UNAVAILABLE"),
    ({"browser_status": {**BROWSER_ST, "Uid": "1000\t1000\t1000\t1000"}}, "CLOAK_ISOLATION_UNAVAILABLE"),
    ({"browser_status": {**BROWSER_ST, "Uid": "0\t0\t0\t0"}}, "CLOAK_ISOLATION_UNAVAILABLE"),
    ({"renderer_status": {**SANDBOXED, "Uid": "1000\t1000\t1000\t1000"}}, "CLOAK_ISOLATION_UNAVAILABLE"),
])
def test_audit_refuses_an_inactive_sandbox_or_a_shared_uid(monkeypatch, change, code):
    _fake_proc(monkeypatch, _tree(**change))
    with pytest.raises(BrowserRefusal) as err:
        browser_use.cloak_process_audit()
    assert err.value.code == code


def test_audit_refuses_when_no_renderer_or_browser_is_found(monkeypatch):
    tree = _tree()
    del tree[904]
    _fake_proc(monkeypatch, tree)
    with pytest.raises(BrowserRefusal) as err:
        browser_use.cloak_process_audit()
    assert err.value.code == "CLOAK_SANDBOX_UNAVAILABLE"
    _fake_proc(monkeypatch, {})
    with pytest.raises(BrowserRefusal):
        browser_use.cloak_process_audit()


# ---- launch: fail closed, latch, one session ---------------------------------------------------

def _ready_bridge(monkeypatch, tmp_path):
    module, _, _ = _fake_cloakbrowser(monkeypatch, tmp_path)
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert bridge._cloak_ready
    return bridge, module


def _fake_browser():
    page = SimpleNamespace(goto=AsyncMock())
    probe = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    return SimpleNamespace(new_context=AsyncMock(return_value=probe), close=AsyncMock(), is_connected=lambda: True)


@pytest.mark.parametrize("failure,code", [
    (RuntimeError("BrowserType.launch: FATAL: No usable sandbox! secret-path=/x"), "CLOAK_SANDBOX_UNAVAILABLE"),
    (RuntimeError(f"random crash {MARK}"), "CLOAK_LAUNCH_FAILED"),
    (TimeoutError(), "CLOAK_LAUNCH_TIMEOUT"),
])
def test_any_launch_error_latches_cloak_off_with_a_fixed_code(monkeypatch, tmp_path, failure, code):
    bridge, module = _ready_bridge(monkeypatch, tmp_path)
    module.launch_async = AsyncMock(side_effect=failure)
    monkeypatch.setattr(session_leases, "register", lambda *a: None)
    with pytest.raises(BrowserRefusal) as err:
        run(bridge.create_cloak())
    assert err.value.code == code and err.value.__cause__ is None
    assert MARK not in str(err.value)
    # Latched: the next create does not even try to launch; health shows why.
    module.launch_async.reset_mock()
    with pytest.raises(BrowserRefusal) as again:
        run(bridge.create_cloak())
    assert again.value.code == code
    module.launch_async.assert_not_awaited()
    health = run(bridge.health())
    assert health["providers"]["cloak"]["disabled_code"] == code
    assert health["providers"]["cloak"]["ready"] is False


@pytest.mark.parametrize("code", ["CLOAK_SANDBOX_UNAVAILABLE", "CLOAK_ISOLATION_UNAVAILABLE"])
def test_a_failed_audit_closes_the_browser_and_latches(monkeypatch, tmp_path, code):
    bridge, module = _ready_bridge(monkeypatch, tmp_path)
    browser = _fake_browser()
    module.launch_async = AsyncMock(return_value=browser)

    def audit():
        raise BrowserRefusal("x", code)
    monkeypatch.setattr(browser_use, "cloak_process_audit", audit)
    with pytest.raises(BrowserRefusal) as err:
        run(bridge.create_cloak())
    assert err.value.code == code
    browser.close.assert_awaited()
    assert bridge._cloak_failure == code and bridge._sessions == {}


def test_launch_is_refused_before_prepare(monkeypatch):
    bridge = BrowserUseBridge()
    with pytest.raises(BrowserRefusal) as err:
        run(bridge._launch_cloak())
    assert err.value.code == "CLOAK_RUNTIME_NOT_READY"


def test_a_verified_launch_creates_the_session(monkeypatch, tmp_path):
    bridge, module = _ready_bridge(monkeypatch, tmp_path)
    browser = _fake_browser()
    module.launch_async = AsyncMock(return_value=browser)
    monkeypatch.setattr(browser_use, "cloak_process_audit", lambda: {"renderers": 1})
    install = AsyncMock()
    monkeypatch.setattr(bridge, "_install_guards", install)
    monkeypatch.setattr(session_leases, "register", lambda *a: None)
    result = run(bridge.create_cloak())
    assert result["provider"] == "cloak"
    kwargs = module.launch_async.await_args.kwargs
    assert kwargs["chromium_sandbox"] is True and MARK not in repr(kwargs)
    install.assert_awaited_once()
    assert bridge.cloak_audit == {"renderers": 1}


@pytest.mark.parametrize("configured", [None, "1", "4", "999", "junk"])
def test_concurrent_creates_launch_at_most_one_chromium(monkeypatch, configured):
    """RED on main (BR-048): the capacity check released the lock before the launch, so two
    concurrent creates both launched; CLOAKBROWSER_MAX_LOCAL_SESSIONS also allowed up to 4 (and a
    non-numeric value crashed)."""
    if configured is None:
        monkeypatch.delenv("CLOAKBROWSER_MAX_LOCAL_SESSIONS", raising=False)
    else:
        monkeypatch.setenv("CLOAKBROWSER_MAX_LOCAL_SESSIONS", configured)
    bridge = BrowserUseBridge()
    launches = []

    async def slow_launch():
        launches.append(1)
        await asyncio.sleep(0.2)
        return _fake_browser()
    monkeypatch.setattr(bridge, "_launch_cloak", slow_launch)
    monkeypatch.setattr(bridge, "_install_guards", AsyncMock())
    monkeypatch.setattr(session_leases, "register", lambda *a: None)

    async def scenario():
        return await asyncio.gather(*(bridge.create_cloak() for _ in range(4)), return_exceptions=True)
    results = run(scenario())
    ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, BrowserRefusal)]
    assert len(launches) == 1 and len(ok) == 1 and len(refused) == 3
    assert {r.code for r in refused} == {"CLOAK_CAPACITY_REACHED"}
    # The slot is released after a failed create too.
    assert bridge._cloak_pending is False


def test_the_slot_is_released_when_a_launch_fails(monkeypatch):
    bridge = BrowserUseBridge()
    monkeypatch.setattr(bridge, "_launch_cloak", AsyncMock(side_effect=BrowserRefusal("x", "CLOAK_LAUNCH_FAILED")))
    with pytest.raises(BrowserRefusal):
        run(bridge.create_cloak())
    assert bridge._cloak_pending is False


# ---- real binary -------------------------------------------------------------------------------
CLOAK_CHROMIUM = os.environ.get("FCB_CLOAK_CHROMIUM", "")
real_binary = pytest.mark.skipif(not CLOAK_CHROMIUM or os.geteuid() == 0,
                                 reason="needs FCB_CLOAK_CHROMIUM and a non-root uid")


def _real_ready_bridge(monkeypatch):
    monkeypatch.setitem(browser_use.CLOAK_PROFILES, "pinned-146", browser_use.CloakProfile(
        "pinned-146", "146.0.7680.177.5", browser_use._sha256_file(CLOAK_CHROMIUM), False))
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", CLOAK_CHROMIUM)
    monkeypatch.setenv("CLOAKBROWSER_HUMANIZE", "true")
    bridge = BrowserUseBridge()
    run(bridge.prepare_cloak_runtime())
    assert bridge._cloak_ready, bridge._cloak_failure
    return bridge


@real_binary
def test_real_same_uid_launch_is_sandboxed_but_refused_for_isolation(monkeypatch):
    """The production launch path on the real binary, same uid as the bridge (the native Render
    runtime): Chromium's sandbox is active (renderers pass the audit) but the shared uid is refused,
    the browser is closed and Cloak latches off. No Chromium process is left behind."""
    if os.environ.get("FCB_CLOAK_UID_SPLIT") == "1":
        pytest.skip("uid-split lab: the same-uid case does not apply")
    bridge = _real_ready_bridge(monkeypatch)
    seen = {}
    real_audit = browser_use.cloak_process_audit

    def spy():
        tree = [p for p in browser_use._descendants(os.getpid())
                if (browser_use._proc_read(p, "comm") or b"").strip() == b"chrome"]
        seen["environ"] = b"".join(browser_use._proc_read(p, "environ") or b"" for p in tree)
        seen["cmdlines"] = [browser_use._proc_read(p, "cmdline") for p in tree]
        try:
            return real_audit()
        except BrowserRefusal as exc:
            seen["code"] = exc.code
            raise
    monkeypatch.setattr(browser_use, "cloak_process_audit", spy)
    with pytest.raises(BrowserRefusal) as err:
        run(bridge.create_cloak())
    assert err.value.code == "CLOAK_ISOLATION_UNAVAILABLE"
    assert seen["code"] == "CLOAK_ISOLATION_UNAVAILABLE"  # i.e. the sandbox checks before it passed
    assert seen["cmdlines"] and not any(b"--no-sandbox" in c for c in seen["cmdlines"])
    assert MARK.encode() not in seen["environ"]
    assert bridge._cloak_failure == "CLOAK_ISOLATION_UNAVAILABLE"
    leftovers = [p for p in browser_use._descendants(os.getpid())
                 if (browser_use._proc_read(p, "comm") or b"").strip() == b"chrome"]
    assert leftovers == []


@real_binary
def test_real_control_no_sandbox_chromium_fails_the_audit():
    """RED control: the same binary started with --no-sandbox (what cloakbrowser's defaults do) is
    caught by the audit: the renderers are not under seccomp and not in their own PID namespace."""
    from playwright.async_api import async_playwright

    async def scenario():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CLOAK_CHROMIUM, args=["--no-sandbox"])
            try:
                page = await browser.new_page()
                await page.goto("about:blank")
                with pytest.raises(BrowserRefusal) as err:
                    browser_use.cloak_process_audit()
                return err.value.code
            finally:
                await browser.close()
    assert run(scenario()) == "CLOAK_SANDBOX_UNAVAILABLE"


@real_binary
def test_real_sandboxed_renderers_pass_the_sandbox_part_of_the_audit():
    """GREEN for the sandbox half: launched as production does (sandbox on), every renderer runs
    under seccomp-BPF in its own PID namespace, and chrome://sandbox agrees."""
    from playwright.async_api import async_playwright

    async def scenario():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CLOAK_CHROMIUM, chromium_sandbox=True,
                                              env=browser_use.cloak_child_env(),
                                              args=browser_use.cloak_chromium_args())
            try:
                page = await browser.new_page()
                await page.goto("chrome://sandbox")
                text = await page.inner_text("body")
                with pytest.raises(BrowserRefusal) as err:
                    browser_use.cloak_process_audit()
                return text, err.value.code
            finally:
                await browser.close()
    text, code = run(scenario())
    assert "adequately sandboxed" in text, text
    assert code == "CLOAK_ISOLATION_UNAVAILABLE"  # sandbox checks passed; only the shared uid fails


# ---- memory watchdog ---------------------------------------------------------------------------

def test_working_set_reads_v1_and_v2_and_excludes_file_cache(tmp_path):
    v2u, v2s = tmp_path / "memory.current", tmp_path / "v2.stat"
    v2u.write_text("500000000\n")
    v2s.write_text("anon 300000000\nactive_file 100000000\ninactive_file 50000000\nshmem 1\n")
    v1u, v1s = tmp_path / "usage", tmp_path / "v1.stat"
    v1u.write_text("400000000")
    v1s.write_text("cache 10\ntotal_active_file 150000000\ntotal_inactive_file 50000000\n")
    assert _REAL_WS([(str(v2u), str(v2s), "")]) == 350000000
    assert _REAL_WS([(str(tmp_path / "missing"), "", ""), (str(v1u), str(v1s), "total_")]) == 200000000
    assert _REAL_WS([(str(tmp_path / "missing"), "", "")]) is None


@pytest.mark.parametrize("value,mb", [(None, 448), ("", 448), ("300", 300), ("9999", 448), ("1", 128), ("x", 448)])
def test_the_kill_threshold_can_only_be_lowered(monkeypatch, value, mb):
    if value is None:
        monkeypatch.delenv("CLOAKBROWSER_MEMORY_KILL_MB", raising=False)
    else:
        monkeypatch.setenv("CLOAKBROWSER_MEMORY_KILL_MB", value)
    assert browser_use.cloak_memory_kill_bytes() == mb * 2**20


def test_watchdog_kills_chromium_over_the_limit_and_stops_without_sessions(monkeypatch):
    bridge = BrowserUseBridge()
    session = browser_use.Session("cloak-w", "local-cloak-owned", None, provider="cloak")
    bridge._sessions[session.id] = session
    readings = iter([100 * 2**20, 900 * 2**20, 100 * 2**20])
    kills = []
    monkeypatch.setattr(browser_use, "container_working_set_bytes", lambda files=None: next(readings, 0))
    monkeypatch.setattr(browser_use, "kill_cloak_chromium", lambda: kills.append(1) or 3)
    monkeypatch.setattr(browser_use, "CLOAK_MEMORY_POLL_SECONDS", 0.01)

    async def scenario():
        bridge._start_cloak_watchdog()
        await asyncio.sleep(0.1)
        bridge._sessions.clear()
        await asyncio.wait_for(bridge._cloak_watchdog, 1)
    run(scenario())
    assert kills == [1] and bridge.cloak_memory_kills == 1
    assert run(bridge.health())["providers"]["cloak"]["memory_kills"] == 1


def test_cloak_screenshot_over_the_pixel_budget_is_refused_not_clipped(monkeypatch):
    bridge = BrowserUseBridge()
    page = SimpleNamespace(url="http://public.example/", frames=[], evaluate=AsyncMock(return_value=30_000_000),
                           screenshot=AsyncMock(return_value=b"png"))
    session = browser_use.Session("cloak-s", "local-cloak-owned", None, provider="cloak",
                                  browser=SimpleNamespace(is_connected=lambda: True), page=page)
    session.redirect_guard_active = session.capability_guard_installed = True
    bridge._sessions[session.id] = session
    monkeypatch.setattr(session_leases, "renew", lambda *a, **k: None)
    monkeypatch.setattr(browser_use, "validate_public_http_url", lambda url: url)
    with pytest.raises(BrowserRefusal) as err:
        run(bridge.screenshot(session.id, True))
    assert err.value.code == "SCREENSHOT_TOO_LARGE"
    page.screenshot.assert_not_awaited()
    page.evaluate = AsyncMock(return_value=1920 * 947)
    assert run(bridge.screenshot(session.id, False))["bytes"] == 3



def test_a_stopping_session_with_a_live_browser_still_holds_the_slot(monkeypatch):
    bridge = BrowserUseBridge()
    stopping = browser_use.Session("cloak-old", "local-cloak-owned", None, provider="cloak",
                                   browser=SimpleNamespace(is_connected=lambda: True))
    stopping.closing = True
    bridge._sessions[stopping.id] = stopping
    launch = AsyncMock()
    monkeypatch.setattr(bridge, "_launch_cloak", launch)
    with pytest.raises(BrowserRefusal) as err:
        run(bridge.create_cloak())
    assert err.value.code == "CLOAK_CAPACITY_REACHED"
    launch.assert_not_awaited()
    stopping.browser = None  # dropped by _drop_local: the slot is free again
    monkeypatch.setattr(bridge, "_launch_cloak", AsyncMock(side_effect=BrowserRefusal("x", "CLOAK_LAUNCH_FAILED")))
    with pytest.raises(BrowserRefusal) as err:
        run(bridge.create_cloak())
    assert err.value.code == "CLOAK_LAUNCH_FAILED"
