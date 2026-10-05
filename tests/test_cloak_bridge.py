import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bridge import session_leases
from bridge.browser_use import BrowserUseBridge, Session


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
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), route=AsyncMock())
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
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
