"""Isolated API-FOOTBALL v3 client. Every failure becomes a structured result.

Order per request: enabled/config/key -> validation -> cache -> in-flight dedup
-> concurrency slot -> per attempt [circuit breaker, provider quota reserve,
provider minute block, local daily budget, local per-minute limiter] -> HTTP.
Retries are bounded (APIFOOTBALL_MAX_RETRIES) and only for timeouts, network
errors, HTTP 429 and 5xx. No call is ever made while the module is disabled.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from bridge.api_football import validators
from bridge.api_football.registry import Endpoint
from bridge.api_football.config import Settings, load_settings
from bridge.api_football.resilience import (
    CircuitBreaker, DailyBudget, QuotaState, SlidingWindowLimiter, TTLCache,
)

LOG = logging.getLogger(__name__)
PROVIDER = "API-FOOTBALL"
AUTH_COOLDOWN_SECONDS = 300.0
MAX_RETRY_AFTER_SECONDS = 10.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Failure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, http_status: int | None = None,
                 retry_after: float | None = None, breaker: bool = False) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.http_status, self.retry_after, self.breaker = http_status, retry_after, breaker


class ApiFootballClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 settings_loader: Callable[[], Settings] = load_settings,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self._transport = transport
        self._settings_loader = settings_loader
        self._sleep = sleep
        self.limiter = SlidingWindowLimiter(clock)
        self.budget = DailyBudget()
        self.breaker = CircuitBreaker(clock=clock)
        self.cache = TTLCache(clock=clock)
        self.quota = QuotaState(clock)
        self.metrics: Counter[str] = Counter()
        self._inflight: dict[Any, asyncio.Future] = {}
        self._semaphores: dict[int, asyncio.Semaphore] = {}

    # ------------------------------------------------------------------ public

    def settings(self) -> Settings:
        return self._settings_loader()

    async def call(self, endpoint: Endpoint, arguments: dict[str, Any],
                   use_cache: bool = True) -> dict[str, Any]:
        started = _utc_now()
        self.metrics["calls"] += 1
        settings = self.settings()
        base = self._base(endpoint, started)
        if not settings.enabled:
            return self._error(base, "DISABLED", "API-FOOTBALL module is disabled (APIFOOTBALL_ENABLED)")
        if settings.errors:
            return self._error(base, "CONFIG_INVALID", "; ".join(settings.errors))
        if not settings.key_configured:
            return self._error(base, "API_KEY_MISSING", "APIFOOTBALL_API_KEY is not configured server-side")
        try:
            params = validators.validate(endpoint.params, arguments, endpoint.require_any)
        except validators.InvalidArgument as exc:
            return self._error(base, "INVALID_ARGUMENT", str(exc))
        base["parameters"] = params

        key = (endpoint.path, tuple(params.items()))
        if settings.cache_enabled and use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                result, age = cached
                self.metrics["cache_hits"] += 1
                return self._finish(dict(result, request_started_at_utc=started,
                                         cache={"status": "HIT", "age_seconds": round(age, 3),
                                                "ttl_seconds": endpoint.cache_ttl}), settings)

        pending = self._inflight.get(key)
        if pending is not None:
            self.metrics["deduplicated"] += 1
            result = await asyncio.shield(pending)
            return self._finish(dict(result, request_started_at_utc=started,
                                     cache=dict(result["cache"], status="DEDUPLICATED")), settings)

        task = asyncio.ensure_future(self._fetch_bounded(endpoint, params, base, settings, key))
        self._inflight[key] = task
        task.add_done_callback(lambda _task: self._inflight.pop(key, None))
        # Shield: a cancelled MCP call must not cancel the request other callers share.
        return self._finish(await asyncio.shield(task), settings)

    def health(self) -> dict[str, Any]:
        settings = self.settings()
        if not settings.enabled:
            state = "DISABLED"
        elif settings.errors:
            state = "CONFIG_INVALID"
        elif not settings.key_configured:
            state = "API_KEY_MISSING"
        elif self.breaker.state != "CLOSED":
            state = "DEGRADED"
        elif self.quota.reserve_reached(settings.quota_reserve_percent):
            state = "QUOTA_RESERVE_REACHED"
        else:
            state = "READY"
        return {
            "provider": PROVIDER,
            "state": state,
            "network_call_made": False,
            "config": settings.public(),
            "circuit_breaker": self.breaker.snapshot(),
            "process": {"pid": os.getpid(), "web_concurrency_env": os.environ.get("WEB_CONCURRENCY"),
                        "limits_scope": "PER_PROCESS"},
            "checked_at_utc": _utc_now(),
        }

    def quota_report(self) -> dict[str, Any]:
        settings = self.settings()
        return {
            "provider": PROVIDER,
            "network_call_made": False,
            "local": {
                "scope": "PER_PROCESS",
                "utc_day": self.budget.day,
                "http_attempts_today": self.budget.used,
                "max_daily_requests": settings.max_daily_requests,
                "requests_last_60s": self.limiter.used(),
                "max_rpm": settings.max_rpm,
                "max_concurrency": settings.max_concurrency,
                "cache_entries": len(self.cache),
                "cache_bytes": self.cache.total_bytes,
                "in_flight": len(self._inflight),
            },
            "provider_reported": self.quota.snapshot(),
            "quota_reserve_percent": settings.quota_reserve_percent,
            "reserve_reached": self.quota.reserve_reached(settings.quota_reserve_percent),
            "circuit_breaker": self.breaker.snapshot(),
            "metrics": dict(self.metrics),
            "checked_at_utc": _utc_now(),
        }

    # ---------------------------------------------------------------- internals

    def _base(self, endpoint: Endpoint, started: str) -> dict[str, Any]:
        return {
            "provider": PROVIDER,
            "api_version": "v3",
            "endpoint": endpoint.path,
            "tool": endpoint.tool_name,
            "ok": False,
            "status": None,
            "parameters": {},
            "request_started_at_utc": started,
            "acquired_at_utc": None,
            "provider_timestamp_utc": None,
            "provider_date_header": None,
            "http_status": None,
            "attempts": 0,
            "latency_ms": None,
            "cache": {"status": "BYPASS", "age_seconds": None, "ttl_seconds": endpoint.cache_ttl},
            "quota": None,
            "results": None,
            "paging": None,
            "provider_errors": None,
            "raw": None,
            "raw_sha256": None,
            "bytes_utf8": None,
            "data_class": endpoint.data_class,
            "registry_id": endpoint.id,
            "contract_status": endpoint.contract_status,
            "parameter_contract": endpoint.parameter_status,
            "client_validation": "PROVISIONAL_CLIENT_POLICY",
            "pit_status": "NOT_PIT_CERTIFIED",
            "warnings": list(endpoint.warnings),
            "error": None,
        }

    def _error(self, base: dict[str, Any], code: str, message: str, *, retryable: bool = False,
               retry_after: float | None = None) -> dict[str, Any]:
        self.metrics["error:" + code] += 1
        base.update(ok=False, status=code, error={
            "code": code, "message": message, "retryable": retryable,
            "retry_after_seconds": None if retry_after is None else round(retry_after, 1),
        })
        base["quota"] = self.quota.snapshot()
        return base

    def _finish(self, result: dict[str, Any], settings: Settings) -> dict[str, Any]:
        result = dict(result, quota=self.quota.snapshot())
        text = json.dumps(result, ensure_ascii=False, default=str)
        if settings.api_key and settings.api_key in text:
            # Defence in depth: the key is only ever sent as a request header.
            LOG.error("API-FOOTBALL result contained the configured key; redacted")
            result = json.loads(text.replace(settings.api_key, "[REDACTED]"))
            text = json.dumps(result, ensure_ascii=False)
        if result.get("ok") and len(text.encode("utf-8")) > settings.max_output_bytes:
            size = result.get("bytes_utf8")
            result = dict(result, ok=False, status="RESPONSE_TOO_LARGE", raw=None, error={
                "code": "RESPONSE_TOO_LARGE",
                "message": (f"provider payload of {size} bytes exceeds APIFOOTBALL_MAX_OUTPUT_BYTES="
                            f"{settings.max_output_bytes}; narrow filters or use page"),
                "retryable": False, "retry_after_seconds": None,
            })
        return result

    def _semaphore(self, size: int) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(size)
        if semaphore is None:
            semaphore = self._semaphores[size] = asyncio.Semaphore(size)
        return semaphore

    @staticmethod
    def _deadline(settings: Settings) -> float:
        """Hard upper bound for one call, including queueing, retries and backoff."""
        return settings.queue_timeout_seconds + (settings.timeout_seconds + MAX_RETRY_AFTER_SECONDS) \
            * (settings.max_retries + 1) + 5

    async def _fetch_bounded(self, endpoint, params, base, settings: Settings, key) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self._deadline(settings)):
                semaphore = self._semaphore(settings.max_concurrency)
                try:
                    async with asyncio.timeout(settings.queue_timeout_seconds):
                        await semaphore.acquire()
                except TimeoutError:
                    return self._error(base, "QUEUE_TIMEOUT", "all API-FOOTBALL concurrency slots are busy",
                                       retryable=True)
                try:
                    return await self._fetch(endpoint, params, base, settings, key)
                finally:
                    semaphore.release()
        except TimeoutError:
            self.breaker.probe_in_flight = False
            return self._error(base, "TIMEOUT", "API-FOOTBALL request exceeded its overall deadline",
                               retryable=True)
        except Exception as exc:
            self.breaker.probe_in_flight = False
            LOG.error("API-FOOTBALL internal failure: endpoint=%s error=%s", endpoint.path, type(exc).__name__)
            return self._error(base, "INTERNAL_ERROR", type(exc).__name__)

    async def _gate(self, endpoint, base, settings: Settings) -> dict[str, Any] | None:
        if self.quota.reserve_reached(settings.quota_reserve_percent):
            return self._error(base, "QUOTA_RESERVE_REACHED",
                               "provider-reported daily quota is at or below the configured reserve")
        if self.quota.minute_blocked():
            return self._error(base, "RATE_LIMITED_PROVIDER", "provider reported no remaining per-minute requests",
                               retryable=True, retry_after=60.0)
        if not self.budget.available(settings.max_daily_requests):
            return self._error(base, "DAILY_BUDGET_EXHAUSTED", "local APIFOOTBALL_MAX_DAILY_REQUESTS reached")
        if not await self.limiter.acquire(settings.max_rpm, settings.queue_timeout_seconds):
            return self._error(base, "RATE_LIMITED_LOCAL", "local APIFOOTBALL_MAX_RPM reached", retryable=True,
                               retry_after=60.0)
        # Checked last: a HALF_OPEN probe slot is only taken when the HTTP call follows.
        if settings.circuit_breaker_enabled and not self.breaker.allow():
            return self._error(base, "CIRCUIT_OPEN", "API-FOOTBALL circuit breaker is open",
                               retryable=True, retry_after=self.breaker.retry_after())
        return None

    async def _fetch(self, endpoint, params, base, settings: Settings, key) -> dict[str, Any]:
        failure: _Failure | None = None
        for attempt in range(settings.max_retries + 1):
            if attempt:
                delay = min(0.5 * 2 ** (attempt - 1), 4.0) + random.uniform(0, 0.25)
                if failure is not None and failure.retry_after is not None:
                    delay = max(delay, min(failure.retry_after, MAX_RETRY_AFTER_SECONDS))
                await self._sleep(delay)
            gated = await self._gate(endpoint, base, settings)
            if gated is not None:
                gated["attempts"] = attempt
                return gated
            self.budget.consume()
            self.metrics["http_attempts"] += 1
            base["attempts"] = attempt + 1
            try:
                result = await self._attempt(endpoint, params, base, settings)
            except _Failure as exc:
                failure = exc
                base["http_status"] = exc.http_status
                if settings.circuit_breaker_enabled:
                    if exc.code == "AUTH_FAILED":
                        self.breaker.trip(exc.code, AUTH_COOLDOWN_SECONDS)
                    elif exc.breaker:
                        self.breaker.failure(exc.code)
                    else:
                        # The provider answered coherently; it is reachable.
                        self.breaker.success()
                LOG.warning("API-FOOTBALL failure: endpoint=%s code=%s http=%s attempt=%s",
                            endpoint.path, exc.code, exc.http_status, attempt + 1)
                if exc.retryable and attempt < settings.max_retries:
                    continue
                return self._error(base, exc.code, exc.message, retryable=exc.retryable,
                                   retry_after=exc.retry_after)
            if settings.circuit_breaker_enabled:
                self.breaker.success()
            if settings.cache_enabled:
                self.cache.put(key, result, endpoint.cache_ttl, result["bytes_utf8"])
            return result
        raise AssertionError("unreachable")

    async def _attempt(self, endpoint, params, base, settings: Settings) -> dict[str, Any]:
        headers = {"x-apisports-key": settings.api_key, "Accept": "application/json"}
        timeout = httpx.Timeout(settings.timeout_seconds)
        begin = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport,
                                         follow_redirects=False) as client:
                async with client.stream("GET", settings.base_url + endpoint.path,
                                         params=params, headers=headers) as response:
                    self.quota.update(response.headers)
                    length = response.headers.get("content-length")
                    if length and length.isdigit() and int(length) > settings.max_response_bytes:
                        raise _Failure("RESPONSE_TOO_LARGE", "provider response exceeds APIFOOTBALL_MAX_RESPONSE_BYTES",
                                       http_status=response.status_code)
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > settings.max_response_bytes:
                            raise _Failure("RESPONSE_TOO_LARGE",
                                           "provider response exceeds APIFOOTBALL_MAX_RESPONSE_BYTES",
                                           http_status=response.status_code)
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    status = response.status_code
                    date_header = response.headers.get("date")
        except httpx.TimeoutException:
            raise _Failure("TIMEOUT", "API-FOOTBALL did not answer within APIFOOTBALL_TIMEOUT_SECONDS",
                           retryable=True, breaker=True) from None
        except httpx.HTTPError as exc:
            # Exception text may include the request URL; only the type is reported.
            raise _Failure("NETWORK_ERROR", f"API-FOOTBALL unreachable ({type(exc).__name__})",
                           retryable=True, breaker=True) from None

        base["latency_ms"] = round((time.monotonic() - begin) * 1000, 1)
        base["http_status"] = status
        base["provider_date_header"] = date_header
        if status in (401, 403):
            raise _Failure("AUTH_FAILED", f"API-FOOTBALL rejected the configured key (HTTP {status})",
                           http_status=status)
        if status == 429:
            raise _Failure("RATE_LIMITED_PROVIDER", "API-FOOTBALL returned HTTP 429", retryable=True,
                           http_status=status, breaker=True, retry_after=60.0)
        if status >= 500:
            raise _Failure("UPSTREAM_ERROR", f"API-FOOTBALL returned HTTP {status}", retryable=True,
                           http_status=status, breaker=True)
        if status != 200:
            raise _Failure("HTTP_ERROR", f"API-FOOTBALL returned HTTP {status}", http_status=status)

        def reject_constant(_value):
            raise ValueError("non-finite JSON constant")

        try:
            envelope = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        except (UnicodeDecodeError, ValueError):
            raise _Failure("INVALID_JSON", "API-FOOTBALL returned a body that is not valid JSON",
                           http_status=status, breaker=True) from None
        if not isinstance(envelope, dict) or "response" not in envelope:
            raise _Failure("INCOMPLETE_RESPONSE", "API-FOOTBALL envelope has no 'response' field",
                           http_status=status, breaker=True)

        acquired = _utc_now()
        provider_errors = envelope.get("errors")
        base.update(acquired_at_utc=acquired, raw_sha256=hashlib.sha256(raw).hexdigest(), bytes_utf8=len(raw),
                    provider_errors=provider_errors, results=envelope.get("results"),
                    paging=envelope.get("paging"))
        if provider_errors:
            self._raise_provider_error(provider_errors)

        warnings = base["warnings"]
        response = envelope["response"]
        if response in ([], {}, None):
            warnings.append("EMPTY_RESPONSE: no rows returned; absence of rows is not proof of no coverage.")
        warnings.extend(self._paging_warnings(endpoint, params, envelope))
        base.update(ok=True, status="EMPTY" if response in ([], {}, None) else "OK", raw=envelope)
        return dict(base, cache={"status": "MISS", "age_seconds": 0.0, "ttl_seconds": endpoint.cache_ttl})

    def _raise_provider_error(self, errors) -> None:
        if isinstance(errors, dict):
            keys = {str(name).lower() for name in errors}
        else:
            keys = set()
            for item in errors if isinstance(errors, list) else [errors]:
                if isinstance(item, dict):
                    keys.update(str(name).lower() for name in item)
                else:
                    keys.add(str(item).lower())
        # Error keys are ASSUMED from documentation and stay visible in provider_errors.
        if keys & {"token", "key", "access"}:
            raise _Failure("AUTH_FAILED", "API-FOOTBALL rejected the configured key", http_status=200)
        if "requests" in keys:
            self.quota.daily_exhausted_day = datetime.now(timezone.utc).date().isoformat()
            raise _Failure("QUOTA_EXHAUSTED", "API-FOOTBALL reports the daily request quota is exhausted",
                           http_status=200)
        if "ratelimit" in keys:
            raise _Failure("RATE_LIMITED_PROVIDER", "API-FOOTBALL reports the per-minute limit was exceeded",
                           http_status=200, breaker=True, retry_after=60.0)
        if "plan" in keys:
            raise _Failure("PLAN_RESTRICTED", "API-FOOTBALL reports this request is not available on the plan",
                           http_status=200)
        raise _Failure("PROVIDER_ERROR", "API-FOOTBALL returned errors; see provider_errors", http_status=200)

    @staticmethod
    def _paging_warnings(endpoint, params, envelope) -> list[str]:
        paging = envelope.get("paging")
        if not isinstance(paging, dict):
            return ["PAGING_ABSENT: provider envelope has no paging object."]
        current, total = paging.get("current"), paging.get("total")
        if type(current) is not int or type(total) is not int:
            return ["PAGINATION_UNEXPECTED: paging.current/total are not integers."]
        warnings = []
        if total and current > total:
            warnings.append("PAGINATION_UNEXPECTED: paging.current exceeds paging.total.")
        requested = params.get("page")
        if requested is not None and int(requested) != current:
            warnings.append("PAGINATION_UNEXPECTED: provider returned a different page than requested.")
        if total > current:
            warnings.append(f"MORE_PAGES: page {current} of {total}; request the next page explicitly.")
        results = envelope.get("results")
        if endpoint.page_size and type(results) is int and results > endpoint.page_size:
            warnings.append("PAGINATION_UNEXPECTED: more results than the documented page size.")
        return warnings
