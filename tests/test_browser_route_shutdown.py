import asyncio
import logging
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright._impl._errors import TargetClosedError

from bridge import browser_use
from bridge.browser_use import BrowserUseBridge, Session
from bridge.security import UnsafeURL


class Route:
    def __init__(self, error=None, navigation=False):
        self.error, self.actions = error, []
        self.request = SimpleNamespace(is_navigation_request=lambda: navigation,
                                       url='https://public.example/path')

    async def continue_(self):
        self.actions.append('continue')
        if self.error: raise self.error

    async def abort(self, reason):
        self.actions.append(reason)
        if self.error: raise self.error


def session(closing):
    return Session('opaque-session-id', 'memory-only-capability', None, closing=closing)


def test_target_closed_during_intentional_shutdown_is_logged_without_capabilities(caplog):
    caplog.set_level(logging.INFO, logger='bridge.browser_use')
    route = Route(TargetClosedError('synthetic target close'))
    asyncio.run(BrowserUseBridge()._navigation_guard(route, session(True)))
    assert route.actions == ['continue']
    assert 'Navigation route cancelled during session shutdown' in caplog.text
    assert 'opaque-session-id' in caplog.text and 'memory-only-capability' not in caplog.text


@pytest.mark.parametrize('closing,error', [
    (False, TargetClosedError('unexpected active close')),
    (True, PlaywrightError('unexpected protocol error')),
    (True, OSError('unexpected delivery error')),
])
def test_active_close_and_other_errors_are_never_hidden(closing, error):
    with pytest.raises(type(error)):
        asyncio.run(BrowserUseBridge()._navigation_guard(Route(error), session(closing)))


@pytest.mark.parametrize('closing', [False, True])
def test_private_navigation_is_aborted_before_any_continue_even_during_shutdown(monkeypatch, closing):
    def refuse(url): raise UnsafeURL('synthetic private target')
    monkeypatch.setattr(browser_use, 'validate_public_http_url', refuse)
    route = Route(navigation=True)
    asyncio.run(BrowserUseBridge()._navigation_guard(route, session(closing)))
    assert route.actions == ['blockedbyclient']


def test_abort_of_private_navigation_on_closed_target_still_never_continues(monkeypatch):
    def refuse(url): raise UnsafeURL('synthetic private target')
    monkeypatch.setattr(browser_use, 'validate_public_http_url', refuse)
    route = Route(TargetClosedError('closed before abort'), navigation=True)
    asyncio.run(BrowserUseBridge()._navigation_guard(route, session(True)))
    assert route.actions == ['blockedbyclient']


def test_page_installs_handler_bound_to_its_own_session(monkeypatch):
    handlers = []
    async def route(pattern, handler): handlers.append(handler)
    page = SimpleNamespace()
    context = SimpleNamespace(pages=[page], route=route)
    browser = SimpleNamespace(contexts=[context])
    async def connect(cdp): return browser
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    async def start(): return playwright
    monkeypatch.setattr(browser_use, 'async_playwright', lambda: SimpleNamespace(start=start))
    s = session(False)
    async def run():
        assert await BrowserUseBridge()._page(s) is page
        s.closing = True
        await handlers[0](Route(TargetClosedError('closed session target')))
    asyncio.run(run())
