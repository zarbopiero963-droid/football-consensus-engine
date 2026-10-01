from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
import logging
from contextlib import asynccontextmanager
from collections import deque
from functools import wraps
from dataclasses import dataclass, field
from typing import Any

import httpx
from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route, async_playwright

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

    async def _navigation_guard(self, route: Route) -> None:
        request = route.request
        if request.is_navigation_request():
            try:
                await asyncio.to_thread(validate_public_http_url, request.url)
            except UnsafeURL:
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    async def _page(self, session: Session) -> Page:
        if session.browser is not None and session.browser.is_connected() and session.page is not None:
            return session.page

        await self._drop_local(session)

        session.playwright = await async_playwright().start()
        session.browser = await session.playwright.chromium.connect_over_cdp(session.cdp_url)
        contexts = session.browser.contexts
        session.context = contexts[0] if contexts else await session.browser.new_context()
        pages = session.context.pages
        session.page = pages[0] if pages else await session.context.new_page()

        if not session.navigation_guard_installed:
            await session.context.route("**/*", self._navigation_guard)
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
        text = await page.locator(selector).first.inner_text()
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

    @session_operation
    async def network_responses(self, session_id: str) -> dict[str, Any]:
        session = await self._session(session_id)
        return {
            "session_id": session_id,
            "responses": list(session.last_network),
            "count": len(session.last_network),
        }

    async def _drop_local(self, session: Session) -> None:
        # Do not call Browser.close(): the Browser Use API owns the remote browser lifecycle.
        try:
            if session.playwright is not None:
                await session.playwright.stop()
        finally:
            session.playwright = None
            session.browser = None
            session.context = None
            session.page = None
            session.navigation_guard_installed = False

    async def _stop_remote(self, session_id):
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

    async def reap(self, idle_seconds=300):
        if not 60 <= idle_seconds <= 21600:
            raise ValueError('idle_seconds must be between 60 and 21600')
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
                await self.stop(session_id)
                stopped.append(session_id)
            except Exception as exc:
                failed.append({"session_id": session_id, "error": str(exc)})
        return {"stopped": stopped, "failed": failed}
