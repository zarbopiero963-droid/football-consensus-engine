import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pyee import EventEmitter

from bridge import browser_use, session_leases
from bridge.browser_use import BrowserUseBridge, Session
from bridge.security import UnsafeURL


class Page(EventEmitter):
    def __init__(self):
        super().__init__()
        self.url = 'https://public.example/test'
        self.error = None
        self.events = 2

    async def goto(self, url, **kwargs):
        self.url = url
        for index in range(self.events):
            self.emit('response', SimpleNamespace(
                request=SimpleNamespace(resource_type='xhr', method='GET'),
                url=f'{url}/{index}', status=200, headers={'content-type': 'application/json'}))
        if self.error:
            raise self.error
        return SimpleNamespace(status=200)

    async def title(self):
        return 'Public test'


@pytest.fixture
def bridge_page(monkeypatch):
    bridge = BrowserUseBridge()
    page = Page()
    session = Session('test', 'memory-only', None)
    bridge._sessions['test'] = session
    monkeypatch.setattr(session_leases, 'renew', lambda *args: None)
    monkeypatch.setattr(browser_use, 'validate_public_http_url', lambda url: url)
    monkeypatch.setattr(bridge, '_page', AsyncMock(return_value=page))
    return bridge, page, session


def test_navigation_does_not_retain_listeners_or_previous_events(bridge_page):
    bridge, page, session = bridge_page
    for url in ['https://public.example/one', 'https://public.example/two']:
        result = asyncio.run(bridge.open_url('test', url))
        assert result['network_events'] == 2
        assert page.listeners('response') == []
        assert all(event['url'].startswith(url) for event in session.last_network)


@pytest.mark.parametrize('error', [RuntimeError('navigation failed'), asyncio.CancelledError()])
def test_failed_navigation_removes_listener_and_replaces_stale_capture(bridge_page, error):
    bridge, page, session = bridge_page
    session.last_network = [{'url': 'stale'}]
    page.error = error
    with pytest.raises(type(error)):
        asyncio.run(bridge.open_url('test', page.url))
    assert page.listeners('response') == []
    assert len(session.last_network) == 2
    assert not any(event['url'] == 'stale' for event in session.last_network)


def test_final_url_rejection_still_cleans_listener(bridge_page, monkeypatch):
    bridge, page, session = bridge_page
    calls = []

    def validate(url):
        calls.append(url)
        if len(calls) == 2:
            raise UnsafeURL('redirect rejected')
        return url

    monkeypatch.setattr(browser_use, 'validate_public_http_url', validate)
    with pytest.raises(UnsafeURL):
        asyncio.run(bridge.open_url('test', page.url))
    assert page.listeners('response') == []


def test_capture_disabled_clears_previous_capture_without_listener(bridge_page):
    bridge, page, session = bridge_page
    session.last_network = [{'url': 'stale'}]
    result = asyncio.run(bridge.open_url('test', page.url, capture_network=False))
    assert result['network_events'] == 0
    assert session.last_network == []
    assert page.listeners('response') == []


def test_capture_is_bounded_during_navigation(bridge_page):
    bridge, page, session = bridge_page
    page.events = 2000
    page.on('response', lambda response: None)
    asyncio.run(bridge.open_url('test', page.url))
    assert len(session.last_network) == 500
    assert session.last_network[0]['url'].endswith('/1500')
    # The bridge removes only its own listener, preserving other consumers.
    assert len(page.listeners('response')) == 1


def test_reconnect_reinstalls_navigation_guard_and_drops_old_connection(monkeypatch):
    bridge = BrowserUseBridge()
    old_playwright = SimpleNamespace(stop=AsyncMock())
    old_browser = SimpleNamespace(is_connected=lambda: False)
    session = Session('test', 'memory-only', None, playwright=old_playwright,
                      browser=old_browser, page=object(), navigation_guard_installed=True)
    new_page = object()
    context = SimpleNamespace(pages=[new_page], route=AsyncMock())
    new_browser = SimpleNamespace(contexts=[context], is_connected=lambda: True)
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=new_browser)))
    monkeypatch.setattr(browser_use, 'async_playwright', Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=playwright))))
    assert asyncio.run(bridge._page(session)) is new_page
    old_playwright.stop.assert_awaited_once()
    context.route.assert_awaited_once_with('**/*', bridge._navigation_guard)
    assert asyncio.run(bridge._page(session)) is new_page
    context.route.assert_awaited_once()


def test_reconnect_cleanup_error_is_visible_and_clears_old_guard(monkeypatch):
    bridge = BrowserUseBridge()
    session = Session('test', 'memory-only', None, playwright=SimpleNamespace(
        stop=AsyncMock(side_effect=RuntimeError('disconnect cleanup failed'))),
        navigation_guard_installed=True)
    start = Mock()
    monkeypatch.setattr(browser_use, 'async_playwright', start)
    with pytest.raises(RuntimeError, match='disconnect cleanup failed'):
        asyncio.run(bridge._page(session))
    start.assert_not_called()
    assert session.playwright is session.page is session.browser is session.context is None
    assert not session.navigation_guard_installed
