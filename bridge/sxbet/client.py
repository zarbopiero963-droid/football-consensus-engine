"""Read-only, host-pinned SX Bet HTTP client.

Only GET exists. There is deliberately no method able to POST/DELETE, so no
order placement, cancellation, approval, transfer or signature can originate
from this module. Error results carry fixed codes only: provider bodies,
exception messages and the API key are never reflected.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

NETWORKS = {
    "mainnet": "https://api.sx.bet",
    "testnet": "https://api.toronto.sx.bet",
}
_MAX_BODY = 524_288
_TIMEOUT_SECONDS = 15.0
_DEADLINE_SECONDS = 30.0  # whole request, not per phase


def _ascii_int(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 9 else None


def _reject_constant(value: str):
    raise ValueError("non-finite JSON number")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    network: str = "mainnet"
    api_key: str = ""
    max_rpm: int = 60

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        network = e.get("SXBET_NETWORK", "mainnet").strip().lower()
        if network not in NETWORKS:
            raise ValueError("SXBET_NETWORK must be mainnet or testnet")
        try:
            rpm = int(e.get("SXBET_MAX_RPM", "60"))
        except ValueError:
            raise ValueError("SXBET_MAX_RPM must be an integer") from None
        if not 1 <= rpm <= 600:
            raise ValueError("SXBET_MAX_RPM out of bounds 1..600")
        enabled = e.get("SXBET_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        key = e.get("SXBET_API_KEY", "").strip()
        if key and not (key.isascii() and key.isprintable() and len(key) <= 512):
            raise ValueError("SXBET_API_KEY must be printable ASCII")
        return cls(enabled=enabled, network=network, api_key=key, max_rpm=rpm)

    @property
    def base_url(self) -> str:
        return NETWORKS[self.network]

    def __repr__(self) -> str:  # never print the key
        return f"Settings(enabled={self.enabled}, network={self.network!r}, key_present={bool(self.api_key)}, max_rpm={self.max_rpm})"


class SXBetError(Exception):
    def __init__(self, code: str, *, http_status: int | None = None, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after


class _RateLimiter:
    """Local sliding 60 s window; conservative versus the provider limits."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute = per_minute
        self.clock = clock
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = self.clock()
            while self.calls and now - self.calls[0] >= 60:
                self.calls.popleft()
            if len(self.calls) >= self.per_minute:
                raise SXBetError("SXBET_LOCAL_RATE_LIMITED",
                                 retry_after=max(1, int(60 - (now - self.calls[0])) + 1))
            self.calls.append(now)


def _encode(params: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[key] = ",".join(str(v) for v in value)  # OpenAPI: comma-separated
        else:
            out[key] = str(value)
    return out


class SXBetReadClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                 limiter: _RateLimiter | None = None):
        self.settings = settings
        self._transport = transport
        self._limiter = limiter or _RateLimiter(settings.max_rpm)

    async def get(self, path: str, params: Mapping[str, Any], *, auth: bool) -> dict[str, Any]:
        if not path.startswith("/") or "//" in path or ".." in path:
            raise SXBetError("SXBET_INVALID_PATH")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if auth:
            if not self.settings.api_key:
                raise SXBetError("SXBET_KEY_NOT_CONFIGURED")
            headers["x-sx-api-key"] = self.settings.api_key
        await self._limiter.acquire()
        try:
            async with asyncio.timeout(_DEADLINE_SECONDS):
                return await self._fetch(path, params, headers)
        except TimeoutError:
            raise SXBetError("SXBET_TIMEOUT") from None

    async def _fetch(self, path: str, params: Mapping[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=self.settings.base_url, timeout=_TIMEOUT_SECONDS,
                                         follow_redirects=False, trust_env=True,
                                         transport=self._transport) as client:
                async with client.stream("GET", path, params=_encode(params), headers=headers) as response:
                    code = response.status_code
                    if 300 <= code <= 399:
                        raise SXBetError("SXBET_REDIRECT_REJECTED", http_status=code)
                    if code == 429:
                        raw = response.headers.get("retry-after", "")
                        raise SXBetError("SXBET_RATE_LIMITED", http_status=code, retry_after=_ascii_int(raw))
                    if code in (401, 403):
                        raise SXBetError("SXBET_UNAUTHORIZED", http_status=code)
                    if not 200 <= code <= 299:
                        raise SXBetError("SXBET_HTTP_ERROR", http_status=code)
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > _MAX_BODY:
                            raise SXBetError("SXBET_RESPONSE_TOO_LARGE", http_status=code)
                        chunks.append(chunk)
                    remaining = response.headers.get("x-ratelimit-remaining", "")
        except httpx.TimeoutException:
            raise SXBetError("SXBET_TIMEOUT") from None
        except httpx.RequestError:
            raise SXBetError("SXBET_NETWORK_ERROR") from None
        try:
            body = json.loads(b"".join(chunks), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise SXBetError("SXBET_INVALID_JSON", http_status=code) from None
        if not isinstance(body, dict) or body.get("status") != "success" or "data" not in body:
            raise SXBetError("SXBET_PROVIDER_STATUS_NOT_SUCCESS", http_status=code)
        return {"http_status": code, "data": body["data"],
                "rate_limit_remaining": _ascii_int(remaining)}
