from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
import logging
import hashlib
import json
import importlib.util
import re
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from collections import deque
from functools import wraps
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass, field
from typing import Any

import httpx
from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, Route, async_playwright

from bridge.security import UnsafeURL, validate_public_http_url
from bridge import session_leases

API_ROOT = "https://api.browser-use.com/api/v4"
LOG = logging.getLogger(__name__)

# Fixed, non-sensitive messages: never echo the rejected URL or provider errors.
REDIRECT_BLOCKED = "navigation redirected to a non-public address"
CONTENT_BLOCKED = "page content is not from a public address"
DOCUMENT_REFUSED = "page document could not be served under the browser security policy"
# FCB-WP-002b: network capabilities that bypass the CDP request guard (reproduced on real
# Chromium: a WebSocket/WebSocketStream opened inside a dedicated or shared worker reached an
# internal host; WebRTC/WebTransport use UDP/QUIC outside the Fetch domain). Layer 1: the
# context init script removes them before page script runs in every realm it reaches (frames,
# popups, srcdoc/data/about:blank/blob documents). The properties read as undefined and cannot be
# redefined, so ordinary feature detection degrades gracefully. Layer 2 (DOCUMENT_CSP below)
# covers the realms layer 1 cannot reach. NOT covered: speculation-rules prefetch/prerender
# (browser-process requests), WebRTC and WebTransport from a sandboxed srcdoc frame on the remote
# path, and DNS rebinding — see
# docs/certification/FCB_PR_WORKPACKAGES.md WP-002b; the browser stays behind the kill switch.
DISABLED_CAPABILITIES = ("Worker", "SharedWorker", "RTCPeerConnection", "webkitRTCPeerConnection",
                         "RTCDataChannel", "WebTransport", "WebSocketStream")
CAPABILITY_GUARD_JS = r"""
(() => {
  for (const name of %s) {
    try {
      Object.defineProperty(globalThis, name, {
        value: undefined, configurable: false, enumerable: false, writable: false,
      });
    } catch (e) {}
  }
})();
""" % json.dumps(list(DISABLED_CAPABILITIES))
# FCB-WP-002b review P0: on the remote (CDP) path a sandboxed srcdoc iframe is committed in its
# own process BEFORE any DevTools client can attach to it (attachedToTarget arrives already
# committed, waitingForDebugger=false), so neither the init script above nor Playwright's
# WebSocket route reaches it: a WebSocket, WebSocketStream or worker opened there reached an
# internal host. A Content-Security-Policy is enforced by the browser, not by page script, and is
# inherited by every srcdoc/about:blank/blob/data document, sandboxed or not. It is appended to
# every document response at the Fetch response stage (re-served with Fetch.fulfillRequest):
#   worker-src 'none'                       no dedicated/shared/service workers in any realm;
#   connect-src http: https: data: blob:    fetch/XHR/EventSource stay (each request is still
#                                           validated by the request guard); ws:/wss: schemes
#                                           are not matched, so WebSocket and WebSocketStream
#                                           are refused everywhere.
# Behaviour change: pages can no longer open WebSockets. NOT covered in such a frame (no CSP
# directive can separate them): WebRTC ICE/TURN, and WebTransport, whose URLs are https: and so
# match connect-src like fetch does — documented residuals, they need an egress proxy.
DOCUMENT_CSP = "worker-src 'none'; connect-src http: https: data: blob:"
# A document is re-served whole to carry the CSP; larger ones fail closed instead of streaming
# unguarded. Memory held for documents in flight is bounded twice, counting the raw body AND its
# base64 copy until Fetch.fulfillRequest returns: per session (one session cannot starve the
# others) and across all sessions (many large frames cannot exhaust the bridge). A body read is
# also time-bounded, so a stalling server cannot hold its share for the session's lifetime.
# Over any bound fails closed (DOCUMENT_REFUSED).
DOCUMENT_BODY_MAX_BYTES = 4 * 1024 * 1024
DOCUMENT_SESSION_BUDGET_BYTES = 12 * 1024 * 1024
DOCUMENT_BUFFER_BUDGET_BYTES = 32 * 1024 * 1024
DOCUMENT_READ_TIMEOUT_SECONDS = 30.0
_document_buffered_bytes = 0
_FULFILL_DROPPED_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
TOOLS_DISABLED = "browser tools are disabled on this server"
CLOAK_SANDBOX_UNAVAILABLE = "CloakBrowser sandbox unavailable"
CLOAK_ISOLATION_UNAVAILABLE = "CloakBrowser process isolation unavailable"

# Schemes a frame may legitimately carry without a network fetch of its own.
_LOCAL_FRAME_PREFIXES = ("about:blank", "about:srcdoc", "data:", "blob:", "chrome-error://")


def browser_tools_enabled() -> bool:
    """Server-side kill switch, FAIL-CLOSED (audit F-03): only an explicit 1/true/yes/on
    (case-insensitive) enables the browser tools. Unset, empty, a typo or any other value keeps
    them OFF, so a lost or mistyped variable can never re-enable the browser."""
    return os.environ.get("BROWSER_TOOLS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


_LOCAL_REQUEST_SCHEMES = ("data:", "blob:", "about:", "chrome-extension:")
_VERDICT_TTL_SECONDS = 10.0
_VERDICT_CACHE_MAX = 1024
_verdict_cache: dict[tuple[str, str, int | None], tuple[bool, float]] = {}


async def request_verdict(url: str):
    """True = public http(s)/ws(s) or local-only scheme; False = refused; str = guard error.

    http(s) and ws(s) destinations are checked with validate_public_http_url (ws/wss are
    validated as http/https). data:/blob:/about:/chrome-extension: never leave the browser.
    Every other scheme (file:, ftp:, ...) is refused. Results for a scheme/host/port are
    cached for a few seconds so a page's sub-resources do not each re-resolve DNS.
    """
    try:
        lowered = url.lower()
        if lowered.startswith(_LOCAL_REQUEST_SCHEMES):
            return True
        parts = urlsplit(url)
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
        if scheme not in ("http", "https") or not parts.hostname:
            return False
        if parts.username or parts.password:
            return False  # credentials in a request URL are always refused
        key = (scheme, parts.hostname.lower(), parts.port)
        now = time.monotonic()
        cached = _verdict_cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        probe = urlunsplit((scheme, parts.netloc, parts.path or "/", "", ""))
        try:
            await asyncio.to_thread(validate_public_http_url, probe)
            ok = True
        except UnsafeURL:
            ok = False
        if len(_verdict_cache) >= _VERDICT_CACHE_MAX:
            _verdict_cache.clear()
        _verdict_cache[key] = (ok, now + _VERDICT_TTL_SECONDS)
        return ok
    except Exception as exc:  # fail closed, never let a guard error allow a request
        return type(exc).__name__


class BrowserRefusal(RuntimeError):
    """A refusal the bridge anticipated. str() keeps the legacy, non-sensitive message; `code` is the
    fixed reason an MCP client receives (#188). Never built from exception text or a URL."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


# RuntimeError messages raised before #188 that a client may meet, mapped to fixed codes.
_LEGACY_RUNTIME_CODES = {
    "BROWSER_USE_API_KEY is required": "BROWSER_USE_NOT_CONFIGURED",
    "CloakBrowser session is no longer connected": "CLOAK_SESSION_DISCONNECTED",
    "browser context unavailable": "BROWSER_CONTEXT_UNAVAILABLE",
    "Provider did not confirm the browser stopped": "BROWSER_STOP_UNCONFIRMED",
    "browser session already claimed or no longer owned": "SESSION_NOT_OWNED",
    "browser session stop lock timed out and lease is not stopping": "SESSION_STOP_TIMEOUT",
}


def browser_error_code(exc: BaseException) -> str:
    """The fixed code an MCP client receives for a failed browser tool (#188).

    Exception messages, URLs, hostnames, provider bodies and tracebacks never reach the client;
    an unexpected exception is reported as BROWSER_INTERNAL_ERROR (its type is logged server-side)."""
    if isinstance(exc, (UnsafeURL, BrowserRefusal)):
        return exc.code
    if isinstance(exc, KeyError):
        return "SESSION_STOPPING" if "stopping" in str(exc) else "SESSION_NOT_FOUND"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "BROWSER_TIMEOUT"
    if isinstance(exc, PlaywrightError):
        return "BROWSER_TIMEOUT" if type(exc).__name__ == "TimeoutError" else "BROWSER_OPERATION_FAILED"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"PROVIDER_HTTP_{exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "PROVIDER_UNAVAILABLE"
    if isinstance(exc, ValueError):
        return "INVALID_ARGUMENT"
    if isinstance(exc, RuntimeError):
        text = str(exc)
        if text.startswith("browser stop failed: "):
            return "BROWSER_STOP_FAILED"
        return _LEGACY_RUNTIME_CODES.get(text, "BROWSER_INTERNAL_ERROR")
    return "BROWSER_INTERNAL_ERROR"


def _require_enabled() -> None:
    if not browser_tools_enabled():
        raise BrowserRefusal(TOOLS_DISABLED, "BROWSER_TOOLS_DISABLED")


# ---- CloakBrowser hardening (#189) ------------------------------------------------------------
# Cloak runs Chromium on the bridge's own host, next to the bridge's secrets. It is created only
# when ALL of these hold, otherwise it fails closed with a fixed code and the rest of the MCP
# (OAuth, Browser Use, API-FOOTBALL, SX Bet) is untouched:
#   * BROWSER_TOOLS_ENABLED and CLOAKBROWSER_ENABLED are both explicitly on (fail-closed parsing);
#   * the runtime was prepared at startup: pinned binary present and its SHA-256 verified;
#   * Chromium's own sandbox is ON and ACTIVE: never --no-sandbox (cloakbrowser's stealth defaults
#     carry it, so they are replaced by the same fingerprint flags without it), Playwright is told
#     chromium_sandbox=True, and after launch every renderer is checked in /proc to run under
#     seccomp-BPF (Seccomp: 2) inside its own PID namespace (CLOAK_SANDBOX_UNAVAILABLE otherwise);
#   * Chromium runs under a DIFFERENT uid from the bridge (deploy/cloak-uid-split): with the same
#     uid a compromised browser process could read or rewrite what the bridge owns, so a same-uid
#     or root browser is refused (CLOAK_ISOLATION_UNAVAILABLE);
#   * the browser and the Playwright driver get an allowlisted environment only (never
#     DATABASE_URL, OAuth, API or SX Bet keys) and the bridge is non-dumpable;
#   * at most ONE local session, reserved atomically before the launch.
# A launch or verification failure latches Cloak off for the life of the process (health shows
# the fixed code); a restart or deploy clears it. Never --no-sandbox, never a same-uid browser.
#
# IsolateSandboxedIframes is disabled so a sandboxed srcdoc frame stays in its parent's process,
# where the init script and the WebSocket route reach it (WP-002b P1, WebRTC/WebTransport,
# reproduced on full Chrome). Chromium honours only the LAST --disable-features switch and
# Playwright already passes its own, so ours repeats Playwright's list (playwright==1.63.0) plus
# the feature; tests/test_cloak_launch_hardening.py checks the installed Playwright for drift.
# Public ws:/wss: stay refused on Cloak too (DOCUMENT_CSP): on the Cloak 146 binary Playwright's
# WebSocket route cannot run (its Runtime bindings break), so it could not tell public from
# private targets there.
CLOAK_PLAYWRIGHT_DISABLED_FEATURES = (
    "AvoidUnnecessaryBeforeUnloadCheckSync", "DestroyProfileOnBrowserClose", "DialMediaRouteProvider",
    "GlobalMediaControls", "HttpsUpgrades", "LensOverlay", "MediaRouter", "PaintHolding",
    "ThirdPartyStoragePartitioning", "BlockOriginHeaderModificationOnRedirect", "Translate",
    "AutoDeElevate", "OptimizationHints", "msForceBrowserSignIn",
    "msEdgeUpdateLaunchServicesPreferredVersion")
CLOAK_DISABLED_FEATURES = CLOAK_PLAYWRIGHT_DISABLED_FEATURES + ("IsolateSandboxedIframes",)
# Memory/process bounds for Render Free (512 MB with the bridge), measured in
# docs/certification/areas/cloak_hardening.md: one renderer process shared by every frame and page
# of the single session, and a V8 heap cap so a page that allocates without bound crashes its own
# tab (fixed error) instead of growing the container towards Render's memory limit.
CLOAK_PROCESS_ARGS = ("--renderer-process-limit=1", "--js-flags=--max-old-space-size=128")
# A full-page screenshot is rasterised whole (width x height x 4 bytes, plus copies): on a tall page
# it measured 1.8 GB. Over this area a Cloak screenshot is refused (no silent clipping).
CLOAK_SCREENSHOT_MAX_PIXELS = 16_000_000
# The ONLY environment the browser process receives. No secret of the bridge is ever listed; the
# Cloak licence key (licensed profile only) is added by cloakbrowser itself for the binary.
CLOAK_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ", "FONTCONFIG_FILE",
                       "FONTCONFIG_PATH", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR")
# The Playwright driver (node) is a separate process: the same allowlist plus Playwright's own
# PW_* and proxy settings.
_DRIVER_ENV_ALLOWLIST = CLOAK_ENV_ALLOWLIST + ("PLAYWRIGHT_BROWSERS_PATH", "HTTP_PROXY", "HTTPS_PROXY",
                                               "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                                               "NODE_EXTRA_CA_CERTS")
CLOAK_LAUNCHER_BINARY_ENV = "FCB_CLOAK_BINARY"  # verified binary handed to the uid-split launcher
_PR_SET_DUMPABLE = 4


@dataclass(frozen=True)
class CloakProfile:
    """A pinned Cloak runtime. `sha256` is the SHA-256 of the `chrome` executable itself."""
    name: str
    chromium_version: str | None
    sha256: str | None
    licensed: bool


# pinned-146 (default): the keyless binary of cloakbrowser==0.5.11, Chromium 146.0.7680.177.5
# (linux-x64). Hash taken from the binary cloakbrowser downloaded and verified against its
# Ed25519-signed SHA256SUMS (2026-11-11), identical to the cached copy used in the lab runs.
# licensed: the binary served to a CLOAKBROWSER_LICENSE_KEY (0.6+/0.7 wrappers; the free plan is
# always served the LATEST build, so its version cannot be pinned upstream). It is accepted only
# when its SHA-256 equals CLOAKBROWSER_BINARY_SHA256, set by the owner after the canary in
# docs/certification/areas/cloak_hardening.md. Unverified here: no licence key in this environment.
CLOAK_PROFILES = {
    "pinned-146": CloakProfile("pinned-146", "146.0.7680.177.5",
                               "715722e8605ae3ce81523c1218aba1ec89425786ab33ceaf99f8a6cb5e70e6e8", False),
    "licensed": CloakProfile("licensed", None, None, True),
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def cloak_profile() -> CloakProfile:
    name = os.environ.get("CLOAKBROWSER_RUNTIME_PROFILE", "pinned-146").strip().lower() or "pinned-146"
    profile = CLOAK_PROFILES.get(name)
    if profile is None:
        raise BrowserRefusal("CloakBrowser runtime profile is not valid", "CLOAK_PROFILE_INVALID")
    if profile.licensed:
        pinned = os.environ.get("CLOAKBROWSER_BINARY_SHA256", "").strip().lower()
        if not _SHA256_RE.fullmatch(pinned):
            raise BrowserRefusal("CloakBrowser binary is not pinned", "CLOAK_BINARY_UNPINNED")
        profile = CloakProfile(profile.name, None, pinned, True)
    return profile


def cloak_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """The only environment the Cloak browser process receives."""
    src = os.environ if source is None else source
    return {k: src[k] for k in CLOAK_ENV_ALLOWLIST if src.get(k)}


def cloak_chromium_args(extra_disabled_features: tuple[str, ...] = ()) -> list[str]:
    """cloakbrowser's Linux stealth flags minus --no-sandbox, the process bound, and ONE combined
    --disable-features (Playwright's list + IsolateSandboxedIframes)."""
    import secrets
    seed = 10000 + secrets.randbelow(90000)
    features = list(dict.fromkeys(CLOAK_DISABLED_FEATURES + tuple(extra_disabled_features)))
    return [f"--fingerprint={seed}", "--fingerprint-platform=windows", *CLOAK_PROCESS_ARGS,
            "--disable-features=" + ",".join(features)]


def _scrub_playwright_driver_env() -> None:
    """Start every Playwright driver with an allowlisted environment (private API: fail closed)."""
    from playwright._impl import _transport
    original = getattr(_transport, "get_driver_env", None)
    if original is None:
        raise BrowserRefusal("CloakBrowser runtime unavailable", "CLOAK_RUNTIME_UNAVAILABLE")
    if getattr(original, "_fcb_scrubbed", False):
        return

    def scrubbed() -> dict[str, str]:
        return {k: v for k, v in original().items() if k in _DRIVER_ENV_ALLOWLIST or k.startswith("PW_")}
    scrubbed._fcb_scrubbed = True  # type: ignore[attr-defined]
    _transport.get_driver_env = scrubbed


def _set_bridge_non_dumpable() -> None:
    """PR_SET_DUMPABLE=0: /proc/<bridge>/environ and /mem become root-owned and the bridge cannot
    be ptraced by its own uid. Defence in depth: the uid split is what keeps the browser out."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise BrowserRefusal("CloakBrowser runtime unavailable", "CLOAK_RUNTIME_UNAVAILABLE")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _proc_read(pid: int, name: str) -> bytes | None:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        stat = _proc_read(int(entry), "stat")
        if not stat:
            continue
        try:
            ppid = int(stat.rsplit(b")", 1)[1].split()[1])
        except (IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    found, todo = [], [root]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


_NO_SANDBOX_RE = re.compile(rb"--no-sandbox(?:[\0 ]|$)")


def _status_fields(pid: int) -> dict[str, list[str]]:
    text = (_proc_read(pid, "status") or b"").decode("ascii", "replace")
    return {line.split(":", 1)[0]: line.split(":", 1)[1].split() for line in text.splitlines() if ":" in line}


# Memory watchdog (#189). Chromium shares the container's memory with the bridge: past Render's
# limit the WHOLE service is killed and restarted (OAuth, API-FOOTBALL, SX Bet, Browser Use). While a
# Cloak session exists the container's non-reclaimable memory (cgroup usage minus file page cache:
# anonymous + shared memory + kernel) is polled
# and, above CLOAKBROWSER_MEMORY_KILL_MB (default 448 of 512), every Chromium process of the session
# is killed: the session then answers CLOAK_SESSION_DISCONNECTED and the bridge keeps running.
CLOAK_MEMORY_KILL_MB_DEFAULT = 448
CLOAK_MEMORY_POLL_SECONDS = 0.1


def _cgroup_memory_files() -> list[tuple[str, str, str]]:
    """(usage file, stat file, memory.stat key prefix) of THIS process's memory cgroup, v2 then v1;
    the mount root last (a container with its own cgroup namespace sees itself there)."""
    found = []
    try:
        with open("/proc/self/cgroup") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        _, controllers, path = (line.split(":", 2) + ["", ""])[:3]
        path = path.lstrip("/")
        if controllers == "":
            base = os.path.join("/sys/fs/cgroup", path)
            found.append((os.path.join(base, "memory.current"), os.path.join(base, "memory.stat"), ""))
        elif "memory" in controllers.split(","):
            base = os.path.join("/sys/fs/cgroup/memory", path)
            found.append((os.path.join(base, "memory.usage_in_bytes"), os.path.join(base, "memory.stat"),
                          "total_"))
    found += [("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat", ""),
              ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.stat", "total_")]
    return found


def cloak_memory_kill_bytes() -> int:
    try:
        mb = int(os.environ.get("CLOAKBROWSER_MEMORY_KILL_MB", "") or CLOAK_MEMORY_KILL_MB_DEFAULT)
    except ValueError:
        mb = CLOAK_MEMORY_KILL_MB_DEFAULT
    return max(128, min(mb, CLOAK_MEMORY_KILL_MB_DEFAULT)) * 2**20  # never above the safe default


def container_working_set_bytes(files: list[tuple[str, str, str]] | None = None) -> int | None:
    """cgroup memory usage minus file page cache (active + inactive: reclaimable), or None."""
    for usage_path, stat_path, prefix in (_cgroup_memory_files() if files is None else files):
        try:
            with open(usage_path) as fh:
                usage = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        file_cache = 0
        try:
            with open(stat_path) as fh:
                for line in fh:
                    key, _, value = line.partition(" ")
                    if key in (prefix + "active_file", prefix + "inactive_file"):
                        file_cache += int(value)
        except (OSError, ValueError):
            pass
        return max(0, usage - file_cache)
    return None


def kill_cloak_chromium(root_pid: int | None = None) -> int:
    """SIGKILL every Chromium process below the bridge (comm `chrome`). Returns how many."""
    import signal
    killed = 0
    for pid in _descendants(os.getpid() if root_pid is None else root_pid):
        if (_proc_read(pid, "comm") or b"").strip() == b"chrome":
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


def cloak_process_audit(root_pid: int | None = None) -> dict[str, Any]:
    """Prove, from /proc, that the Cloak Chromium below `root_pid` is sandboxed and uid-isolated.

    The browser process is the descendant whose comm is `chrome` without --type= (another uid's
    /proc/<pid>/exe is not readable; a launcher wrapper is not `chrome`). In its whole subtree: no
    process may carry --no-sandbox; every renderer must run under seccomp-BPF (Seccomp: 2) with
    no_new_privs inside a nested PID namespace (Chromium's layer-1 sandbox, namespace or SUID);
    every process must run as a uid that is neither root nor the bridge's own."""
    bridge_uid = os.getuid()
    root = os.getpid() if root_pid is None else root_pid
    browsers = [pid for pid in _descendants(root)
                if (_proc_read(pid, "comm") or b"").strip() == b"chrome"
                and b"--type=" not in (_proc_read(pid, "cmdline") or b"")]
    renderers, processes, uids = 0, 0, set()
    for pid in browsers + [child for browser in browsers for child in _descendants(browser)]:
        cmdline = _proc_read(pid, "cmdline")
        fields = _status_fields(pid)
        if cmdline is None or "Uid" not in fields:
            continue  # exited between the listing and the read
        processes += 1
        if _NO_SANDBOX_RE.search(cmdline):
            raise BrowserRefusal(CLOAK_SANDBOX_UNAVAILABLE, "CLOAK_SANDBOX_UNAVAILABLE")
        uids.add(int(fields["Uid"][0]))
        if b"--type=renderer" in cmdline:
            if not (fields.get("Seccomp") == ["2"] and fields.get("NoNewPrivs") == ["1"]
                    and len(fields.get("NSpid", [])) >= 2):
                raise BrowserRefusal(CLOAK_SANDBOX_UNAVAILABLE, "CLOAK_SANDBOX_UNAVAILABLE")
            renderers += 1
    if not browsers or not renderers:
        raise BrowserRefusal(CLOAK_SANDBOX_UNAVAILABLE, "CLOAK_SANDBOX_UNAVAILABLE")
    if 0 in uids or bridge_uid in uids:
        raise BrowserRefusal(CLOAK_ISOLATION_UNAVAILABLE, "CLOAK_ISOLATION_UNAVAILABLE")
    return {"browser_processes": len(browsers), "renderers": renderers, "chromium_processes": processes,
            "separate_uid": True}


def session_operation(method):
    @wraps(method)
    async def wrapped(self, session_id, *args, **kwargs):
        _require_enabled()
        # Operation duration is bounded below the durable lease. A crashed
        # worker can be reclaimed; a healthy in-flight operation stays owned.
        async with asyncio.timeout(180):
            async with self._operation(session_id):
                return await method(self, session_id, *args, **kwargs)
    return wrapped


@dataclass
class Session:
    id: str
    cdp_url: str
    live_url: str | None
    provider: str = "browser_use"
    created_monotonic: float = field(default_factory=time.monotonic)
    last_network: list[dict[str, Any]] = field(default_factory=list)
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    navigation_guard_installed: bool = False
    redirect_guard: Any = None
    redirect_guard_active: bool = False
    redirect_blocked: bool = False
    websocket_guard_installed: bool = False
    capability_guard_installed: bool = False
    blocked_requests: int = 0
    document_csp_failures: int = 0
    document_buffered_bytes: int = 0
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False


class BrowserUseBridge:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._owner_id = 'bridge-' + uuid.uuid4().hex
        self.reaper_error = None
        # CloakBrowser runtime state (#189): ready only after prepare_cloak_runtime verified it.
        self._cloak_ready = False
        self._cloak_failure: str | None = None
        self._cloak_pending = False
        self._cloak_license: str | None = None
        self._cloak_binary: str | None = None
        self._cloak_profile: CloakProfile | None = None
        self.cloak_audit: dict[str, Any] | None = None
        self._cloak_watchdog: asyncio.Future | None = None
        self.cloak_memory_kills = 0

    @staticmethod
    def _api_key() -> str:
        value = os.environ.get("BROWSER_USE_API_KEY", "").strip()
        if not value:
            raise RuntimeError("BROWSER_USE_API_KEY is required")
        return value

    @classmethod
    def _headers(cls) -> dict[str, str]:
        return {
            "X-Browser-Use-API-Key": cls._api_key(),
            "Content-Type": "application/json",
        }

    async def create(self, proxy_country_code: str | None = None) -> dict[str, Any]:
        _require_enabled()
        payload: dict[str, Any] = {}
        if proxy_country_code:
            if len(proxy_country_code) != 2 or not proxy_country_code.isalpha():
                raise ValueError("proxy_country_code must be a two-letter country code")
            payload["proxyCountryCode"] = proxy_country_code.lower()

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{API_ROOT}/browsers", headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()

        session = Session(
            id=str(data["id"]),
            cdp_url=str(data["cdpUrl"]),
            live_url=data.get("liveUrl"),
        )
        try:
            await asyncio.to_thread(session_leases.register, session.id, self._owner_id)
        except Exception:
            # An untracked browser must not be handed to the caller. Compensate
            # the allocation; report failed compensation without capabilities.
            try:
                await self._stop_remote(session.id)
            except Exception as stop_error:
                LOG.error('Untracked browser allocation cleanup failed: session=%s error=%s',
                          session.id, type(stop_error).__name__)
            raise
        async with self._lock:
            self._sessions[session.id] = session

        # cdpUrl is intentionally never returned: treat it as a bearer-like capability.
        # Browser Use's liveUrl embeds the session CDP host (`wss=...cdp...`), so it
        # is treated the same way: withheld unless the operator explicitly opts in.
        result = {
            "session_id": session.id,
            "status": data.get("status", "active"),
            "live_view_available": bool(session.live_url),
        }
        if self._expose_live_url() and session.live_url:
            result["live_url"] = session.live_url
        return result

    @staticmethod
    def _expose_live_url() -> bool:
        return os.environ.get("BROWSER_USE_EXPOSE_LIVE_URL", "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _cloak_enabled() -> bool:
        return os.environ.get("CLOAKBROWSER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _cloak_headless() -> bool:
        return os.environ.get("CLOAKBROWSER_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}

    async def prepare_cloak_runtime(self) -> None:
        """Verify Cloak's pinned Chromium before the service accepts traffic. NEVER raises: any
        failure leaves Cloak disabled with a fixed code (health) and the MCP starts normally.
        Does nothing unless BOTH kill switches are on."""
        self._cloak_ready = False
        if not (browser_tools_enabled() and self._cloak_enabled()):
            return
        try:
            async with asyncio.timeout(180):
                await asyncio.to_thread(self._prepare_cloak_sync)
        except Exception as exc:
            self._disable_cloak(exc)
            return
        self._cloak_ready = True
        LOG.info("CloakBrowser runtime prepared: profile=%s", self._cloak_profile.name)

    def _disable_cloak(self, exc: BaseException) -> str:
        code = exc.code if isinstance(exc, BrowserRefusal) else "CLOAK_RUNTIME_UNAVAILABLE"
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            code = "CLOAK_LAUNCH_TIMEOUT"
        self._cloak_ready = False
        self._cloak_failure = code
        LOG.error("CLOAK_DISABLED code=%s type=%s", code, type(exc).__name__)
        return code

    def _prepare_cloak_sync(self) -> None:
        profile = cloak_profile()
        # The licence key is moved out of the process environment: no child (driver, browser,
        # anything else) inherits it; cloakbrowser gets it explicitly, licensed profile only.
        licence = os.environ.pop("CLOAKBROWSER_LICENSE_KEY", "").strip() or None
        if profile.licensed and not licence:
            raise BrowserRefusal("CloakBrowser licence missing", "CLOAK_LICENSE_MISSING")
        self._cloak_license = licence if profile.licensed else None
        # No background binary/wrapper switch from the running service: forced, not defaulted.
        os.environ["CLOAKBROWSER_AUTO_UPDATE"] = "false"
        try:
            import cloakbrowser  # noqa: F401
            from cloakbrowser import ensure_binary
        except Exception:
            raise BrowserRefusal("CloakBrowser runtime unavailable", "CLOAK_RUNTIME_UNAVAILABLE") from None
        _scrub_playwright_driver_env()
        _set_bridge_non_dumpable()
        launcher = os.environ.get("CLOAKBROWSER_LAUNCHER", "").strip()
        binary = os.environ.get("CLOAKBROWSER_BINARY_PATH", "").strip()
        if launcher and binary and os.path.realpath(binary) == os.path.realpath(launcher):
            binary = ""  # set to the launcher by an earlier prepare in this process
        if not binary:
            binary = self._resolve_profile_binary(profile, ensure_binary)
        observed = _sha256_file(binary) if os.path.isfile(binary) else None
        if observed != profile.sha256:
            # The observed hash is public information about a public binary (never a secret): the
            # owner compares it with the canary before pinning it (cloak_hardening.md).
            LOG.error("CLOAK_BINARY_UNVERIFIED profile=%s observed_sha256=%s", profile.name, observed)
            raise BrowserRefusal("CloakBrowser binary does not match its pin", "CLOAK_BINARY_UNVERIFIED")
        self._cloak_binary = os.path.realpath(binary)
        self._cloak_profile = profile
        if launcher:
            if not (os.path.isfile(launcher) and os.access(launcher, os.X_OK)):
                raise BrowserRefusal("CloakBrowser launcher unavailable", "CLOAK_ISOLATION_UNAVAILABLE")
            os.environ["CLOAKBROWSER_BINARY_PATH"] = launcher
        else:
            os.environ["CLOAKBROWSER_BINARY_PATH"] = self._cloak_binary

    def _resolve_profile_binary(self, profile: CloakProfile, ensure_binary) -> str:
        """The profile's binary, downloaded (cloakbrowser verifies its signed SHA256SUMS) if missing."""
        if profile.licensed:
            return ensure_binary(license_key=self._cloak_license)
        from cloakbrowser.config import get_binary_path
        cached = get_binary_path(profile.chromium_version)
        if os.path.isfile(cached):
            return str(cached)
        version = getattr(importlib.import_module("cloakbrowser"), "__version__", "")
        if not str(version).startswith("0.5."):
            # 0.6+ wrappers download only with a licence key (latest build): the keyless pin can
            # only come from a prebuilt image or an existing cache.
            raise BrowserRefusal("CloakBrowser binary unavailable", "CLOAK_RUNTIME_UNAVAILABLE")
        return ensure_binary(browser_version=profile.chromium_version)

    def _cloak_launch_kwargs(self) -> dict[str, Any]:
        env = cloak_child_env()
        if os.environ.get("CLOAKBROWSER_LAUNCHER", "").strip():
            env[CLOAK_LAUNCHER_BINARY_ENV] = self._cloak_binary or ""
        kwargs: dict[str, Any] = {
            "headless": self._cloak_headless(),
            "humanize": os.environ.get("CLOAKBROWSER_HUMANIZE", "true").strip().lower() in {"1", "true", "yes", "on"},
            "stealth_args": False,  # cloakbrowser's stealth defaults carry --no-sandbox
            "args": cloak_chromium_args(),
            "chromium_sandbox": True,
            "env": env,
        }
        if self._cloak_license:
            kwargs["license_key"] = self._cloak_license  # injected by cloakbrowser for the binary only
        proxy = os.environ.get("CLOAKBROWSER_PROXY_URL", "").strip()
        if proxy:
            kwargs["proxy"] = proxy
        return kwargs

    async def _launch_cloak(self):
        """Launch the pinned Chromium and PROVE it is sandboxed and uid-isolated before use.
        Any failure closes what was started, latches Cloak off and raises a fixed code."""
        if not self._cloak_ready:
            raise BrowserRefusal("CloakBrowser runtime is not ready", self._cloak_failure or "CLOAK_RUNTIME_NOT_READY")
        browser = None
        try:
            from cloakbrowser import launch_async
            async with asyncio.timeout(45):
                browser = await launch_async(**self._cloak_launch_kwargs())
                probe = await browser.new_context()
                try:
                    page = await probe.new_page()
                    await page.goto("about:blank")
                    self.cloak_audit = await asyncio.to_thread(cloak_process_audit)
                finally:
                    await probe.close()
            return browser
        except Exception as exc:
            if browser is not None:
                try:
                    async with asyncio.timeout(10):
                        await browser.close()
                except Exception:
                    LOG.warning("Cloak cleanup after a failed launch did not complete")
            if not isinstance(exc, BrowserRefusal):
                text = str(exc)
                if "No usable sandbox" in text or "sandboxing failed" in text or "setuid sandbox" in text.lower():
                    exc = BrowserRefusal(CLOAK_SANDBOX_UNAVAILABLE, "CLOAK_SANDBOX_UNAVAILABLE")
                elif not isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    exc = BrowserRefusal("CloakBrowser launch failed", "CLOAK_LAUNCH_FAILED")
            code = self._disable_cloak(exc)
            raise BrowserRefusal("CloakBrowser is disabled after a launch failure", code) from None

    async def create_cloak(self) -> dict[str, Any]:
        """Create THE bridge-owned CloakBrowser session (at most one). No CDP or secret is returned."""
        _require_enabled()
        if not self._cloak_enabled():
            raise BrowserRefusal("CloakBrowser is not enabled", "CLOAK_DISABLED")
        if self._cloak_failure:
            raise BrowserRefusal("CloakBrowser is disabled after a launch failure", self._cloak_failure)

        # One local Chromium at most (Render Free memory). The slot is reserved under the lock
        # BEFORE the launch, so concurrent creates cannot both pass the check (BR-048).
        async with self._lock:
            # A stopping session still counts while its Chromium is alive (browser not yet dropped).
            local_cloak = [s for s in self._sessions.values()
                           if s.provider == "cloak" and (not s.closing or s.browser is not None)]
            if local_cloak or self._cloak_pending:
                raise BrowserRefusal("CloakBrowser local session capacity reached", "CLOAK_CAPACITY_REACHED")
            self._cloak_pending = True
        try:
            browser = await self._launch_cloak()
            session_id = "cloak-" + uuid.uuid4().hex
            session = Session(
                id=session_id,
                cdp_url="local-cloak-owned",
                live_url=None,
                provider="cloak",
                browser=browser,
            )
            try:
                async with asyncio.timeout(30):
                    session.context = await browser.new_context()
                    session.page = await session.context.new_page()
                await self._install_guards(session)
                await asyncio.to_thread(session_leases.register, session.id, self._owner_id)
            except Exception:
                try:
                    await browser.close()
                except Exception:
                    pass
                raise

            async with self._lock:
                self._sessions[session.id] = session
            self._start_cloak_watchdog()
        finally:
            async with self._lock:
                self._cloak_pending = False
        return {"session_id": session.id, "status": "active", "provider": "cloak"}

    def _start_cloak_watchdog(self) -> None:
        if self._cloak_watchdog is None or self._cloak_watchdog.done():
            self._cloak_watchdog = asyncio.ensure_future(self._cloak_memory_watchdog())

    async def _cloak_memory_watchdog(self) -> None:
        """Runs while a Cloak session exists; see CLOAK_MEMORY_KILL_MB_DEFAULT."""
        limit = cloak_memory_kill_bytes()
        while True:
            async with self._lock:
                active = [s for s in self._sessions.values() if s.provider == "cloak" and not s.closing]
            if not active:
                return
            used = container_working_set_bytes()
            if used is not None and used > limit:
                killed = await asyncio.to_thread(kill_cloak_chromium)
                self.cloak_memory_kills += 1
                LOG.error("CLOAK_MEMORY_LIMIT working_set_mb=%d limit_mb=%d killed=%d",
                          used // 2**20, limit // 2**20, killed)
            await asyncio.sleep(CLOAK_MEMORY_POLL_SECONDS)

    async def _session(self, session_id: str) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise KeyError("unknown or stopped browser session")
        return session

    @asynccontextmanager
    async def _operation(self, session_id):
        session = await self._session(session_id)
        async with session.operation_lock:
            if session.closing:
                raise KeyError('browser session is stopping')
            await asyncio.to_thread(session_leases.renew, session.id, self._owner_id)
            try:
                yield session
            finally:
                await asyncio.to_thread(session_leases.renew, session.id, self._owner_id)

    async def _navigation_guard(self, route: Route, session: Session | None = None) -> None:
        try:
            request = route.request
            if request.is_navigation_request():
                try:
                    await asyncio.to_thread(validate_public_http_url, request.url)
                except UnsafeURL:
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()
        except PlaywrightError as exc:
            # Provider stop can close a target while an existing route callback
            # is pending. Only this expected shutdown cancellation is terminal.
            if session is None or not session.closing or type(exc).__name__ != 'TargetClosedError':
                raise
            LOG.info('Navigation route cancelled during session shutdown: session=%s', session.id)

    async def _page(self, session: Session) -> Page:
        if session.browser is not None and session.browser.is_connected() and session.page is not None:
            return session.page

        if session.provider == "cloak":
            raise RuntimeError("CloakBrowser session is no longer connected")

        await self._drop_local(session)

        session.playwright = await async_playwright().start()
        session.browser = await session.playwright.chromium.connect_over_cdp(session.cdp_url)
        contexts = session.browser.contexts
        session.context = contexts[0] if contexts else await session.browser.new_context()
        pages = session.context.pages
        session.page = pages[0] if pages else await session.context.new_page()

        await self._install_guards(session)
        return session.page

    async def _install_guards(self, session: Session) -> None:
        """Context navigation guard + browser-level request guard + WebSocket guard.

        The context route is a first defence on the initial navigation request. The
        browser-level CDP Fetch guard closes SSRF for EVERY request the browser makes
        (documents, redirects, fetch/XHR, script, CSS, images, fonts, beacons, EventSource,
        prefetch, workers, service workers, form posts): each request is paused at the
        REQUEST stage, so the URL validated is the one Chromium itself resolved and will
        connect to, across popups and out-of-process iframes via flat auto-attach. WebSocket
        handshakes are not Fetch requests, so a context WebSocket route validates them. All
        guards are armed before any navigation; if the request guard cannot be armed the
        session stays guard-inactive and every navigation and read fails closed.
        """
        if not session.navigation_guard_installed:
            async def guard(route):
                await self._navigation_guard(route, session)
            await session.context.route("**/*", guard)
            session.navigation_guard_installed = True
        await self._arm_redirect_guard(session)
        await self._arm_websocket_guard(session)
        await self._arm_capability_guard(session)
        await self._quarantine_preexisting(session)

    async def _quarantine_preexisting(self, session: Session) -> None:
        """A document loaded before the guard was armed (e.g. a page that already existed
        when the bridge connected over CDP, or one that kept running while the connection
        was down) is not covered by the request guard. Navigate every such page to
        about:blank so nothing loaded pre-guard stays active or readable; the guard then
        covers all subsequent requests. Best-effort and bounded; a page that will not
        settle is closed."""
        if not session.redirect_guard_active or session.context is None:
            return
        for page in list(session.context.pages):
            # about:blank included (review P3): a pre-existing blank document carries neither the
            # init script nor the document CSP; re-navigating creates one that does.
            try:
                async with asyncio.timeout(6):
                    await page.goto("about:blank")
                    await page.wait_for_load_state("load")
            except Exception:
                try:
                    await page.close()
                except Exception:
                    pass
                if session.page is page:
                    session.page = None
        if session.page is None or session.page not in list(session.context.pages):
            pages = list(session.context.pages)
            session.page = pages[0] if pages else await session.context.new_page()

    async def _arm_redirect_guard(self, session: Session) -> None:
        if session.redirect_guard_active or session.browser is None:
            return
        cdp = await session.browser.new_browser_cdp_session()

        async def on_paused(event: dict[str, Any]) -> None:
            request_id = event.get("requestId")
            if "responseStatusCode" in event or "responseErrorReason" in event:
                await self._document_response(cdp, session, event)
                return
            # Validate the URL Chromium itself resolved (request stage), not a header we
            # re-parse: this is what the connection will actually go to.
            url = (event.get("request") or {}).get("url", "")
            document = event.get("resourceType") == "Document"
            verdict = await request_verdict(url)
            if verdict is not True:
                if verdict is not False:
                    LOG.warning("Request guard failed closed: session=%s error=%s", session.id, verdict)
                session.blocked_requests += 1
                if document:
                    # A blocked document (main frame, iframe, popup, redirect hop) fails the
                    # navigation closed; a blocked sub-resource is dropped silently.
                    session.redirect_blocked = True
                await self._fetch_fail(cdp, request_id)
                return
            try:
                await cdp.send("Fetch.continueRequest", {"requestId": request_id})
            except Exception:
                pass

        def dispatch(event):
            task = asyncio.ensure_future(on_paused(event))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        cdp.on("Fetch.requestPaused", dispatch)
        await cdp.send("Fetch.enable", {"patterns": [
            {"urlPattern": "*", "requestStage": "Request"},
            # Document responses only: append DOCUMENT_CSP (WP-002b). The URL was already
            # validated at the request stage of the same request.
            {"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"},
        ]})
        # Flat auto-attach so popups, OOPIFs, workers and service workers inherit the guard.
        await cdp.send("Target.setAutoAttach",
                       {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        session.redirect_guard = cdp
        session.redirect_guard_active = True

    async def _arm_capability_guard(self, session: Session) -> None:
        if session.capability_guard_installed or session.context is None:
            return
        add_init = getattr(session.context, "add_init_script", None)
        if add_init is None:
            session.redirect_guard_active = False  # fail closed: cannot disable capabilities
            return
        try:
            await add_init(CAPABILITY_GUARD_JS)
        except Exception:
            session.redirect_guard_active = False
            return
        session.capability_guard_installed = True

    async def _arm_websocket_guard(self, session: Session) -> None:
        if session.websocket_guard_installed or session.context is None:
            return
        route_ws = getattr(session.context, "route_web_socket", None)
        if route_ws is None:
            # Without a WebSocket guard the session cannot be used safely.
            session.redirect_guard_active = False
            return

        async def ws_guard(ws) -> None:
            if await request_verdict(ws.url) is True:
                ws.connect_to_server()
                return
            session.blocked_requests += 1
            try:
                await ws.close(code=1008, reason="blocked")
            except Exception:
                pass

        await route_ws("**/*", ws_guard)
        session.websocket_guard_installed = True

    @staticmethod
    def _document_csp_headers(headers: list[dict[str, str]] | None) -> list[dict[str, str]]:
        """The response headers plus DOCUMENT_CSP. The site's own policies are kept: every
        Content-Security-Policy header is enforced, so ours can only restrict further. The body
        is handed back already decoded, so encoding/length framing headers are dropped."""
        out = [{"name": h.get("name", ""), "value": h.get("value", "")} for h in (headers or [])
               if h.get("name", "").lower() not in _FULFILL_DROPPED_HEADERS]
        out.append({"name": "Content-Security-Policy", "value": DOCUMENT_CSP})
        return out

    @staticmethod
    def _reserve(session: Session, amount: int) -> bool:
        """Account `amount` bytes against the session and global budgets; False if over either."""
        global _document_buffered_bytes
        if (session.document_buffered_bytes + amount > DOCUMENT_SESSION_BUDGET_BYTES
                or _document_buffered_bytes + amount > DOCUMENT_BUFFER_BUDGET_BYTES):
            return False
        session.document_buffered_bytes += amount
        _document_buffered_bytes += amount
        return True

    @staticmethod
    def _release(session: Session, amount: int) -> None:
        global _document_buffered_bytes
        session.document_buffered_bytes -= amount
        _document_buffered_bytes -= amount

    async def _read_document_body(self, cdp, session: Session, request_id, held: list[int]) -> str | None:
        """Base64 body of a paused document response, or None when over a cap or budget. Every
        byte accounted is added to held[0]; the CALLER releases it after fulfillRequest returns."""
        stream = (await cdp.send("Fetch.takeResponseBodyAsStream", {"requestId": request_id}))["stream"]
        body = bytearray()
        try:
            while True:
                part = await cdp.send("IO.read", {"handle": stream, "size": 1 << 16})
                data = part.get("data", "")
                raw = base64.b64decode(data) if part.get("base64Encoded") else data.encode("utf-8")
                if len(body) + len(raw) > DOCUMENT_BODY_MAX_BYTES or not self._reserve(session, len(raw)):
                    return None
                held[0] += len(raw)
                body += raw
                if part.get("eof"):
                    break
        finally:
            try:
                await cdp.send("IO.close", {"handle": stream})
            except Exception:
                pass
        encoded_size = 4 * ((len(body) + 2) // 3)
        if not self._reserve(session, encoded_size):
            return None
        held[0] += encoded_size
        return base64.b64encode(body).decode("ascii")

    async def _document_response(self, cdp, session: Session, event: dict[str, Any]) -> None:
        """Response stage of a document: re-serve it with DOCUMENT_CSP, or fail it closed.

        Fetch.continueResponse is NOT used: Chromium replaces the headers but keeps the policy it
        parsed from the original response, so an appended CSP is silently not enforced (verified
        on real Chromium). Fetch.fulfillRequest makes Chromium parse the headers it is given."""
        request_id = event.get("requestId")
        status = event.get("responseStatusCode")
        headers = event.get("responseHeaders") or []
        redirect = status in _REDIRECT_STATUSES and any(
            h.get("name", "").lower() == "location" and h.get("value") for h in headers)
        if "responseErrorReason" in event or redirect:
            # No document is committed from these (network error page; a redirect hop whose
            # target is paused again). A 3xx without Location renders its body: it is re-served.
            try:
                await cdp.send("Fetch.continueRequest", {"requestId": request_id})
            except Exception:
                pass
            return
        held = [0]
        try:
            async with asyncio.timeout(DOCUMENT_READ_TIMEOUT_SECONDS):
                body = await self._read_document_body(cdp, session, request_id, held)
            if body is None:
                raise ValueError("document body over cap")
            params = {"requestId": request_id, "responseCode": status, "body": body,
                      "responseHeaders": self._document_csp_headers(headers)}
            if event.get("responseStatusText"):
                params["responsePhrase"] = event["responseStatusText"]
            await cdp.send("Fetch.fulfillRequest", params)
        except Exception:
            # Without the CSP the document could open unguarded sockets: never render it.
            # Fetch.failRequest alone fails it closed (a main-frame navigation then errors with
            # net::ERR_BLOCKED_BY_CLIENT). This is NOT an SSRF verdict: it does not touch
            # redirect_blocked or blocked_requests (review P2: a cancelled iframe or an oversized
            # page must not be reported as a non-public redirect, nor spill into the next
            # navigation).
            LOG.warning("Document CSP could not be applied: session=%s", session.id)
            session.document_csp_failures += 1
            await self._fetch_fail(cdp, request_id)
        finally:
            self._release(session, held[0])

    @staticmethod
    async def _fetch_fail(cdp, request_id) -> None:
        try:
            await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
        except Exception:
            pass

    def _require_guard(self, session: Session) -> None:
        """Fail closed: never navigate or read on a session without an active redirect guard
        and the capability guard (WP-002b)."""
        if not (session.redirect_guard_active and session.capability_guard_installed):
            raise UnsafeURL(CONTENT_BLOCKED, "BROWSER_GUARD_INACTIVE")

    async def _quarantine(self, session: Session, page: Page) -> None:
        """Discard a page that reached (or tried to reach) a non-public address.

        The page is closed and, if it was current, replaced by a fresh blank page; the
        browser-level redirect guard already covers that new page, so nothing of the
        rejected navigation stays readable and no later read can race Chromium committing
        its own error page.
        """
        replacement = None
        if session.page is page and session.context is not None:
            try:
                replacement = await session.context.new_page()
            except Exception as exc:
                LOG.warning("Quarantine replacement page failed: session=%s error=%s", session.id, type(exc).__name__)
        try:
            await page.close()
        except Exception:
            pass
        if session.page is page:
            session.page = replacement

    async def _assert_public_page(self, session: Session, page: Page) -> None:
        """Fail closed before any read: the page and every frame must be public or local-only."""
        self._require_guard(session)
        urls = [page.url] + [frame.url for frame in getattr(page, "frames", [])]
        for value in urls:
            if not value or value.startswith(_LOCAL_FRAME_PREFIXES):
                continue
            try:
                await asyncio.to_thread(validate_public_http_url, value)
            except UnsafeURL:
                await self._quarantine(session, page)
                raise UnsafeURL(CONTENT_BLOCKED, "PAGE_CONTENT_NOT_PUBLIC") from None

    async def _after_navigation(self, session: Session, page: Page) -> None:
        """Post-navigation check shared by every navigating tool; quarantines on failure."""
        blocked = session.redirect_blocked
        session.redirect_blocked = False
        if blocked:
            await self._quarantine(session, page)
            raise UnsafeURL(REDIRECT_BLOCKED, "NAVIGATION_REDIRECT_BLOCKED")
        await self._assert_public_page(session, page)

    async def _navigate(self, session: Session, page: Page, action):
        """Run one navigation with the redirect guard armed, then fail closed if needed."""
        self._require_guard(session)
        session.redirect_blocked = False
        csp_failures_before = session.document_csp_failures
        try:
            response = await action()
        except PlaywrightError:
            if session.redirect_blocked:
                session.redirect_blocked = False
                await self._quarantine(session, page)
                raise UnsafeURL(REDIRECT_BLOCKED, "NAVIGATION_REDIRECT_BLOCKED") from None
            if session.document_csp_failures != csp_failures_before:
                # A document of this navigation was refused because the WP-002b CSP could not be
                # applied (oversized, unreadable, buffer budget): fixed code, nothing rendered.
                await self._quarantine(session, page)
                raise UnsafeURL(DOCUMENT_REFUSED, "DOCUMENT_REFUSED") from None
            raise
        await self._after_navigation(session, page)
        return response

    @session_operation
    async def open_url(
        self,
        session_id: str,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 45000,
        capture_network: bool = True,
    ) -> dict[str, Any]:
        await asyncio.to_thread(validate_public_http_url, url)
        if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError("invalid wait_until")
        timeout_ms = max(1000, min(int(timeout_ms), 120000))
        session = await self._session(session_id)
        page = await self._page(session)
        network = deque(maxlen=500)
        session.last_network = []
        on_response = None

        if capture_network:
            def on_response(resp):
                req = resp.request
                if req.resource_type in {"xhr", "fetch", "document"}:
                    network.append({
                        "url": resp.url,
                        "status": resp.status,
                        "resource_type": req.resource_type,
                        "method": req.method,
                        "content_type": resp.headers.get("content-type", ""),
                    })

            page.on("response", on_response)

        blocked_before = session.blocked_requests
        csp_failures_before = session.document_csp_failures
        try:
            response = await self._navigate(
                session, page, lambda: page.goto(url, wait_until=wait_until, timeout=timeout_ms))
            final_url = page.url
            return {
                "blocked_requests": session.blocked_requests - blocked_before,
                # Documents (frames) not rendered because the WP-002b CSP could not be applied.
                "documents_refused": session.document_csp_failures - csp_failures_before,
                "session_id": session_id,
                "requested_url": url,
                "final_url": final_url,
                "title": await page.title(),
                "http_status": response.status if response else None,
                "network_events": len(network),
            }
        finally:
            # Includes timeout, cancellation and rejected redirects. Remove
            # only this navigation's handler, preserving other consumers.
            if on_response is not None:
                page.remove_listener("response", on_response)
            session.last_network = list(network)

    @session_operation
    async def html(self, session_id: str, selector: str | None = None) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
        await self._assert_public_page(session, page)
        if selector:
            locator = page.locator(selector).first
            content = await locator.evaluate("(el) => el.outerHTML")
        else:
            content = await page.content()
        return {
            "session_id": session_id,
            "url": page.url,
            "selector": selector,
            "html": content,
            "bytes_utf8": len(content.encode("utf-8")),
        }

    @session_operation
    async def dom_text(self, session_id: str, selector: str = "body", max_chars: int = 100000) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
        await self._assert_public_page(session, page)
        max_chars = max(1000, min(int(max_chars), 500000))
        if session.provider == "cloak":
            # Cloak's Locator.inner_text path can wedge on reactive pages.
            # Use a direct DOM query for CSS selectors instead; this bypasses
            # actionability/layout waiting and is strictly bounded.
            async with asyncio.timeout(6):
                text = await page.evaluate(
                    """selector => {
                        const el = document.querySelector(selector);
                        if (!el) throw new Error('selector not found');
                        return el.innerText || el.textContent || '';
                    }""",
                    selector,
                )
        else:
            locator = page.locator(selector).first
            try:
                async with asyncio.timeout(12):
                    text = await locator.inner_text(timeout=10000)
            except (TimeoutError, PlaywrightError):
                async with asyncio.timeout(5):
                    text = await locator.evaluate(
                        "(el) => el.innerText || el.textContent || ''"
                    )
        return {
            "session_id": session_id,
            "url": page.url,
            "selector": selector,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    @session_operation
    async def screenshot(self, session_id: str, full_page: bool = True) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
        await self._assert_public_page(session, page)
        if session.provider == "cloak":
            # Local Chromium shares the bridge's memory (#189): refuse, never clip, an oversized one.
            area = await page.evaluate(
                "(full) => (devicePixelRatio || 1) ** 2 * (full ? Math.max(document.documentElement.scrollWidth,"
                " innerWidth) * Math.max(document.documentElement.scrollHeight, innerHeight)"
                " : innerWidth * innerHeight)", bool(full_page))
            if not isinstance(area, (int, float)) or area > CLOAK_SCREENSHOT_MAX_PIXELS:
                raise BrowserRefusal("screenshot too large for the local browser", "SCREENSHOT_TOO_LARGE")
        body = await page.screenshot(full_page=bool(full_page), type="png")
        return {
            "session_id": session_id,
            "url": page.url,
            "mime_type": "image/png",
            "base64": base64.b64encode(body).decode("ascii"),
            "bytes": len(body),
        }

    @staticmethod
    def _bounded_timeout(timeout_ms: int) -> int:
        return max(250, min(int(timeout_ms), 120000))

    @staticmethod
    def _stop_lock_timeout_seconds() -> float:
        try:
            value = float(os.environ.get("BROWSER_STOP_LOCK_TIMEOUT_SECONDS", "8"))
        except ValueError:
            value = 8.0
        return max(0.05, min(value, 30.0))

    @session_operation
    async def click(
        self,
        session_id: str,
        selector: str,
        button: str = "left",
        click_count: int = 1,
        timeout_ms: int = 30000,
        wait_after_ms: int = 0,
    ) -> dict[str, Any]:
        if button not in {"left", "right", "middle"}:
            raise ValueError("invalid mouse button")
        if type(click_count) is not int or not 1 <= click_count <= 3:
            raise ValueError("click_count must be 1..3")
        wait_after_ms = max(0, min(int(wait_after_ms), 10000))
        session = await self._session(session_id)
        page = await self._page(session)
        if session.provider == "cloak":
            # CloakBrowser's humanization layer can time out on valid elements.
            # Prefer its page-level action, then fall back to a DOM click for
            # ordinary left-clicks so the bridge remains usable on reactive UIs.
            try:
                await page.click(
                    selector,
                    button=button,
                    click_count=click_count,
                    timeout=self._bounded_timeout(timeout_ms),
                )
            except Exception:
                if button != "left":
                    raise
                await page.locator(selector).first.evaluate(
                    "(el, count) => { for (let i = 0; i < count; i++) el.click(); }",
                    click_count,
                )
        else:
            await page.locator(selector).first.click(
                button=button,
                click_count=click_count,
                timeout=self._bounded_timeout(timeout_ms),
            )
        if wait_after_ms:
            await page.wait_for_timeout(wait_after_ms)
        return {"session_id": session_id, "url": page.url, "selector": selector, "status": "clicked"}

    @session_operation
    async def fill(
        self,
        session_id: str,
        selector: str,
        value: str,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
        if session.provider == "cloak":
            # Prefer Cloak's humanized Page.fill, but some reactive inputs time
            # out despite being present in the DOM. Fall back to the native
            # value setter and dispatch the same input/change events a form
            # framework expects. The supplied value is never returned.
            try:
                await page.fill(selector, value, timeout=self._bounded_timeout(timeout_ms))
            except Exception:
                await page.locator(selector).first.evaluate(
                    """(el, value) => {
                        const proto = Object.getPrototypeOf(el);
                        const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (descriptor && descriptor.set) descriptor.set.call(el, value);
                        else el.value = value;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    value,
                )
        else:
            await page.locator(selector).first.fill(value, timeout=self._bounded_timeout(timeout_ms))
        # Never echo filled text: this tool is intentionally safe for credentials.
        return {"session_id": session_id, "url": page.url, "selector": selector, "status": "filled"}

    @session_operation
    async def type_text(
        self,
        session_id: str,
        selector: str,
        text: str,
        delay_ms: int = 0,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        delay_ms = max(0, min(int(delay_ms), 1000))
        session = await self._session(session_id)
        page = await self._page(session)
        if session.provider == "cloak":
            # Keep Cloak actions on the page-level humanized API for the same
            # compatibility reason as click/fill.
            await page.click(selector, timeout=self._bounded_timeout(timeout_ms))
            await page.locator(selector).first.press_sequentially(
                text, delay=delay_ms, timeout=self._bounded_timeout(timeout_ms)
            )
        else:
            locator = page.locator(selector).first
            await locator.click(timeout=self._bounded_timeout(timeout_ms))
            await locator.press_sequentially(text, delay=delay_ms, timeout=self._bounded_timeout(timeout_ms))
        # Never echo typed text.
        return {"session_id": session_id, "url": page.url, "selector": selector, "status": "typed"}

    @session_operation
    async def press(
        self,
        session_id: str,
        selector: str,
        key: str,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
        await page.locator(selector).first.press(key, timeout=self._bounded_timeout(timeout_ms))
        return {"session_id": session_id, "url": page.url, "selector": selector, "key": key, "status": "pressed"}

    @session_operation
    async def select_option(
        self,
        session_id: str,
        selector: str,
        values: list[str],
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        if not values or len(values) > 50:
            raise ValueError("values must contain 1..50 options")
        session = await self._session(session_id)
        page = await self._page(session)
        selected = await page.locator(selector).first.select_option(
            value=values,
            timeout=self._bounded_timeout(timeout_ms),
        )
        return {"session_id": session_id, "url": page.url, "selector": selector, "selected": selected}

    @session_operation
    async def set_checked(
        self,
        session_id: str,
        selector: str,
        checked: bool = True,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
        await page.locator(selector).first.set_checked(bool(checked), timeout=self._bounded_timeout(timeout_ms))
        return {"session_id": session_id, "url": page.url, "selector": selector, "checked": bool(checked)}

    @session_operation
    async def hover(self, session_id: str, selector: str, timeout_ms: int = 30000) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
        await page.locator(selector).first.hover(timeout=self._bounded_timeout(timeout_ms))
        return {"session_id": session_id, "url": page.url, "selector": selector, "status": "hovered"}

    @session_operation
    async def focus(self, session_id: str, selector: str, timeout_ms: int = 30000) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
        locator = page.locator(selector).first
        await locator.wait_for(state="attached", timeout=self._bounded_timeout(timeout_ms))
        await locator.focus()
        return {"session_id": session_id, "url": page.url, "selector": selector, "status": "focused"}

    @session_operation
    async def scroll(self, session_id: str, delta_x: int = 0, delta_y: int = 700) -> dict[str, Any]:
        delta_x = max(-100000, min(int(delta_x), 100000))
        delta_y = max(-100000, min(int(delta_y), 100000))
        session = await self._session(session_id)
        page = await self._page(session)
        await page.mouse.wheel(delta_x, delta_y)
        return {"session_id": session_id, "url": page.url, "delta_x": delta_x, "delta_y": delta_y}

    @session_operation
    async def wait_for_selector(
        self,
        session_id: str,
        selector: str,
        state: str = "visible",
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        if state not in {"attached", "detached", "visible", "hidden"}:
            raise ValueError("invalid selector state")
        session = await self._session(session_id)
        page = await self._page(session)
        await page.locator(selector).first.wait_for(state=state, timeout=self._bounded_timeout(timeout_ms))
        return {"session_id": session_id, "url": page.url, "selector": selector, "state": state}

    @session_operation
    async def wait(self, session_id: str, milliseconds: int = 1000) -> dict[str, Any]:
        if type(milliseconds) is not int or not 0 <= milliseconds <= 30000:
            raise ValueError("milliseconds must be 0..30000")
        session = await self._session(session_id)
        page = await self._page(session)
        await page.wait_for_timeout(milliseconds)
        return {"session_id": session_id, "url": page.url, "waited_ms": milliseconds}

    @session_operation
    async def back(self, session_id: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> dict[str, Any]:
        if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError("invalid wait_until")
        session = await self._session(session_id)
        page = await self._page(session)
        response = await self._navigate(
            session, page, lambda: page.go_back(wait_until=wait_until, timeout=self._bounded_timeout(timeout_ms)))
        return {"session_id": session_id, "url": page.url, "title": await page.title(),
                "http_status": response.status if response else None}

    @session_operation
    async def forward(self, session_id: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> dict[str, Any]:
        if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError("invalid wait_until")
        session = await self._session(session_id)
        page = await self._page(session)
        response = await self._navigate(
            session, page, lambda: page.go_forward(wait_until=wait_until, timeout=self._bounded_timeout(timeout_ms)))
        return {"session_id": session_id, "url": page.url, "title": await page.title(),
                "http_status": response.status if response else None}

    @session_operation
    async def reload(self, session_id: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> dict[str, Any]:
        if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError("invalid wait_until")
        session = await self._session(session_id)
        page = await self._page(session)
        response = await self._navigate(
            session, page, lambda: page.reload(wait_until=wait_until, timeout=self._bounded_timeout(timeout_ms)))
        return {"session_id": session_id, "url": page.url, "title": await page.title(),
                "http_status": response.status if response else None}

    @session_operation
    async def elements(self, session_id: str, selector: str = "input,button,a,select,textarea", limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        session = await self._session(session_id)
        page = await self._page(session)
        await self._assert_public_page(session, page)
        locators = page.locator(selector)
        count = await locators.count()
        items = []
        for index in range(min(count, limit)):
            item = locators.nth(index)
            try:
                data = await item.evaluate("""el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type'),
                    name: el.getAttribute('name'),
                    id: el.id || null,
                    placeholder: el.getAttribute('placeholder'),
                    aria_label: el.getAttribute('aria-label'),
                    role: el.getAttribute('role'),
                    href: el.getAttribute('href'),
                    autocomplete: el.getAttribute('autocomplete'),
                    disabled: !!el.disabled,
                    checked: typeof el.checked === 'boolean' ? el.checked : null,
                    text: (el.innerText || el.textContent || '').trim().slice(0, 500)
                })""")
                data["index"] = index
                items.append(data)
            except PlaywrightError:
                continue
        return {"session_id": session_id, "url": page.url, "selector": selector,
                "items": items, "returned": len(items), "matched": count, "truncated": count > limit}

    @session_operation
    async def list_pages(self, session_id: str) -> dict[str, Any]:
        session = await self._session(session_id)
        await self._page(session)
        pages = list(session.context.pages if session.context else [])
        items = []
        for index, candidate in enumerate(pages):
            try:
                await self._assert_public_page(session, candidate)
            except UnsafeURL:
                items.append({"index": index, "url": "about:blank", "title": "", "blocked": True,
                              "current": candidate is session.page})
                continue
            items.append({"index": index, "url": candidate.url, "title": await candidate.title(),
                          "current": candidate is session.page})
        return {"session_id": session_id, "pages": items, "count": len(items)}

    @session_operation
    async def new_page(self, session_id: str, url: str | None = None) -> dict[str, Any]:
        session = await self._session(session_id)
        await self._page(session)
        if session.context is None:
            raise RuntimeError("browser context unavailable")
        if url:
            await asyncio.to_thread(validate_public_http_url, url)
        page = await session.context.new_page()
        session.page = page
        if url:
            await self._navigate(session, page,
                                 lambda: page.goto(url, wait_until="domcontentloaded", timeout=45000))
        return {"session_id": session_id, "url": page.url, "title": await page.title(),
                "page_index": list(session.context.pages).index(page)}

    @session_operation
    async def switch_page(self, session_id: str, page_index: int) -> dict[str, Any]:
        session = await self._session(session_id)
        await self._page(session)
        pages = list(session.context.pages if session.context else [])
        if type(page_index) is not int or page_index < 0 or page_index >= len(pages):
            raise ValueError("invalid page_index")
        await self._assert_public_page(session, pages[page_index])
        session.page = pages[page_index]
        return {"session_id": session_id, "url": session.page.url, "title": await session.page.title(),
                "page_index": page_index}

    @session_operation
    async def close_page(self, session_id: str, page_index: int) -> dict[str, Any]:
        session = await self._session(session_id)
        await self._page(session)
        pages = list(session.context.pages if session.context else [])
        if type(page_index) is not int or page_index < 0 or page_index >= len(pages):
            raise ValueError("invalid page_index")
        if len(pages) <= 1:
            raise ValueError("cannot close the last page")
        target = pages[page_index]
        was_current = target is session.page
        await target.close()
        remaining = list(session.context.pages if session.context else [])
        if was_current:
            session.page = remaining[-1]
        return {"session_id": session_id, "closed_page_index": page_index,
                "current_url": session.page.url if session.page else None, "remaining_pages": len(remaining)}

    @session_operation
    async def network_responses(self, session_id: str) -> dict[str, Any]:
        session = await self._session(session_id)
        return {
            "session_id": session_id,
            "responses": list(session.last_network),
            "count": len(session.last_network),
        }

    async def _drop_local(self, session: Session) -> None:
        # Browser Use owns its remote browser lifecycle; CloakBrowser is owned locally.
        # Cleanup is best-effort and strictly bounded: a wedged child must never keep
        # the MCP worker or a durable lease stuck forever.
        try:
            if session.provider == "cloak":
                if session.context is not None and hasattr(session.context, "close"):
                    try:
                        async with asyncio.timeout(5):
                            await session.context.close()
                    except Exception as exc:
                        LOG.warning("Cloak context cleanup failed: session=%s error=%s",
                                    session.id, type(exc).__name__)
                if session.browser is not None:
                    try:
                        async with asyncio.timeout(8):
                            await session.browser.close()
                    except Exception as exc:
                        LOG.warning("Cloak browser cleanup failed: session=%s error=%s",
                                    session.id, type(exc).__name__)
            if session.playwright is not None:
                if session.provider == "cloak":
                    try:
                        async with asyncio.timeout(8):
                            await session.playwright.stop()
                    except Exception as exc:
                        LOG.warning("Playwright cleanup failed: session=%s error=%s",
                                    session.id, type(exc).__name__)
                else:
                    # Preserve fail-visible cleanup semantics for Browser Use/CDP
                    # reconnects while still bounding the operation.
                    async with asyncio.timeout(8):
                        await session.playwright.stop()
        finally:
            session.playwright = None
            session.browser = None
            session.context = None
            session.page = None
            session.navigation_guard_installed = False
            session.redirect_guard = None
            session.redirect_guard_active = False
            session.websocket_guard_installed = False
            session.capability_guard_installed = False

    async def _stop_remote(self, session_id):
        # CloakBrowser sessions are local child processes. Their lifecycle is handled
        # by _drop_local; orphan durable leases after a worker restart require no
        # provider API call.
        if str(session_id).startswith("cloak-"):
            return
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.patch(f"{API_ROOT}/browsers/{session_id}",
                                          headers=self._headers(), json={"action": "stop"})
            if response.status_code in (404, 410):
                return
            response.raise_for_status()
            # A successful HTTP command is insufficient if the provider still
            # reports active. Verify terminal state without retaining its URLs.
            if response.json().get('status') != 'stopped':
                check = await client.get(f"{API_ROOT}/browsers/{session_id}", headers=self._headers())
                if check.status_code in (404, 410):
                    return
                check.raise_for_status()
                if check.json().get('status') != 'stopped':
                    raise RuntimeError('Provider did not confirm the browser stopped')

    async def _stop_claimed(self, session_id):
        await asyncio.to_thread(session_leases.renew_stop, session_id, self._owner_id)
        error = None
        retry_seconds = 60
        try:
            await self._stop_remote(session_id)
        except Exception as exc:
            error = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                error += ':' + str(exc.response.status_code)
                try:
                    retry_seconds = max(60, int(exc.response.headers.get('Retry-After', '60')))
                except ValueError:
                    pass
        await asyncio.to_thread(session_leases.finish, session_id, self._owner_id, error is None, error or '', retry_seconds)
        async with self._lock:
            session = self._sessions.get(session_id)
        if session:
            session.closing = True
            try:
                await self._drop_local(session)
            finally:
                if error is None:
                    async with self._lock:
                        self._sessions.pop(session_id, None)
        if error:
            # The durable STOP_FAILED record survives this process and retries
            # after its backoff. Never forget an unsuccessful remote stop.
            raise RuntimeError('browser stop failed: ' + error)
        async with self._lock:
            self._sessions.pop(session_id, None)
        return {'session_id': session_id, 'status': 'stopped'}

    async def stop(self, session_id: str) -> dict[str, Any]:
        session = await self._session(session_id)
        try:
            async with asyncio.timeout(self._stop_lock_timeout_seconds()):
                async with session.operation_lock:
                    claimed = await asyncio.to_thread(session_leases.claim_stop, session_id, self._owner_id)
                    if not claimed:
                        raise RuntimeError('browser session already claimed or no longer owned')
                    session.closing = True
                    return await self._stop_claimed(session_id)
        except TimeoutError:
            # A wedged operation must not make stop hang forever. The reaper may
            # already own the same durable lease in STOPPING state; treat that as
            # an in-progress stop instead of racing a second claim.
            session.closing = True
            claimed = await asyncio.to_thread(session_leases.claim_stop, session_id, self._owner_id)
            if claimed:
                return await self._stop_claimed(session_id)

            for _ in range(50):
                row = await asyncio.to_thread(session_leases.get_session, session_id)
                if row is None or row.get("state") == "STOPPED":
                    async with self._lock:
                        self._sessions.pop(session_id, None)
                    return {"session_id": session_id, "status": "stopped"}
                if row.get("state") != "STOPPING":
                    raise RuntimeError(
                        "browser session stop lock timed out and lease is not stopping"
                    )
                await asyncio.sleep(0.2)

            # Another same-owner cleanup is still running. Return a truthful
            # non-error state rather than wedging the MCP call.
            return {"session_id": session_id, "status": "stopping"}

    @staticmethod
    def _provider_from_session_id(session_id: str) -> str:
        return "cloak" if str(session_id).startswith("cloak-") else "browser_use"

    @staticmethod
    def _iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    async def _decorate_lease(self, row: dict[str, Any]) -> dict[str, Any]:
        session_id = str(row["session_id"])
        async with self._lock:
            local = self._sessions.get(session_id)
        return {
            "session_id": session_id,
            "provider": self._provider_from_session_id(session_id),
            "state": row["state"],
            "created_at": self._iso(row.get("created_at")),
            "last_activity_at": self._iso(row.get("last_activity_at")),
            "lease_until": self._iso(row.get("lease_until")),
            "updated_at": self._iso(row.get("updated_at")),
            "stop_error": row.get("stop_error") or None,
            "local": local is not None,
            "active_operation": local.operation_lock.locked() if local is not None else None,
            "closing": local.closing if local is not None else None,
        }

    async def list_sessions(self, include_stopped: bool = False, limit: int = 200) -> dict[str, Any]:
        rows = await asyncio.to_thread(session_leases.list_sessions, include_stopped, limit)
        sessions = [await self._decorate_lease(row) for row in rows]
        return {"sessions": sessions, "count": len(sessions), "include_stopped": include_stopped}

    async def session_status(self, session_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(session_leases.get_session, session_id)
        if row is None:
            raise KeyError("unknown browser session")
        return await self._decorate_lease(row)

    async def health(self) -> dict[str, Any]:
        try:
            database_ok = await asyncio.to_thread(session_leases.healthcheck)
        except Exception:
            database_ok = False
        browser_use_configured = bool(os.environ.get("BROWSER_USE_API_KEY", "").strip())
        cloak_enabled = self._cloak_enabled()
        cloak_runtime_available = importlib.util.find_spec("cloakbrowser") is not None
        async with self._lock:
            local_count = len(self._sessions)
        return {
            "ok": bool(database_ok and (browser_use_configured or cloak_enabled)),
            "database_ok": database_ok,
            "providers": {
                "browser_use": {"configured": browser_use_configured},
                "cloak": {"enabled": cloak_enabled, "runtime_available": cloak_runtime_available,
                          # #189: verified-runtime state and the fixed code that disabled it, if any.
                          "ready": self._cloak_ready, "disabled_code": self._cloak_failure,
                          "profile": self._cloak_profile.name if self._cloak_profile else None,
                          "max_local_sessions": 1, "memory_kills": self.cloak_memory_kills,
                          "last_sandbox_audit": self.cloak_audit},
            },
            "local_session_count": local_count,
            "reaper_error": self.reaper_error,
            # WP-006: the server-side kill switch, so a client sees why tools refuse.
            "browser_tools_enabled": browser_tools_enabled(),
        }

    async def cleanup_report(self) -> dict[str, Any]:
        try:
            states = await asyncio.to_thread(session_leases.state_summary)
            database_ok = True
        except Exception as exc:
            states = {}
            database_ok = False
            error = type(exc).__name__
        async with self._lock:
            local = list(self._sessions.values())
        report = {
            "database_ok": database_ok,
            "states": states,
            "open_durable_sessions": sum(
                states.get(name, 0) for name in ("ACTIVE", "STOPPING", "STOP_FAILED")
            ),
            "local_sessions": len(local),
            "local_busy_sessions": sum(1 for session in local if session.operation_lock.locked()),
            "last_reaper_error": self.reaper_error,
        }
        if not database_ok:
            report["error"] = error
        return report

    async def reap(self, idle_seconds=300):
        if type(idle_seconds) is not int or not 60 <= idle_seconds <= 21600:
            return {
                'stopped': [],
                'failed': [],
                'error': 'INVALID_IDLE_SECONDS',
                'idle_seconds': idle_seconds,
                'min_idle_seconds': 60,
                'max_idle_seconds': 21600,
            }
        async with self._lock:
            busy = [sid for sid, session in self._sessions.items() if session.operation_lock.locked()]
        ids = await asyncio.to_thread(session_leases.claim_stale, self._owner_id, idle_seconds, busy)
        stopped, failed = [], []
        for session_id in ids:
            async with self._lock:
                session = self._sessions.get(session_id)
            try:
                if session:
                    async with session.operation_lock:
                        session.closing = True
                        await self._stop_claimed(session_id)
                else:
                    await self._stop_claimed(session_id)
                stopped.append(session_id)
            except Exception as exc:
                failed.append({'session_id': session_id, 'error': type(exc).__name__})
        return {'stopped': stopped, 'failed': failed}

    async def stop_all_idle(self, idle_seconds=300) -> dict[str, Any]:
        return await self.reap(idle_seconds)

    async def reaper_loop(self):
        while True:
            try:
                result = await self.reap()
                self.reaper_error = result['failed'] or None
                if self.reaper_error:
                    LOG.error('Browser reaper failed for %s owned sessions', len(self.reaper_error))
            except Exception as exc:
                self.reaper_error = type(exc).__name__
                LOG.error('Browser reaper unavailable: %s', self.reaper_error)
            await asyncio.sleep(60)

    async def stop_all(self) -> dict[str, Any]:
        async with self._lock:
            ids = list(self._sessions)
        stopped, failed = [], []
        for session_id in ids:
            try:
                async with asyncio.timeout(20):
                    await self.stop(session_id)
                stopped.append(session_id)
            except Exception as exc:
                # Fixed code only (#188): the exception text (e.g. a DB error with host/user) never reaches the client.
                failed.append({"session_id": session_id, "error": browser_error_code(exc)})
        return {"stopped": stopped, "failed": failed}
