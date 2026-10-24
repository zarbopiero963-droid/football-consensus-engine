"""Single optional MCP diagnostic tool for foundation certification.

Endpoint/query tools and production HTTP transport are deliberately deferred.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import Settings


def register_tools(server: Any, settings: Settings) -> tuple[str, ...]:
    if not settings.enabled:
        return ()

    @server.tool()
    async def apifootball_health() -> dict[str, Any]:
        """Offline-only API-FOOTBALL adapter readiness (never contacts provider)."""
        return {
            "provider": "API-FOOTBALL",
            "module": "FCB-APIFOOTBALL-INTEGRATION-CERT-01/PR-01",
            "status": "MOCK_ONLY" if settings.configured else "KEY_NOT_CONFIGURED",
            "enabled": True,
            "key_present": bool(settings.api_key),
            "provider_requests_authorized": False,
            "network_calls_made": 0,
            "http_transport": "NOT_IMPLEMENTED",
            "documented_contract": "PENDING_OFFICIAL_RECONCILIATION",
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "limits_configured_not_enforced": {
                "per_minute": settings.max_rpm,
                "per_day": settings.max_daily_requests,
                "max_concurrency": settings.max_concurrency,
            },
            "warnings": [
                "No live endpoint or provider field has been certified.",
                "No request to API-FOOTBALL is possible in PR-01.",
                "Rate limiting, cache, retry and circuit breaker are planned for PR-02.",
            ],
        }

    return ("apifootball_health",)
