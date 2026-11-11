"""#188: a refused browser tool call reaches the MCP client as ONE fixed code, for both providers.

RED on main: every refusal (a ws:/wss: URL, a private address, a disabled kill switch, an unknown
session...) surfaced as the bare "Error executing tool <name>" of an unexpected crash, so a client
could not tell a policy refusal from a bug. Codes never carry a URL, hostname, exception message
or secret.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from bridge import browser_use, server, session_leases
from bridge.browser_use import BrowserUseBridge, Session

SECRET = "FCB-MOCK-SECRET-88"
PROVIDERS = ("browser_use", "cloak")


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "true")
    monkeypatch.setattr(session_leases, "renew", lambda *a, **k: None)
    b = BrowserUseBridge()
    monkeypatch.setattr(server, "bridge", b)
    return b


def _session(bridge, provider):
    sid = ("cloak-" if provider == "cloak" else "bu-") + "s1"
    page = SimpleNamespace(url="about:blank", frames=[], on=lambda *a: None, remove_listener=lambda *a: None)
    bridge._sessions[sid] = Session(sid, "local-cloak-owned" if provider == "cloak" else "wss://cdp.invalid",
                                    None, provider=provider,
                                    browser=SimpleNamespace(is_connected=lambda: True), page=page)
    return sid


def call(name, **arguments):
    """The tool through the MCP server, exactly as a client calls it."""
    try:
        asyncio.run(server.mcp.call_tool(name, arguments))
    except UnexpectedToolError as exc:  # the RED shape: a bare crash
        return "UNEXPECTED:" + str(exc)
    except ToolError as exc:
        return str(exc)
    return "OK"


def code_of(text, tool):
    prefix = f"Error executing tool {tool}: "
    assert text.startswith(prefix), text
    return text[len(prefix):]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("url", ["ws://echo.websocket.org", "wss://echo.websocket.org",
                                 "WSS://echo.websocket.org/x", "file:///etc/passwd", "ftp://example.com/",
                                 "javascript:alert(1)", "chrome://sandbox"])
def test_open_url_with_a_non_http_scheme_returns_url_scheme_not_allowed(bridge, provider, url):
    sid = _session(bridge, provider)
    text = call("browser_open_url", session_id=sid, url=url)
    assert code_of(text, "browser_open_url") == "URL_SCHEME_NOT_ALLOWED"
    assert "echo" not in text and "passwd" not in text
    assert sid in bridge._sessions  # the session survives the refusal


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("url,code", [
    ("http://127.0.0.1/", "URL_NON_PUBLIC_DESTINATION"),
    ("http://169.254.169.254/latest/meta-data/", "URL_NON_PUBLIC_DESTINATION"),
    ("http://[::ffff:10.0.0.1]/", "URL_NON_PUBLIC_DESTINATION"),
    ("http://user:pw@example.com/", "URL_CREDENTIALS_NOT_ALLOWED"),
    ("http:///nohost", "URL_HOST_REQUIRED"),
    ("http://does-not-resolve.invalid/", "URL_HOST_UNRESOLVABLE"),
])
def test_open_url_ssrf_refusals_have_fixed_codes(bridge, provider, url, code):
    sid = _session(bridge, provider)
    text = call("browser_open_url", session_id=sid, url=url)
    assert code_of(text, "browser_open_url") == code
    for fragment in ("127.0.0.1", "169.254", "user:pw", "invalid", "10.0.0.1"):
        assert fragment not in text


@pytest.mark.parametrize("provider", PROVIDERS)
def test_new_page_refusal_has_the_same_code(bridge, provider):
    sid = _session(bridge, provider)
    bridge._sessions[sid].context = SimpleNamespace(pages=[], new_page=AsyncMock())
    text = call("browser_new_page", session_id=sid, url="wss://echo.websocket.org")
    assert code_of(text, "browser_new_page") == "URL_SCHEME_NOT_ALLOWED"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_invalid_arguments_and_unknown_sessions(bridge, provider):
    sid = _session(bridge, provider)
    assert code_of(call("browser_open_url", session_id=sid, url="http://93.184.215.14/", wait_until="never"),
                   "browser_open_url") == "INVALID_ARGUMENT"
    assert code_of(call("browser_open_url", session_id="nope", url="wss://x"),
                   "browser_open_url") == "SESSION_NOT_FOUND"
    assert code_of(call("browser_wait", session_id=sid, milliseconds=99999), "browser_wait") == "INVALID_ARGUMENT"


def test_kill_switch_and_cloak_switch_have_fixed_codes(bridge, monkeypatch):
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "false")
    assert code_of(call("browser_create"), "browser_create") == "BROWSER_TOOLS_DISABLED"
    assert code_of(call("browser_create_cloak"), "browser_create_cloak") == "BROWSER_TOOLS_DISABLED"
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "true")
    monkeypatch.delenv("CLOAKBROWSER_ENABLED", raising=False)
    assert code_of(call("browser_create_cloak"), "browser_create_cloak") == "CLOAK_DISABLED"
    monkeypatch.setenv("CLOAKBROWSER_ENABLED", "true")
    assert code_of(call("browser_create_cloak"), "browser_create_cloak") == "CLOAK_RUNTIME_NOT_READY"


def test_navigation_policy_refusals_keep_their_codes(bridge, monkeypatch):
    sid = _session(bridge, "cloak")
    for message, code in ((browser_use.REDIRECT_BLOCKED, "NAVIGATION_REDIRECT_BLOCKED"),
                          (browser_use.DOCUMENT_REFUSED, "DOCUMENT_REFUSED"),
                          (browser_use.CONTENT_BLOCKED, "PAGE_CONTENT_NOT_PUBLIC")):
        async def refuse(*a, _m=message, _c=code, **k):
            raise browser_use.UnsafeURL(_m, _c)
        monkeypatch.setattr(bridge, "_page", refuse)
        assert code_of(call("browser_get_html", session_id=sid), "browser_get_html") == code


def test_an_unexpected_failure_is_a_fixed_code_without_its_message(bridge, monkeypatch):
    sid = _session(bridge, "browser_use")

    async def crash(*a, **k):
        raise RuntimeError(f"postgres://u:{SECRET}@db/x connection reset")
    monkeypatch.setattr(bridge, "_page", crash)
    text = call("browser_get_html", session_id=sid)
    assert code_of(text, "browser_get_html") == "BROWSER_INTERNAL_ERROR"
    assert SECRET not in text and "postgres" not in text


def test_a_successful_tool_is_unchanged(bridge, monkeypatch):
    monkeypatch.setattr(session_leases, "list_sessions", lambda *a: [])
    assert call("browser_list_sessions") == "OK"


def test_every_browser_tool_is_wrapped():
    """No browser_* tool may fall back to the SDK's bare crash message."""
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools if t.name.startswith("browser_")}
    wrapped = {name for name in names
               if getattr(server.mcp._tool_manager.get_tool(name).fn, "__wrapped__", None) is not None}
    assert names and wrapped == names


@pytest.mark.parametrize("exc,code", [
    (KeyError("unknown or stopped browser session"), "SESSION_NOT_FOUND"),
    (KeyError("browser session is stopping"), "SESSION_STOPPING"),
    (TimeoutError(), "BROWSER_TIMEOUT"),
    (ValueError("limit must be 1..200"), "INVALID_ARGUMENT"),
    (RuntimeError("browser stop failed: HTTPStatusError:500"), "BROWSER_STOP_FAILED"),
    (RuntimeError("BROWSER_USE_API_KEY is required"), "BROWSER_USE_NOT_CONFIGURED"),
    (RuntimeError(f"anything else {SECRET}"), "BROWSER_INTERNAL_ERROR"),
    (Exception(SECRET), "BROWSER_INTERNAL_ERROR"),
])
def test_error_code_table(exc, code):
    assert browser_use.browser_error_code(exc) == code


def test_stop_all_reports_fixed_codes_not_exception_text():
    """P2 review #191: browser_stop_all returns failures inside a successful result, so the
    _fixed_errors wrapper never sees them; the per-session error must still be a fixed code."""
    import asyncio
    from bridge import browser_use as bu

    secret = "connection to server at db.internal.example (10.0.0.5), user=avnadmin failed"

    class Boom(Exception):
        pass

    bridge = bu.BrowserUseBridge.__new__(bu.BrowserUseBridge)
    bridge._lock = asyncio.Lock()
    bridge._sessions = {"s-db": object(), "s-rt": object()}

    async def stop(session_id):
        if session_id == "s-db":
            raise Boom(secret)
        raise RuntimeError(secret)

    bridge.stop = stop
    result = asyncio.run(bridge.stop_all())
    assert result["stopped"] == []
    assert {f["session_id"] for f in result["failed"]} == {"s-db", "s-rt"}
    for item in result["failed"]:
        assert item["error"] == "BROWSER_INTERNAL_ERROR"
        assert secret not in repr(result)
