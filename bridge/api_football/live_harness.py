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
_ORPHAN_WAIT_SECONDS = 30.0
_RELEASE_TIMEOUT_SECONDS = 10.0


class CoordinatorRefused(RuntimeError):
    """A fixed, public category. ``sent`` tells whether the provider request went out.

    ``sent=False``: refused BEFORE sending (quota, concurrency, breaker, coordinator).
    ``sent=True``: the request was sent but its accounting failed afterwards (lease release
    or provider-zero write-back); the evidence must not claim it was never sent.
    """

    def __init__(self, code: str, *, sent: bool = False):
        super().__init__(code)
        self.code = code
        self.sent = sent


def _refused(code: str, *, sent: bool = False) -> CoordinatorRefused:
    # Built outside any ``except`` block by the callers, so neither __cause__ nor
    # __context__ carries the original exception (DSN, SQL, key) along.
    return CoordinatorRefused(code, sent=sent)


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
        self.orphaned_units = 0  # units consumed by a reservation we timed out on (never sent)

    async def _acquire(self):
        prepare = getattr(self._quota, "prepare", None)
        code = None
        if prepare is not None:
            try:
                await asyncio.wait_for(prepare(), timeout=_LEASE_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                code = "COORDINATOR_UNAVAILABLE"
        if code is not None:
            raise _refused(code)
        # shield(): the coordinator runs in a worker thread that cannot be cancelled and may
        # still commit a lease after we stop waiting (same convention as
        # ResilientFootballClient._lease). That late lease is released below, never leaked.
        reservation = asyncio.ensure_future(self._quota.acquire(self._settings.api_key))
        try:
            return await asyncio.wait_for(asyncio.shield(reservation), timeout=_LEASE_TIMEOUT_SECONDS)
        except QuotaExceeded:
            code = "QUOTA_EXCEEDED"
        except QuotaConcurrencyBusy:
            code = "QUOTA_CONCURRENCY_BUSY"
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            await self._compensate(reservation)
            if isinstance(exc, asyncio.CancelledError):
                raise
            code = "COORDINATOR_UNAVAILABLE"
        except Exception:
            # DSN, psycopg detail and the API key never leave this module.
            code = "COORDINATOR_UNAVAILABLE"
        raise _refused(code)

    async def _compensate(self, reservation: asyncio.Future) -> None:
        """Wait (bounded) for a reservation we gave up on and return its lease.

        The quota unit stays consumed (conservative) and is counted in ``orphaned_units``;
        only the concurrency lease is returned. If even the bounded wait expires, the lease
        falls back to the 180 s expiry enforced by PostgreSQL.
        """
        try:
            lease = await asyncio.wait_for(asyncio.shield(reservation), timeout=_ORPHAN_WAIT_SECONDS)
        except asyncio.CancelledError:
            # Never swallow a cancellation: hand the late lease to a best-effort callback
            # (180 s PostgreSQL expiry if the loop ends first) and propagate.
            reservation.add_done_callback(self._release_later)
            raise
        except BaseException:
            return  # still pending after the bound (or failed): 180 s expiry applies
        self.orphaned_units += 1
        try:
            await asyncio.wait_for(asyncio.shield(self._quota.release(lease)), timeout=_RELEASE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except BaseException:
            pass  # bounded by the 180 s expiry

    def _release_later(self, reservation: asyncio.Future) -> None:
        if reservation.cancelled() or reservation.exception() is not None:
            return
        self.orphaned_units += 1
        task = asyncio.ensure_future(self._quota.release(reservation.result()))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # consume, never log text

    async def _release(self, lease) -> None:
        failed = False
        try:
            await asyncio.wait_for(asyncio.shield(self._quota.release(lease)), timeout=_RELEASE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True  # incl. timeout: the lease expires server-side; the failure stays visible
        if failed:
            raise _refused("QUOTA_RELEASE_FAILED", sent=True)

    async def _reconcile(self, response: httpx.Response) -> None:
        for header, kind in (("x-ratelimit-requests-remaining", "day"), ("x-ratelimit-remaining", "minute")):
            if _remaining(response.headers, header) == 0:
                failed = False
                try:
                    await self._quota.mark_exhausted(self._settings.api_key, kind)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed = True
                if failed:
                    raise _refused("QUOTA_RECONCILIATION_FAILED", sent=True)

    async def _settle(self, status: int | None) -> None:
        if status is None or status == 429 or status >= 500:
            await self._breaker.fail()
        else:
            await self._breaker.success()

    @asynccontextmanager
    async def _guarded(self) -> AsyncIterator[dict]:
        try:
            await self._breaker.before_request()
            open_ = False
        except CircuitOpen:
            open_ = True
        if open_:
            raise _refused("CIRCUIT_OPEN")
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
            outcome["settled"] = outcome["cancelled"] = True
            raise
        except httpx.TransportError:
            # Headers may have arrived, but reading the body failed: a provider failure.
            outcome["status"] = None
            raise
        finally:
            try:
                await self._release(lease)
            except CoordinatorRefused:
                # A release failure must not replace a propagating cancellation.
                if not outcome.get("cancelled"):
                    raise
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
