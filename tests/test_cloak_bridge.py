import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bridge import session_leases
from bridge.browser_use import BrowserUseBridge, Session


@pytest.fixture(autouse=True)
def _browser_tools_on(monkeypatch):
    """The kill switch is fail-closed (audit F-03): these tests exercise an enabled bridge."""
    monkeypatch.setenv("BROWSER_TOOLS_ENABLED", "true")


def run(coro):
    return asyncio.run(coro)


def test_cloak_is_fail_closed_when_disabled(monkeypatch):
    monkeypatch.delenv("CLOAKBROWSER_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        run(BrowserUseBridge().create_cloak())


def test_cloak_create_returns_only_opaque_session(monkeypatch):
    bridge = BrowserUseBridge()
    monkeypatch.setenv("CLOAKBROWSER_ENABLED", "true")
    monkeypatch.setattr(session_leases, "register", lambda *args: None)

    page = SimpleNamespace()
    cdp = SimpleNamespace(on=Mock(), send=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), route=AsyncMock(), pages=[page],
                              on=Mock(), route_web_socket=AsyncMock(), add_init_script=AsyncMock())
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        new_browser_cdp_session=AsyncMock(return_value=cdp),
        close=AsyncMock(),
        is_connected=lambda: True,
    )
    monkeypatch.setattr(bridge, "_launch_cloak", AsyncMock(return_value=browser))

    result = run(bridge.create_cloak())

    assert result["status"] == "active"
    assert result["provider"] == "cloak"
    assert result["session_id"].startswith("cloak-")
    assert "cdp" not in " ".join(result.keys()).lower()
    session = bridge._sessions[result["session_id"]]
    assert session.provider == "cloak"
    assert session.cdp_url == "local-cloak-owned"
    context.route.assert_awaited_once()
    browser.new_browser_cdp_session.assert_awaited_once_with()  # WP-001 browser-level redirect guard
    assert session.redirect_guard_active is True
    context.route_web_socket.assert_awaited_once()  # WP-002 WebSocket guard
    context.add_init_script.assert_awaited_once()  # WP-002b capability guard
    browser.close.assert_not_awaited()


def test_cloak_stop_closes_local_browser_without_browser_use_api(monkeypatch):
    bridge = BrowserUseBridge()
    browser = SimpleNamespace(close=AsyncMock(), is_connected=lambda: True)
    session = Session(
        "cloak-test",
        "local-cloak-owned",
        None,
        provider="cloak",
        browser=browser,
        context=SimpleNamespace(),
        page=SimpleNamespace(),
    )
    bridge._sessions[session.id] = session

    monkeypatch.setattr(session_leases, "claim_stop", lambda *args: True)
    monkeypatch.setattr(session_leases, "renew_stop", lambda *args: None)
    monkeypatch.setattr(session_leases, "finish", lambda *args: None)
    remote = AsyncMock()
    monkeypatch.setattr(bridge, "_stop_remote", remote)

    result = run(bridge.stop(session.id))

    assert result == {"session_id": "cloak-test", "status": "stopped"}
    remote.assert_awaited_once_with("cloak-test")
    browser.close.assert_awaited_once()
    assert session.id not in bridge._sessions


def test_cloak_orphan_stop_never_calls_browser_use_http(monkeypatch):
    bridge = BrowserUseBridge()
    # Prefix routing is the durable provider marker for restart cleanup.
    run(bridge._stop_remote("cloak-orphan"))


def test_cloak_page_does_not_silently_relaunch_after_disconnect():
    bridge = BrowserUseBridge()
    session = Session(
        "cloak-test",
        "local-cloak-owned",
        None,
        provider="cloak",
        browser=SimpleNamespace(is_connected=lambda: False),
    )
    with pytest.raises(RuntimeError, match="no longer connected"):
        run(bridge._page(session))



def test_cloak_create_enforces_local_capacity(monkeypatch):
    bridge = BrowserUseBridge()
    monkeypatch.setenv("CLOAKBROWSER_ENABLED", "true")
    monkeypatch.setenv("CLOAKBROWSER_MAX_LOCAL_SESSIONS", "1")
    existing = Session(
        "cloak-existing",
        "local-cloak-owned",
        None,
        provider="cloak",
        browser=SimpleNamespace(is_connected=lambda: True),
        context=SimpleNamespace(),
        page=SimpleNamespace(),
    )
    bridge._sessions[existing.id] = existing
    launch = AsyncMock()
    monkeypatch.setattr(bridge, "_launch_cloak", launch)

    with pytest.raises(RuntimeError, match="capacity reached"):
        run(bridge.create_cloak())

    launch.assert_not_awaited()



def test_cloak_stop_survives_close_failure(monkeypatch):
    bridge = BrowserUseBridge()
    browser = SimpleNamespace(
        close=AsyncMock(side_effect=TimeoutError()),
        is_connected=lambda: True,
    )
    context = SimpleNamespace(close=AsyncMock())
    session = Session(
        "cloak-timeout",
        "local-cloak-owned",
        None,
        provider="cloak",
        browser=browser,
        context=context,
        page=SimpleNamespace(),
    )
    bridge._sessions[session.id] = session

    monkeypatch.setattr(session_leases, "claim_stop", lambda *args: True)
    monkeypatch.setattr(session_leases, "renew_stop", lambda *args: None)
    monkeypatch.setattr(session_leases, "finish", lambda *args: None)

    result = run(bridge.stop(session.id))

    assert result == {"session_id": "cloak-timeout", "status": "stopped"}
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    assert session.id not in bridge._sessions



def test_prepare_cloak_runtime_downloads_binary(monkeypatch, tmp_path):
    # Intentional change (#189): prepare now downloads the PINNED version only (keyless 146 profile)
    # and accepts it only when its SHA-256 matches the pin; it still downloads when nothing is cached.
    from bridge import browser_use
    bridge = BrowserUseBridge()
    monkeypatch.setenv("CLOAKBROWSER_ENABLED", "true")
    monkeypatch.delenv("CLOAKBROWSER_BINARY_PATH", raising=False)
    monkeypatch.delenv("CLOAKBROWSER_LAUNCHER", raising=False)
    monkeypatch.delenv("CLOAKBROWSER_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr(browser_use, "_set_bridge_non_dumpable", lambda: None)
    monkeypatch.setattr(browser_use, "_scrub_playwright_driver_env", lambda: None)
    binary = tmp_path / "chrome"
    binary.write_bytes(b"pinned-test-binary")
    pin = browser_use._sha256_file(str(binary))
    monkeypatch.setitem(browser_use.CLOAK_PROFILES, "pinned-146", browser_use.CloakProfile(
        "pinned-146", "146.0.7680.177.5", pin, False))
    module = ModuleType("cloakbrowser")
    module.__version__ = "0.5.11"
    config = ModuleType("cloakbrowser.config")
    config.get_binary_path = lambda version: tmp_path / "not-cached" / "chrome"
    calls = []
    module.ensure_binary = lambda **kw: calls.append(kw) or str(binary)
    monkeypatch.setitem(sys.modules, "cloakbrowser", module)
    monkeypatch.setitem(sys.modules, "cloakbrowser.config", config)
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", "")
    monkeypatch.setenv("CLOAKBROWSER_AUTO_UPDATE", "true")  # forced to false, restored after the test

    run(bridge.prepare_cloak_runtime())

    assert calls == [{"browser_version": "146.0.7680.177.5"}]
    assert bridge._cloak_ready is True and bridge._cloak_failure is None
