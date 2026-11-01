"""Read-only, host-pinned SX Bet HTTP client.

Only GET exists. There is deliberately no method able to POST/DELETE, so no
order placement, cancellation, approval, transfer or signature can originate
from this module. Error results carry fixed codes only: provider bodies,
exception messages and the API key are never reflected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .ratelimit import LocalRateLimiter, PostgresRateCoordinator, RateLimited, build_plan

_LOG = logging.getLogger(__name__)

NETWORKS = {
    "mainnet": "https://api.sx.bet",
    "testnet": "https://api.toronto.sx.bet",
}
_MAX_BODY = 524_288
_TIMEOUT_SECONDS = 15.0
_DEADLINE_SECONDS = 30.0  # whole request, not per phase
# Rate coordinator budgets: one-time schema ensure (cold start) and each reservation.
# Either expiring means SXBET_RATE_COORDINATOR_UNAVAILABLE and no provider call.
_COORDINATOR_PREPARE_SECONDS = 10.0
_COORDINATOR_TIMEOUT_SECONDS = 3.0
# Errors raised before any request leaves the process.
NO_NETWORK_CODES = frozenset({"SXBET_LOCAL_RATE_LIMITED", "SXBET_KEY_NOT_CONFIGURED",
                              "SXBET_RATE_COORDINATOR_UNAVAILABLE", "SXBET_INVALID_PATH"})


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
    # "postgres" when DATABASE_URL is set (production): shared, fail-closed coordinator.
    # "local" only without a database (tests/local); never a fallback for PG errors.
    rate_coordinator: str = "local"

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
        coordinator = "postgres" if e.get("DATABASE_URL", "").strip() else "local"
        return cls(enabled=enabled, network=network, api_key=key, max_rpm=rpm, rate_coordinator=coordinator)

    @property
    def base_url(self) -> str:
        return NETWORKS[self.network]

    def __repr__(self) -> str:  # never print the key
        return f"Settings(enabled={self.enabled}, network={self.network!r}, key_present={bool(self.api_key)}, max_rpm={self.max_rpm}, rate_coordinator={self.rate_coordinator!r})"


class SXBetError(Exception):
    def __init__(self, code: str, *, http_status: int | None = None, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after


# Back-compatible name: the in-process limiter now applies the same documented buckets.
_RateLimiter = LocalRateLimiter


def default_limiter(settings: Settings):
    if settings.rate_coordinator == "postgres":
        return PostgresRateCoordinator()
    if settings.rate_coordinator == "local":
        return LocalRateLimiter(settings.max_rpm)
    raise ValueError("unknown rate coordinator")


def _consume(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


def _coordinator_unavailable(stage: str, reason: str, exc: BaseException | None) -> SXBetError:
    # Fixed, secret-free diagnostics: exception CLASS name only, never its text (DSN, key).
    _LOG.warning("SXBET_RATE_COORDINATOR_UNAVAILABLE stage=%s reason=%s error_type=%s",
                 stage, reason, type(exc).__name__ if exc is not None else "-")
    return SXBetError("SXBET_RATE_COORDINATOR_UNAVAILABLE")


async def _bounded(coro, timeout: float, stage: str) -> None:
    """Run a coordinator step shielded and time-bounded; fail closed on anything but a decision."""
    task = asyncio.ensure_future(coro)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except RateLimited:
        raise
    except asyncio.CancelledError:
        task.add_done_callback(_consume)
        raise
    except (asyncio.TimeoutError, TimeoutError):
        # The worker thread cannot be cancelled; a late reservation stays consumed (conservative).
        task.add_done_callback(_consume)
        raise _coordinator_unavailable(stage, "TIMEOUT", None) from None
    except Exception as exc:  # psycopg errors, broken adapters, anything else
        raise _coordinator_unavailable(stage, "ERROR", exc) from None


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
        self._limiter = limiter or default_limiter(settings)

    async def get(self, path: str, params: Mapping[str, Any], *, auth: bool) -> dict[str, Any]:
        if not path.startswith("/") or "//" in path or ".." in path:
            raise SXBetError("SXBET_INVALID_PATH")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if auth:
            if not self.settings.api_key:
                raise SXBetError("SXBET_KEY_NOT_CONFIGURED")
            headers["x-sx-api-key"] = self.settings.api_key
        plan = build_plan(network=self.settings.network, api_key=self.settings.api_key,
                          max_rpm=self.settings.max_rpm, path=path, auth=auth)
        await self._admit(plan)
        try:
            async with asyncio.timeout(_DEADLINE_SECONDS):
                return await self._fetch(path, params, headers)
        except TimeoutError:
            raise SXBetError("SXBET_TIMEOUT") from None
        except SXBetError as exc:
            if exc.code == "SXBET_RATE_LIMITED":
                await self._record_rate_limited(plan, exc.retry_after)
            raise

    async def _admit(self, plan) -> None:
        """Reserve a slot in every bucket of the plan, or raise without any network call."""
        try:
            prepare = getattr(self._limiter, "prepare", None)
            if prepare is not None:
                await _bounded(prepare(), _COORDINATOR_PREPARE_SECONDS, "prepare")
            await _bounded(self._limiter.acquire(plan), _COORDINATOR_TIMEOUT_SECONDS, "reserve")
        except RateLimited as exc:
            raise SXBetError("SXBET_LOCAL_RATE_LIMITED", retry_after=exc.retry_after) from None

    async def _record_rate_limited(self, plan, retry_after: int | None) -> None:
        # Share the provider's back-off with every process. The 429 itself is still returned:
        # a failure here is logged with a fixed event and never retried or hidden.
        try:
            await _bounded(self._limiter.record_rate_limited(plan, retry_after),
                           _COORDINATOR_TIMEOUT_SECONDS, "cooldown")
        except SXBetError:
            pass

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
