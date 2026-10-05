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
        click=AsyncMock(),
        fill=AsyncMock(),
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

    page = bridge._sessions["cloak-actions"].page
    page.fill.assert_awaited_once()
    locator.fill.assert_not_awaited()
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


def test_click_uses_provider_compatible_action_path(monkeypatch):
    for session_id in ("cloak-click", "browser-use-click"):
        bridge, locator = make_bridge(monkeypatch, session_id)
        page = bridge._sessions[session_id].page
        result = run(bridge.click(session_id, "text=Login", wait_after_ms=0))
        if session_id.startswith("cloak-"):
            page.click.assert_awaited_once()
            locator.click.assert_not_awaited()
        else:
            locator.click.assert_awaited_once()
            page.click.assert_not_awaited()
        assert result["status"] == "clicked"
        assert result["selector"] == "text=Login"



def test_cloak_fill_falls_back_to_dom_when_page_fill_fails(monkeypatch):
    bridge, locator = make_bridge(monkeypatch, "cloak-actions")
    page = bridge._sessions["cloak-actions"].page
    page.fill.side_effect = RuntimeError("humanized fill timeout")
    locator.evaluate = AsyncMock()

    result = run(bridge.fill("cloak-actions", "input[type=text]", "Italy"))

    page.fill.assert_awaited_once()
    locator.evaluate.assert_awaited_once()
    assert result["status"] == "filled"


def test_cloak_click_falls_back_to_dom_when_page_click_fails(monkeypatch):
    bridge, locator = make_bridge(monkeypatch, "cloak-actions")
    page = bridge._sessions["cloak-actions"].page
    page.click.side_effect = RuntimeError("humanized click timeout")
    locator.evaluate = AsyncMock()

    result = run(bridge.click("cloak-actions", "button", wait_after_ms=0))

    page.click.assert_awaited_once()
    locator.evaluate.assert_awaited_once()
    assert result["status"] == "clicked"



def test_dom_text_falls_back_when_inner_text_times_out(monkeypatch):
    bridge, locator = make_bridge(monkeypatch, "cloak-dom")
    locator.inner_text = AsyncMock(side_effect=TimeoutError())
    locator.evaluate = AsyncMock(return_value="fallback text")

    result = run(bridge.dom_text("cloak-dom", "body", 10000))

    locator.inner_text.assert_awaited_once()
    locator.evaluate.assert_awaited_once()
    assert result["text"] == "fallback text"
