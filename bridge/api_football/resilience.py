"""Opt-in, fail-closed orchestration for a *future* football-only transport.

PR-02 registers NO new production endpoint tools and performs NO provider
traffic. Transport is always injected. The existing bridge doesn't instantiate
this class. All network tests use httpx.MockTransport / deterministic fakes.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import random
from typing import Any, Mapping, Protocol

from .cache import BoundedCache, ttl_for_endpoint
from .circuit import CircuitBreaker
from .config import Settings
from .errors import (
    CircuitOpen, ProviderUnavailable, ProviderHTTPError, ProviderDataError,
    RequestQueueFull,
)
from .quota import PostgresQuota, QuotaExceeded, QuotaConcurrencyBusy, QuotaLease, QuotaLimits
from .transport import ProviderReply, validated_request


class ProviderTransport(Protocol):
    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> ProviderReply:
        ...


class QuotaProtocol(Protocol):
    async def acquire(self, secret: str) -> QuotaLease:
        ...
    async def release(self, lease: QuotaLease) -> None:
        ...
    async def mark_exhausted(self, secret: str, kind: str) -> None:
        ...


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.1
    max_delay: float = 0.75

    def __post_init__(self):
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("No more than 3 attempts permitted")
        if not 0.0 <= self.base_delay <= self.max_delay <= 2.0:
            raise ValueError("Invalid retry policy")


class ResilientFootballClient:
    """Bounded research client with one reserved quota unit per real attempt.

    Shared Postgres quota enforces global per-key buckets and outstanding leases
    across workers; per-worker cache/breaker/queue are just a second boundary.
    This class is inert until explicitly constructed by trusted future code.
    """

    def __init__(
        self, settings: Settings, transport: ProviderTransport, *,
        quota: QuotaProtocol | None = None,
        cache: BoundedCache | None = None,
        breaker: CircuitBreaker | None = None,
        retry: RetryPolicy | None = None,
    ):
        if not settings.configured:
            raise ProviderUnavailable("PROVIDER_NOT_CONFIGURED", retryable=False)
        self.settings = settings
        self.transport = transport
        self.quota = quota or PostgresQuota(
            QuotaLimits(settings.max_rpm, settings.max_daily_requests, settings.max_concurrency)
        )
        self.cache = cache or BoundedCache()
        self.breaker = breaker or CircuitBreaker(threshold=2, cooldown=30)
        self.retry = retry or RetryPolicy()
        self._slots = asyncio.Semaphore(settings.max_concurrency)
        # No unbounded queue; maintain <= 4x concurrency active+queued.
        self._queue = asyncio.Semaphore(settings.max_concurrency * 4)
        self._flights_lock = asyncio.Lock()
        self._flights: dict[tuple, asyncio.Task] = {}

    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        path, checked = validated_request(path, params)
        # Type-safe cache keys: string "12" is different from integer 12.
        key = (path, tuple(sorted((k, type(v).__name__, v) for k, v in checked.items())))
        if self.settings.cache_enabled:
            hit = self.cache.get(key)
            if hit is not None:
                hit["cache_status"] = "HIT"
                return hit

        async with self._flights_lock:
            # Recheck under lock to avoid races between dispatches.
            hit = self.cache.get(key) if self.settings.cache_enabled else None
            if hit is not None:
                hit["cache_status"] = "HIT"
                return hit
            task = self._flights.get(key)
            if task is None:
                # Bound new per-key tasks BEFORE create_task(). The downstream
                # semaphore alone only bounds active jobs; without this guard
                # thousands of unique callers could allocate unbounded tasks
                # while waiting to acquire the local queue.
                if len(self._flights) >= self.settings.max_concurrency * 4:
                    raise RequestQueueFull()
                task = asyncio.create_task(self._execute(path, checked, key))
                self._flights[key] = task
                task.add_done_callback(lambda t, k=key: self._flight_completed(k, t))
        # A cancelled caller does not cancel shared in-flight work;
        # it always terminates within policy bounds.
        return deepcopy(await asyncio.shield(task))

    def _flight_completed(self, key: tuple, task: asyncio.Task) -> None:
        if self._flights.get(key) is task:
            del self._flights[key]
        # Consume exception even if all waiters were cancelled.
        if not task.cancelled():
            task.exception()

    @asynccontextmanager
    async def _queued_slot(self):
        try:
            await asyncio.wait_for(self._queue.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            raise RequestQueueFull() from None
        try:
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=5.0)
            except asyncio.TimeoutError:
                raise RequestQueueFull() from None
            try:
                yield
            finally:
                self._slots.release()
        finally:
            self._queue.release()

    async def _lease(self) -> QuotaLease:
        try:
            return await asyncio.wait_for(self.quota.acquire(self.settings.api_key), timeout=3.0)
        except (QuotaExceeded, QuotaConcurrencyBusy):
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # DSN, psycopg details and API key are never sent to the MCP caller.
            raise ProviderUnavailable("QUOTA_COORDINATOR_UNAVAILABLE", retryable=False) from None

    async def _release(self, lease: QuotaLease) -> None:
        try:
            # If the caller is cancelled, the release continues or the lease
            # expires in PostgreSQL after 180 seconds (fail-closed).
            await asyncio.shield(self.quota.release(lease))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderUnavailable("QUOTA_RELEASE_FAILED", retryable=False) from None

    async def _mark_exhausted(self, kind: str) -> None:
        try:
            await self.quota.mark_exhausted(self.settings.api_key, kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderUnavailable("QUOTA_RECONCILIATION_FAILED", retryable=False) from None

    async def _attempt(self, path: str, params: Mapping[str, str | int]) -> ProviderReply:
        lease = await self._lease()
        try:
            try:
                reply = await asyncio.wait_for(
                    self.transport.get(path, params), timeout=self.settings.timeout_seconds
                )
            except asyncio.TimeoutError:
                raise ProviderUnavailable("PROVIDER_TIMEOUT") from None
        finally:
            await self._release(lease)
        if not isinstance(reply, ProviderReply):
            raise ProviderDataError("PROVIDER_REPLY_CONTRACT_INVALID")
        return reply

    async def _execute(self, path: str, checked: dict[str, str | int], key: tuple) -> dict[str, Any]:
        began = False
        try:
            if self.settings.circuit_breaker_enabled:
                await self.breaker.before_request()
                began = True
            async with self._queued_slot():
                last_error: Exception | None = None
                for i in range(self.retry.max_attempts):
                    try:
                        reply = await self._attempt(path, checked)
                        # A provider reply reporting zero overrides our local
                        # allowance globally for the corresponding UTC bucket.
                        if reply.quota_remaining_day == 0:
                            await self._mark_exhausted("day")
                        if reply.quota_remaining_minute == 0:
                            await self._mark_exhausted("minute")
                        outcome = {
                            "provider": "API-FOOTBALL",
                            "endpoint": path,
                            "params": deepcopy(checked),
                            "status": "MOCK_TRANSPORT_ONLY",
                            "http_status": reply.status_code,
                            "retrieved_at_utc": reply.received_at_utc,
                            "provider_timestamp_utc": None,
                            "cache_status": "MISS",
                            "quota_remaining_provider_day": reply.quota_remaining_day,
                            "quota_remaining_provider_minute": reply.quota_remaining_minute,
                            "data": deepcopy(reply.data),
                            "warnings": ["No real provider data verified in PR-02"],
                        }
                        if self.settings.cache_enabled:
                            self.cache.put(key, outcome, ttl_for_endpoint(path))
                        if began:
                            await self.breaker.success()
                        return outcome
                    except ProviderHTTPError as exc:
                        if exc.status == 429:
                            await self._mark_exhausted("minute")
                        last_error = exc
                        if not exc.retryable:
                            break
                    except ProviderUnavailable as exc:
                        last_error = exc
                        if not exc.retryable:
                            break
                    except (ProviderDataError, QuotaExceeded, QuotaConcurrencyBusy):
                        raise
                    if i < self.retry.max_attempts - 1:
                        delay = min(
                            self.retry.max_delay, self.retry.base_delay * (2**i)
                        )
                        if delay:
                            await asyncio.sleep(random.uniform(0, delay))
                if last_error is not None:
                    raise last_error
                raise ProviderUnavailable("PROVIDER_NO_REPLY", retryable=False)
        except asyncio.CancelledError:
            if began:
                await self.breaker.abort()
            raise
        except (QuotaExceeded, QuotaConcurrencyBusy, RequestQueueFull, CircuitOpen):
            if began:
                await self.breaker.abort()
            raise
        except (ProviderUnavailable, ProviderHTTPError, ProviderDataError):
            if began:
                await self.breaker.fail()
            raise
        except Exception:
            if began:
                await self.breaker.fail()
            # An injected transport, cache or quota adapter may raise an
            # unexpected exception whose message contains internal URLs,
            # connection strings or even secrets. Never return it through a
            # future MCP tool. The stable code retains fail-closed behavior.
            raise ProviderUnavailable("PROVIDER_INTERNAL_ERROR", retryable=False) from None
