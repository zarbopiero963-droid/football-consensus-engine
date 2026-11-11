"""Fixed-code, non-sensitive errors. Never embed provider bodies or credentials."""
from __future__ import annotations

from typing import Callable


class FootballError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ProviderUnavailable(FootballError):
    """Bounded HTTP transport error, optionally retryable."""

    def __init__(self, code: str, *, retryable: bool = True):
        self.retryable = retryable
        super().__init__(code)


class ProviderHTTPError(FootballError):
    def __init__(self, status: int, *, retry_after_seconds: int | None = None):
        self.status = status
        self.retryable = status in (500, 502, 503, 504)
        # Numeric Retry-After only (seconds), never the raw header text.
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"PROVIDER_HTTP_{status}")


class ProviderDataError(FootballError):
    """Response cannot be safely interpreted: no implicit defaults."""


class CircuitOpen(FootballError):
    def __init__(self):
        super().__init__("PROVIDER_CIRCUIT_OPEN")


class RequestQueueFull(FootballError):
    def __init__(self):
        super().__init__("PROVIDER_REQUEST_QUEUE_TIMEOUT")


# Keys of the provider's HTTP-200 `errors` object whose category rests on repository evidence:
#   requests, rateLimit: audit 2026-11-09 F-05 (docs/certification/audit/2026-11-09/areas/C_apifootball.md);
#   plan:  provider-shaped fixture in tests/test_api_football_capability_inventory.py;
#   token: provider-shaped fixtures in tests/test_apifootball_resilience.py and test_api_football_live_smoke.py.
# A key equal to a documented parameter of the called endpoint is INVALID_PARAMETER (ASSUMED: no
# captured provider reply shows one). Any other key is exposed as OTHER, with category UNKNOWN.
DECLARED_ERROR_CATEGORIES = {"plan": "PLAN_NOT_INCLUDED", "rateLimit": "RATE_LIMIT",
                             "requests": "DAILY_QUOTA", "token": "AUTH"}
_MAX_DECLARED_KEYS = 16


class ProviderDeclaredErrors(ProviderDataError):
    """HTTP 200 whose `errors` is not empty. Holds the KEYS only, never the provider's values,
    and the quota headers of the same reply so they are reconciled like a normal reply."""

    def __init__(self, keys: tuple[str, ...], *, quota_remaining_day: int | None = None,
                 quota_remaining_minute: int | None = None):
        self.error_keys = keys
        self.quota_remaining_day = quota_remaining_day
        self.quota_remaining_minute = quota_remaining_minute
        super().__init__("PROVIDER_DECLARED_ERRORS")


def declared_error_keys(errors: object, is_key: Callable[[str], bool]) -> tuple[str, ...]:
    """Sorted, de-duplicated keys of a provider `errors` object (a dict, or a list of dicts).
    A key that is not a plain identifier becomes OTHER; values are never read."""
    if isinstance(errors, dict):
        items = [errors]
    elif isinstance(errors, list):
        items = [e for e in errors if isinstance(e, dict)]
    else:
        items = []
    keys = {k if isinstance(k, str) and is_key(k) else "OTHER" for d in items for k in d}
    # The reserved keys (quota, rate limit, plan, auth) drive quota reconciliation and the
    # stop rules: they survive the bound whatever else the provider declared.
    reserved = sorted(keys & DECLARED_ERROR_CATEGORIES.keys())
    rest = sorted(keys - DECLARED_ERROR_CATEGORIES.keys())
    return tuple(sorted(reserved + rest[:_MAX_DECLARED_KEYS - len(reserved)]))


def classify_declared_errors(keys: tuple[str, ...], parameters: tuple[str, ...]) -> tuple[list[str], str]:
    """(exposed keys, category). Keys outside the evidence list and the endpoint's documented
    parameters are exposed as OTHER. A category is given only when every key agrees on it."""
    exposed = sorted({k if k in DECLARED_ERROR_CATEGORIES or k in parameters else "OTHER" for k in keys})
    categories = {DECLARED_ERROR_CATEGORIES.get(k, "INVALID_PARAMETER" if k in parameters else "UNKNOWN")
                  for k in exposed}
    return exposed, categories.pop() if len(categories) == 1 else "UNKNOWN"
