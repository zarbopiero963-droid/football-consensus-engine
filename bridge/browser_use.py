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
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from collections import deque
from functools import wraps
from dataclasses import dataclass, field
from typing import Any

import httpx
from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, Route, async_playwright

from bridge.security import UnsafeURL, validate_public_http_url
from bridge import session_leases

API_ROOT = "https://api.browser-use.com/api/v4"
LOG = logging.getLogger(__name__)


def session_operation(method):
    @wraps(method)
    async def wrapped(self, session_id, *args, **kwargs):
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
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False


class BrowserUseBridge:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._owner_id = 'bridge-' + uuid.uuid4().hex
        self.reaper_error = None

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
        return {
            "session_id": session.id,
            "status": data.get("status", "active"),
            "live_url": session.live_url,
        }

    @staticmethod
    def _cloak_enabled() -> bool:
        return os.environ.get("CLOAKBROWSER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _cloak_headless() -> bool:
        return os.environ.get("CLOAKBROWSER_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}

    async def _launch_cloak(self):
        try:
            from cloakbrowser import launch_async
        except Exception as exc:
            raise RuntimeError("CloakBrowser runtime unavailable") from exc

        kwargs: dict[str, Any] = {
            "headless": self._cloak_headless(),
            "humanize": os.environ.get("CLOAKBROWSER_HUMANIZE", "true").strip().lower() in {"1", "true", "yes", "on"},
        }
        proxy = os.environ.get("CLOAKBROWSER_PROXY_URL", "").strip()
        if proxy:
            kwargs["proxy"] = proxy

        # CLOAKBROWSER_LICENSE_KEY is consumed by the upstream library directly.
        return await launch_async(**kwargs)

    async def create_cloak(self) -> dict[str, Any]:
        """Create one bridge-owned CloakBrowser session. No CDP or secret is returned."""
        if not self._cloak_enabled():
            raise RuntimeError("CloakBrowser is not enabled")

        # Cloak is a local Chromium process. Bound startup and refuse
        # unbounded local fan-out on small workers.
        async with self._lock:
            local_cloak = [
                s for s in self._sessions.values()
                if s.provider == "cloak" and not s.closing
            ]
        max_local = max(1, min(int(os.environ.get("CLOAKBROWSER_MAX_LOCAL_SESSIONS", "1")), 4))
        if len(local_cloak) >= max_local:
            raise RuntimeError("CloakBrowser local session capacity reached")
        async with asyncio.timeout(45):
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
            async def guard(route):
                await self._navigation_guard(route, session)
            await session.context.route("**/*", guard)
            session.navigation_guard_installed = True
            await asyncio.to_thread(session_leases.register, session.id, self._owner_id)
        except Exception:
            try:
                await browser.close()
            except Exception:
                pass
            raise

        async with self._lock:
            self._sessions[session.id] = session
        return {"session_id": session.id, "status": "active", "provider": "cloak"}

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

        if not session.navigation_guard_installed:
            async def guard(route):
                await self._navigation_guard(route, session)
            await session.context.route("**/*", guard)
            session.navigation_guard_installed = True
        return session.page

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

        try:
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            final_url = page.url
            await asyncio.to_thread(validate_public_http_url, final_url)
            return {
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
    async def json_document(self, session_id: str, url: str, max_bytes: int = 5 * 1024 * 1024):
        """Complete public JSON document bytes, never truncated DOM text."""
        await asyncio.to_thread(validate_public_http_url, url)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 5 * 1024 * 1024:
            raise ValueError('invalid document size limit')
        session = await self._session(session_id)
        page = await self._page(session)
        session.last_network = []
        response = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
        await asyncio.to_thread(validate_public_http_url, page.url)
        if page.url != url or response is None or response.status != 200:
            raise RuntimeError('JSON document status or final URL mismatch')
        if 'application/json' not in response.headers.get('content-type', '').lower():
            raise RuntimeError('JSON document content type mismatch')
        length = response.headers.get('content-length')
        if length and int(length) > max_bytes:
            raise RuntimeError('JSON document exceeds size limit')
        raw = await response.body()
        if len(raw) > max_bytes:
            raise RuntimeError('JSON document exceeds size limit')
        body = raw.decode('utf-8')
        def invalid_constant(value):
            raise ValueError('nonfinite JSON constant')
        rows = json.loads(body, parse_constant=invalid_constant)
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise RuntimeError('JSON document is not an object list')
        return {'requested_url': url, 'final_url': page.url, 'http_status': response.status,
                'body': body, 'bytes_utf8': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                'captured_at_utc': datetime.now(timezone.utc).isoformat(), 'truncated': False}

    @session_operation
    async def html(self, session_id: str, selector: str | None = None) -> dict[str, Any]:
        session = await self._session(session_id)
        page = await self._page(session)
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
        max_chars = max(1000, min(int(max_chars), 500000))
        locator = page.locator(selector).first
        try:
            async with asyncio.timeout(12):
                text = await locator.inner_text(timeout=10000)
        except (TimeoutError, PlaywrightError):
            # innerText may wait on layout/visibility in reactive pages.
            # textContent is a cheaper DOM read and avoids wedging the worker.
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
        response = await page.go_back(wait_until=wait_until, timeout=self._bounded_timeout(timeout_ms))
        await asyncio.to_thread(validate_public_http_url, page.url)
        return {"session_id": session_id, "url": page.url, "title": await page.title(),
                "http_status": response.status if response else None}

    @session_operation
    async def forward(self, session_id: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> dict[str, Any]:
        if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError("invalid wait_until")
        session = await self._session(session_id)
        page = await self._page(session)
        response = await page.go_forward(wait_until=wait_until, timeout=self._bounded_timeout(timeout_ms))
        await asyncio.to_thread(validate_public_http_url, page.url)
        return {"session_id": session_id, "url": page.url, "title": await page.title(),
                "http_status": response.status if response else None}

    @session_operation
    async def reload(self, session_id: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> dict[str, Any]:
        if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError("invalid wait_until")
        session = await self._session(session_id)
        page = await self._page(session)
        response = await page.reload(wait_until=wait_until, timeout=self._bounded_timeout(timeout_ms))
        await asyncio.to_thread(validate_public_http_url, page.url)
        return {"session_id": session_id, "url": page.url, "title": await page.title(),
                "http_status": response.status if response else None}

    @session_operation
    async def elements(self, session_id: str, selector: str = "input,button,a,select,textarea", limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        session = await self._session(session_id)
        page = await self._page(session)
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
            items.append({"index": index, "url": candidate.url, "title": await candidate.title(),
                          "current": candidate is session.page})
        return {"session_id": session_id, "pages": items, "count": len(items)}

    @session_operation
    async def new_page(self, session_id: str, url: str | None = None) -> dict[str, Any]:
        session = await self._session(session_id)
        await self._page(session)
        if session.context is None:
            raise RuntimeError("browser context unavailable")
        page = await session.context.new_page()
        session.page = page
        if url:
            await asyncio.to_thread(validate_public_http_url, url)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.to_thread(validate_public_http_url, page.url)
        return {"session_id": session_id, "url": page.url, "title": await page.title(),
                "page_index": list(session.context.pages).index(page)}

    @session_operation
    async def switch_page(self, session_id: str, page_index: int) -> dict[str, Any]:
        session = await self._session(session_id)
        await self._page(session)
        pages = list(session.context.pages if session.context else [])
        if type(page_index) is not int or page_index < 0 or page_index >= len(pages):
            raise ValueError("invalid page_index")
        session.page = pages[page_index]
        await asyncio.to_thread(validate_public_http_url, session.page.url)
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
        try:
            if session.provider == "cloak" and session.browser is not None:
                try:
                    async with asyncio.timeout(12):
                        await session.browser.close()
                except Exception:
                    # Cleanup must still clear all local references even if the child browser
                    # already exited.
                    if session.browser.is_connected():
                        raise
            if session.playwright is not None:
                await session.playwright.stop()
        finally:
            session.playwright = None
            session.browser = None
            session.context = None
            session.page = None
            session.navigation_guard_installed = False

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
        async with session.operation_lock:
            claimed = await asyncio.to_thread(session_leases.claim_stop, session_id, self._owner_id)
            if not claimed:
                raise RuntimeError('browser session already claimed or no longer owned')
            session.closing = True
            return await self._stop_claimed(session_id)

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
                "cloak": {"enabled": cloak_enabled, "runtime_available": cloak_runtime_available},
            },
            "local_session_count": local_count,
            "reaper_error": self.reaper_error,
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
                failed.append({"session_id": session_id, "error": str(exc)})
        return {"stopped": stopped, "failed": failed}
