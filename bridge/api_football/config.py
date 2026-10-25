"""Fail-closed configuration for the optional football-only research adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
import os
from urllib.parse import urlsplit

OFFICIAL_BASE_URL = "https://v3.football.api-sports.io"


class ConfigurationError(ValueError):
    """A public, sanitized configuration error (never includes secret values)."""


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name, "true" if default else "false").strip().lower()
    if value not in ("true", "false", "1", "0"):
        raise ConfigurationError(f"{name}: expected true/false/1/0")
    return value in ("true", "1")


def _integer(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(env.get(name, str(default)).strip())
    except (ValueError, TypeError):
        raise ConfigurationError(f"{name}: expected integer") from None
    if not low <= value <= high:
        raise ConfigurationError(f"{name}: outside permitted range")
    return value


def _timeout(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get("APIFOOTBALL_TIMEOUT_SECONDS", "8").strip())
    except (TypeError, ValueError):
        raise ConfigurationError("APIFOOTBALL_TIMEOUT_SECONDS: expected number") from None
    if not (0.5 <= value <= 30):
        raise ConfigurationError("APIFOOTBALL_TIMEOUT_SECONDS: outside permitted range")
    return value


def _base_url(env: Mapping[str, str]) -> str:
    value = env.get("APIFOOTBALL_BASE_URL", OFFICIAL_BASE_URL).strip()
    parts = urlsplit(value)
    # Exactly one audited destination.  No alternative hosts, IPs, ports,
    # paths, redirects, arbitrary URLs or userinfo at this stage.
    if (
        value != OFFICIAL_BASE_URL
        or parts.scheme != "https"
        or parts.netloc != "v3.football.api-sports.io"
        or parts.username or parts.password or parts.path or parts.query or parts.fragment
    ):
        raise ConfigurationError("APIFOOTBALL_BASE_URL: unsupported provider origin")
    return OFFICIAL_BASE_URL


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    api_key: str = field(default="", repr=False, compare=False)
    base_url: str = OFFICIAL_BASE_URL
    timeout_seconds: float = 8.0
    max_rpm: int = 8
    max_daily_requests: int = 80
    max_concurrency: int = 2
    cache_enabled: bool = True
    circuit_breaker_enabled: bool = True
    # Independent server-side authorization; never enabled by API key presence.
    live_authorized: bool = False

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    @property
    def real_requests_authorized(self) -> bool:
        return self.configured and self.live_authorized

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        if not _flag(e, "APIFOOTBALL_ENABLED"):
            # Disabling the optional component must work even if unrelated
            # APIFOOTBALL_* settings are invalid or the key is absent.
            return cls()
        return cls(
            enabled=True,
            api_key=e.get("APIFOOTBALL_API_KEY", "").strip(),
            base_url=_base_url(e),
            timeout_seconds=_timeout(e),
            max_rpm=_integer(e, "APIFOOTBALL_MAX_RPM", 8, 1, 10000),
            max_daily_requests=_integer(e, "APIFOOTBALL_MAX_DAILY_REQUESTS", 80, 1, 1000000),
            max_concurrency=_integer(e, "APIFOOTBALL_MAX_CONCURRENCY", 2, 1, 32),
            cache_enabled=_flag(e, "APIFOOTBALL_CACHE_ENABLED", default=True),
            circuit_breaker_enabled=_flag(e, "APIFOOTBALL_CIRCUIT_BREAKER_ENABLED", default=True),
            live_authorized=_flag(e, "APIFOOTBALL_LIVE_AUTHORIZED", default=False),
        )
