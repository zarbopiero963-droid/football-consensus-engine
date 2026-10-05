import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bridge import session_leases
from bridge.browser_use import BrowserUseBridge, Session


def run(coro):
    return asyncio.run(coro)


def make_bridge(monkeypatch, session_id="cloak-actions"):
    bridge = BrowserUseBridge()
    locator = SimpleNamespace(
        fill=AsyncMock(),
        click=AsyncMock(),
        press_sequentially=AsyncMock(),
    )
    locator.first = locator
    page = SimpleNamespace(
        url="https://example.com/",
        locator=lambda selector: locator,
        wait_for_timeout=AsyncMock(),
    )
    browser = SimpleNamespace(is_connected=lambda: True)
    session = Session(
        session_id,
        "local-test",
        None,
        provider="cloak" if session_id.startswith("cloak-") else "browser_use",
        browser=browser,
        context=SimpleNamespace(),
        page=page,
    )
    bridge._sessions[session_id] = session
    monkeypatch.setattr(session_leases, "renew", lambda *args: None)
    return bridge, locator


def test_fill_never_echoes_secret(monkeypatch):
    bridge, locator = make_bridge(monkeypatch)
    secret = "super-secret-password"
    result = run(bridge.fill("cloak-actions", "input[type=password]", secret))

    locator.fill.assert_awaited_once()
    assert secret not in repr(result)
    assert result["status"] == "filled"


def test_type_never_echoes_text(monkeypatch):
    bridge, locator = make_bridge(monkeypatch, "browser-use-actions")
    secret = "private-user-value"
    result = run(bridge.type_text("browser-use-actions", "#username", secret, delay_ms=25))

    locator.click.assert_awaited_once()
    locator.press_sequentially.assert_awaited_once()
    assert secret not in repr(result)
    assert result["status"] == "typed"


def test_click_is_shared_by_both_provider_session_types(monkeypatch):
    for session_id in ("cloak-click", "browser-use-click"):
        bridge, locator = make_bridge(monkeypatch, session_id)
        result = run(bridge.click(session_id, "text=Login", wait_after_ms=0))
        locator.click.assert_awaited_once()
        assert result["status"] == "clicked"
        assert result["selector"] == "text=Login"
