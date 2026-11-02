"""FCB-WP-003 — the only way the live API-FOOTBALL scripts may reach the provider.

The manual live scripts (route inventory, capability probe, live smoke) need the raw
HTTP status, headers and bytes, so they cannot use ``ResilientFootballClient``'s
normalized output. They use :func:`coordinated_client` instead, which exposes the small
subset of ``httpx.AsyncClient`` they need (``get`` and ``stream``) and puts every request
behind the same controls as the MCP runtime (CLAUDE.md §2.4):

* the shared PostgreSQL coordinator (:class:`PostgresQuota`): one durable quota unit and
  one concurrency lease per real attempt, counted even when the attempt fails;
* a circuit breaker: provider 5xx/429 or transport failures open it, and an open breaker
  refuses further requests without sending them;
* a provider-reported zero remaining quota is written back to the coordinator.

Fail closed: without ``DATABASE_URL`` (or an injected coordinator, tests only) no client
is built and nothing is sent. Coordinator failures never echo DSN, SQL or key material.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .circuit import CircuitBreaker
from .config import OFFICIAL_BASE_URL, Settings
from .errors import CircuitOpen
from .quota import PostgresQuota, QuotaConcurrencyBusy, QuotaExceeded, QuotaLimits

COORDINATOR_REQUIRED = "COORDINATOR_DATABASE_REQUIRED"
_LEASE_TIMEOUT_SECONDS = 15.0


class CoordinatorRefused(RuntimeError):
    """The request was NOT sent. ``code`` is a fixed, public category."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _remaining(headers: Mapping[str, str], name: str) -> int | None:
    raw = headers.get(name)
    return int(raw) if isinstance(raw, str) and raw.isdecimal() and len(raw) <= 12 else None


class CoordinatedClient:
    """Provider client whose every request holds a coordinator lease and passes the breaker."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings, quota: Any, breaker: CircuitBreaker):
        self._http = http
        self._settings = settings
        self._quota = quota
        self._breaker = breaker
        self.attempts = 0  # leases actually granted (= requests sent or attempted)

    async def _acquire(self):
        prepare = getattr(self._quota, "prepare", None)
        try:
            if prepare is not None:
                await asyncio.wait_for(prepare(), timeout=_LEASE_TIMEOUT_SECONDS)
            return await asyncio.wait_for(self._quota.acquire(self._settings.api_key),
                                          timeout=_LEASE_TIMEOUT_SECONDS)
        except QuotaExceeded:
            raise CoordinatorRefused("QUOTA_EXCEEDED") from None
        except QuotaConcurrencyBusy:
            raise CoordinatorRefused("QUOTA_CONCURRENCY_BUSY") from None
        except asyncio.CancelledError:
            raise
        except Exception:
            # DSN, psycopg detail and the API key never leave this module.
            raise CoordinatorRefused("COORDINATOR_UNAVAILABLE") from None

    async def _release(self, lease) -> None:
        try:
            await asyncio.shield(self._quota.release(lease))
        except asyncio.CancelledError:
            raise
        except Exception:
            # The lease expires server-side; the failure stays visible to the caller.
            raise CoordinatorRefused("QUOTA_RELEASE_FAILED") from None

    async def _reconcile(self, response: httpx.Response) -> None:
        for header, kind in (("x-ratelimit-requests-remaining", "day"), ("x-ratelimit-remaining", "minute")):
            if _remaining(response.headers, header) == 0:
                try:
                    await self._quota.mark_exhausted(self._settings.api_key, kind)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise CoordinatorRefused("QUOTA_RECONCILIATION_FAILED") from None

    async def _settle(self, status: int | None) -> None:
        if status is None or status == 429 or status >= 500:
            await self._breaker.fail()
        else:
            await self._breaker.success()

    @asynccontextmanager
    async def _guarded(self) -> AsyncIterator[dict]:
        try:
            await self._breaker.before_request()
        except CircuitOpen:
            raise CoordinatorRefused("CIRCUIT_OPEN") from None
        outcome: dict = {"status": None, "settled": False}
        try:
            lease = await self._acquire()
        except BaseException:
            await self._breaker.abort()
            raise
        self.attempts += 1
        try:
            yield outcome
        except asyncio.CancelledError:
            await self._breaker.abort()
            outcome["settled"] = True
            raise
        finally:
            try:
                await self._release(lease)
            finally:
                if not outcome["settled"]:
                    await self._settle(outcome["status"])

    async def get(self, path: str, *, params=None, headers=None) -> httpx.Response:
        async with self._guarded() as outcome:
            response = await self._http.get(path, params=params, headers=headers)
            outcome["status"] = response.status_code
            await self._reconcile(response)
            return response

    @asynccontextmanager
    async def stream(self, method: str, path: str, *, params=None, headers=None) -> AsyncIterator[httpx.Response]:
        if method != "GET":
            raise ValueError("GET_ONLY")
        async with self._guarded() as outcome:
            async with self._http.stream("GET", path, params=params, headers=headers) as response:
                outcome["status"] = response.status_code
                await self._reconcile(response)
                yield response


def _default_quota(settings: Settings, env: Mapping[str, str]):
    if not str(env.get("DATABASE_URL", "")).strip():
        raise ValueError(COORDINATOR_REQUIRED)
    return PostgresQuota(QuotaLimits(settings.max_rpm, settings.max_daily_requests, settings.max_concurrency))


@asynccontextmanager
async def coordinated_client(settings: Settings, env: Mapping[str, str], *, transport=None,
                             quota=None, breaker: CircuitBreaker | None = None) -> AsyncIterator[CoordinatedClient]:
    """Build the pinned provider client behind the coordinator; fail closed without one.

    ``quota`` is injectable for offline tests only; production paths pass ``None`` and get
    the shared :class:`PostgresQuota` on ``DATABASE_URL``.
    """
    if settings.base_url != OFFICIAL_BASE_URL:
        raise ValueError("UNTRUSTED_API_ORIGIN")
    coordinator = quota if quota is not None else _default_quota(settings, env)
    async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, timeout=settings.timeout_seconds,
                                 follow_redirects=False, trust_env=False, transport=transport) as http:
        yield CoordinatedClient(http, settings, coordinator, breaker or CircuitBreaker(threshold=2, cooldown=30))
