"""Server-side API-FOOTBALL configuration. The API key never leaves this process."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Only the direct API-Sports host is supported. RapidAPI uses a different host
# and header scheme and is intentionally not accepted.
ALLOWED_BASE_URLS = frozenset({"https://v3.football.api-sports.io"})
DEFAULT_BASE_URL = "https://v3.football.api-sports.io"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    enabled: bool
    api_key: str = field(repr=False)
    base_url: str
    timeout_seconds: float
    max_rpm: int
    max_daily_requests: int
    max_concurrency: int
    cache_enabled: bool
    circuit_breaker_enabled: bool
    quota_reserve_percent: int
    max_retries: int
    queue_timeout_seconds: float
    max_response_bytes: int
    max_output_bytes: int
    errors: tuple[str, ...] = ()

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key)

    def public(self) -> dict:
        """Configuration safe to return through MCP: booleans and limits only."""
        return {
            "enabled": self.enabled,
            "api_key_configured": self.key_configured,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "max_rpm": self.max_rpm,
            "max_daily_requests": self.max_daily_requests,
            "max_concurrency": self.max_concurrency,
            "cache_enabled": self.cache_enabled,
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "quota_reserve_percent": self.quota_reserve_percent,
            "max_retries": self.max_retries,
            "queue_timeout_seconds": self.queue_timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "max_output_bytes": self.max_output_bytes,
            "config_errors": list(self.errors),
        }


def _bool(name: str, default: bool, errors: list[str]) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    errors.append(f"{name} must be a boolean")
    return default


def _number(name: str, default, low, high, errors: list[str], kind=int):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        errors.append(f"{name} must be a number")
        return default
    if not low <= value <= high:
        errors.append(f"{name} must be within {low}..{high}")
        return default
    return value


def load_settings() -> Settings:
    """Read configuration from the environment. Never raises and never does I/O.

    Defaults are deliberately prudent (roughly the provider's free tier with a
    margin) so an unconfigured plan cannot exhaust a key or trigger an IP block.
    """
    errors: list[str] = []
    base_url = os.environ.get("APIFOOTBALL_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
    if base_url not in ALLOWED_BASE_URLS:
        errors.append("APIFOOTBALL_BASE_URL is not an allowlisted API-FOOTBALL host")
        base_url = DEFAULT_BASE_URL
    return Settings(
        enabled=_bool("APIFOOTBALL_ENABLED", False, errors),
        api_key=os.environ.get("APIFOOTBALL_API_KEY", "").strip(),
        base_url=base_url,
        timeout_seconds=_number("APIFOOTBALL_TIMEOUT_SECONDS", 15.0, 1.0, 60.0, errors, float),
        max_rpm=_number("APIFOOTBALL_MAX_RPM", 8, 1, 1200, errors),
        max_daily_requests=_number("APIFOOTBALL_MAX_DAILY_REQUESTS", 75, 1, 1_500_000, errors),
        max_concurrency=_number("APIFOOTBALL_MAX_CONCURRENCY", 2, 1, 16, errors),
        cache_enabled=_bool("APIFOOTBALL_CACHE_ENABLED", True, errors),
        circuit_breaker_enabled=_bool("APIFOOTBALL_CIRCUIT_BREAKER_ENABLED", True, errors),
        quota_reserve_percent=_number("APIFOOTBALL_QUOTA_RESERVE_PERCENT", 10, 0, 90, errors),
        max_retries=_number("APIFOOTBALL_MAX_RETRIES", 1, 0, 3, errors),
        queue_timeout_seconds=_number("APIFOOTBALL_QUEUE_TIMEOUT_SECONDS", 10.0, 0.0, 60.0, errors, float),
        max_response_bytes=_number("APIFOOTBALL_MAX_RESPONSE_BYTES", 8 * 1024 * 1024, 1024, 32 * 1024 * 1024, errors),
        max_output_bytes=_number("APIFOOTBALL_MAX_OUTPUT_BYTES", 2_000_000, 1024, 16 * 1024 * 1024, errors),
        errors=tuple(errors),
    )
