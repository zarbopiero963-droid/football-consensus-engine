"""Trusted server-side construction of the optional API-FOOTBALL client.

The factory is deliberately inert unless the independent live authorization
gate is ON. MCP arguments cannot reach this module.
"""
from __future__ import annotations

import httpx

from .config import Settings
from .resilience import ResilientFootballClient
from .transport import InjectedHTTPXTransport


def build_client(settings: Settings) -> ResilientFootballClient | None:
    """Build a pinned client only after all live preconditions pass."""
    if not settings.enabled or not settings.real_requests_authorized:
        return None
    http_client = httpx.AsyncClient(
        base_url=settings.base_url,
        timeout=settings.timeout_seconds,
        follow_redirects=False,
    )
    transport = InjectedHTTPXTransport(http_client, api_key=settings.api_key)
    return ResilientFootballClient(settings, transport)
