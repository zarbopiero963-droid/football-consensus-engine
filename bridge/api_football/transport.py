"""Pinned-host football API transport, injected client only in PR-02.

No HTTPX client is instantiated by this module. The MCP tool registry never
constructs this class. Unit tests inject httpx.MockTransport to avoid network.
A separately authorized future PR may wire it after live-key readiness.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping

import httpx

from .catalog import documented_path
from .config import OFFICIAL_BASE_URL
from .client import UnsupportedEndpoint
from .errors import (
    ProviderUnavailable, ProviderHTTPError, ProviderDataError, ProviderDeclaredErrors, declared_error_keys,
)

_PARAM = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_MAX_BODY = 524288
_MAX_ODDS_BODY = 2_097_152  # only /odds and /odds/live: bounded, still fail-closed
_LARGE_BODY_PATHS = frozenset({"/odds", "/odds/live"})
_MAX_PARAMS = 16


def validated_request(path: str, params: Mapping[str, str | int] | None) -> tuple[str, dict[str, str | int]]:
    if not documented_path(path):
        raise UnsupportedEndpoint("API-FOOTBALL path not allowed")
    if params is None:
        return path, {}
    if not isinstance(params, Mapping) or len(params) > _MAX_PARAMS:
        raise ValueError("Invalid API-FOOTBALL query parameters")
    out: dict[str, str | int] = {}
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM.fullmatch(key):
            raise ValueError("Invalid API-FOOTBALL parameter key")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Invalid API-FOOTBALL parameter value")
        if isinstance(value, str) and (
            len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("Invalid API-FOOTBALL parameter length or characters")
        if isinstance(value, int) and not (-2**31 <= value <= 2**31 - 1):
            raise ValueError("Invalid API-FOOTBALL numeric parameter")
        out[key] = value
    return path, out


@dataclass(frozen=True)
class ProviderReply:
    status_code: int
    data: dict[str, Any]
    received_at_utc: str
    bytes_received: int
    quota_remaining_day: int | None = None
    quota_remaining_minute: int | None = None
    # The provider's own HTTP `Date` header (RFC 9110 IMF-fixdate, GMT), normalised to UTC ISO
    # 8601; None when absent or not in that exact form. Never the bridge clock.
    provider_date_utc: str | None = None


_IMF_FIXDATE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT$")


def provider_http_date(headers: Mapping[str, str]) -> str | None:
    """The provider's `Date` header as UTC ISO 8601, or None. Only the strict IMF-fixdate form
    is accepted: an obsolete or malformed date is not guessed."""
    raw = headers.get("date", "")
    if not isinstance(raw, str) or not _IMF_FIXDATE.fullmatch(raw.strip()):
        return None
    try:
        parsed = datetime.strptime(raw.strip(), "%a, %d %b %Y %H:%M:%S GMT")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _quota_header(headers: httpx.Headers, key: str) -> int | None:
    raw = headers.get(key, "")
    if raw.isdigit() and len(raw) <= 12:
        return int(raw)
    return None


_MAX_RETRY_AFTER_SECONDS = 86_400


def _retry_after(headers: httpx.Headers) -> int | None:
    # Delta-seconds form only; an HTTP-date or any other text is not reported.
    raw = headers.get("retry-after", "").strip()
    if raw.isascii() and raw.isdigit() and len(raw) <= 6 and int(raw) <= _MAX_RETRY_AFTER_SECONDS:
        return int(raw)
    return None


class InjectedHTTPXTransport:
    """Requires an explicit preconfigured client, not accessible via MCP."""

    def __init__(self, client: httpx.AsyncClient, *, api_key: str):
        if not api_key:
            raise ValueError("API key not configured")
        # An injected client must be bound to the single allowlisted origin.
        if str(client.base_url).rstrip("/") != OFFICIAL_BASE_URL:
            raise ValueError("Provider client must use pinned base origin")
        self._client = client
        self.__api_key = api_key

    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> ProviderReply:
        path, checked = validated_request(path, params)
        try:
            # Redirects must never be followed, even if caller set a client default.
            async with self._client.stream(
                "GET", path, params=checked,
                headers={"x-apisports-key": self.__api_key, "Accept": "application/json"},
                follow_redirects=False,
            ) as response:
                code = response.status_code
                if not (200 <= code <= 299):
                    if 300 <= code <= 399:
                        raise ProviderUnavailable("PROVIDER_REDIRECT_REJECTED", retryable=False)
                    raise ProviderHTTPError(
                        code, retry_after_seconds=_retry_after(response.headers) if code == 429 else None)
                chunks: list[bytes] = []
                count = 0
                async for chunk in response.aiter_bytes():
                    count += len(chunk)
                    if count > (_MAX_ODDS_BODY if path in _LARGE_BODY_PATHS else _MAX_BODY):
                        raise ProviderDataError("PROVIDER_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                try:
                    data = json.loads(b"".join(chunks))
                except (UnicodeDecodeError, ValueError):
                    raise ProviderDataError("PROVIDER_INVALID_JSON") from None
                if not isinstance(data, dict):
                    raise ProviderDataError("PROVIDER_JSON_NOT_OBJECT")
                if data.get("errors") not in (None, [], {}):
                    # Keys only (never the values), and this reply's quota headers.
                    raise ProviderDeclaredErrors(
                        declared_error_keys(data["errors"], _PARAM.fullmatch),
                        quota_remaining_day=_quota_header(response.headers, "x-ratelimit-requests-remaining"),
                        quota_remaining_minute=_quota_header(response.headers, "x-ratelimit-remaining"),
                    )
                if "response" not in data:
                    raise ProviderDataError("PROVIDER_RESPONSE_FIELD_MISSING")
                paging = data.get("paging")
                if paging is not None:
                    if not isinstance(paging, dict):
                        raise ProviderDataError("PROVIDER_INVALID_PAGINATION")
                    current, total = paging.get("current"), paging.get("total")
                    if not (type(current) is int and type(total) is int and
                            1 <= current <= total <= 100000):
                        raise ProviderDataError("PROVIDER_INVALID_PAGINATION")
                return ProviderReply(
                    status_code=code,
                    data=data,
                    received_at_utc=datetime.now(timezone.utc).isoformat(),
                    bytes_received=count,
                    quota_remaining_day=_quota_header(response.headers, "x-ratelimit-requests-remaining"),
                    quota_remaining_minute=_quota_header(response.headers, "x-ratelimit-remaining"),
                    provider_date_utc=provider_http_date(response.headers),
                )
        except httpx.TimeoutException:
            raise ProviderUnavailable("PROVIDER_TIMEOUT") from None
        except httpx.RequestError:
            raise ProviderUnavailable("PROVIDER_NETWORK_ERROR") from None
