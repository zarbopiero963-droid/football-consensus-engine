from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from playwright.async_api import async_playwright

from bridge.security import validate_public_http_url

API_ROOT = "https://api.browser-use.com/api/v4"


@dataclass
class Session:
    id: str
    cdp_url: str
    live_url: str | None
    created_monotonic: float = field(default_factory=time.monotonic)
    last_network: list[dict[str, Any]] = field(default_factory=list)


class BrowserUseBridge:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

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
        async with self._lock:
            self._sessions[session.id] = session

        # Never expose cdpUrl. It is a bearer-like capability URL.
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

    async def _connect_page(self, session: Session):
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(session.cdp_url)
        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        pages = context.pages
        page = pages[0] if pages else await context.new_page()
        return pw, browser, context, page

    async def open_url(
        self,
        session_id: str,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 45000,
        capture_network: bool = True,
    ) -> dict[str, Any]:
        validate_public_http_url(url)
        if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError("invalid wait_until")
        timeout_ms = max(1000, min(int(timeout_ms), 120000))
        session = await self._session(session_id)
        network: list[dict[str, Any]] = []
        pw, browser, _context, page = await self._connect_page(session)
        try:
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
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            final_url = page.url
            validate_public_http_url(final_url)
            title = await page.title()
            session.last_network = network[-500:]
            return {
                "session_id": session_id,
                "requested_url": url,
                "final_url": final_url,
                "title": title,
                "http_status": response.status if response else None,
                "network_events": len(session.last_network),
            }
        finally:
            await browser.close()
            await pw.stop()

    async def html(self, session_id: str, selector: str | None = None) -> dict[str, Any]:
        session = await self._session(session_id)
        pw, browser, _context, page = await self._connect_page(session)
        try:
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
        finally:
            await browser.close()
            await pw.stop()

    async def dom_text(self, session_id: str, selector: str = "body", max_chars: int = 100000) -> dict[str, Any]:
        session = await self._session(session_id)
        max_chars = max(1000, min(int(max_chars), 500000))
        pw, browser, _context, page = await self._connect_page(session)
        try:
            locator = page.locator(selector).first
            text = await locator.inner_text()
            return {
                "session_id": session_id,
                "url": page.url,
                "selector": selector,
                "text": text[:max_chars],
                "truncated": len(text) > max_chars,
            }
        finally:
            await browser.close()
            await pw.stop()

    async def screenshot(self, session_id: str, full_page: bool = True) -> dict[str, Any]:
        session = await self._session(session_id)
        pw, browser, _context, page = await self._connect_page(session)
        try:
            body = await page.screenshot(full_page=bool(full_page), type="png")
            return {
                "session_id": session_id,
                "url": page.url,
                "mime_type": "image/png",
                "base64": base64.b64encode(body).decode("ascii"),
                "bytes": len(body),
            }
        finally:
            await browser.close()
            await pw.stop()

    async def network_responses(self, session_id: str) -> dict[str, Any]:
        session = await self._session(session_id)
        return {
            "session_id": session_id,
            "responses": list(session.last_network),
            "count": len(session.last_network),
        }

    async def stop(self, session_id: str) -> dict[str, Any]:
        session = await self._session(session_id)
        error: str | None = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.patch(
                    f"{API_ROOT}/browsers/{session.id}",
                    headers=self._headers(),
                    json={"action": "stop"},
                )
                response.raise_for_status()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            async with self._lock:
                self._sessions.pop(session_id, None)

        if error:
            raise RuntimeError(f"browser stop failed; local session removed: {error}")
        return {"session_id": session_id, "status": "stopped"}

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
