"""Structured result codes returned by apifootball_* tools. Never raised into MCP."""
from __future__ import annotations

CODES: dict[str, str] = {
    "OK": "Provider answered with data.",
    "EMPTY": "Provider answered with no rows; not proof of missing coverage.",
    "DISABLED": "APIFOOTBALL_ENABLED is off; no provider call is made.",
    "CONFIG_INVALID": "Server-side configuration is invalid; no provider call is made.",
    "API_KEY_MISSING": "APIFOOTBALL_API_KEY is not configured server-side.",
    "INVALID_ARGUMENT": "Client-side validation refused the arguments; no provider call is made.",
    "CIRCUIT_OPEN": "Recent provider failures opened the circuit breaker.",
    "QUOTA_RESERVE_REACHED": "Provider-reported daily remaining quota is at or below the reserve.",
    "QUOTA_EXHAUSTED": "Provider reported the daily quota is exhausted.",
    "DAILY_BUDGET_EXHAUSTED": "Local APIFOOTBALL_MAX_DAILY_REQUESTS reached in this process.",
    "RATE_LIMITED_LOCAL": "Local APIFOOTBALL_MAX_RPM reached in this process.",
    "RATE_LIMITED_PROVIDER": "Provider rate limit (HTTP 429 or reported per-minute limit).",
    "QUEUE_TIMEOUT": "No concurrency slot became free within the queue timeout.",
    "TIMEOUT": "Provider did not answer within the configured timeout.",
    "NETWORK_ERROR": "Provider unreachable.",
    "AUTH_FAILED": "Provider rejected the configured key.",
    "PLAN_RESTRICTED": "Provider reported the request is not available on the plan.",
    "HTTP_ERROR": "Unexpected non-200 HTTP status (redirects are never followed).",
    "UPSTREAM_ERROR": "Provider HTTP 5xx.",
    "INVALID_JSON": "Provider body is not valid JSON.",
    "INCOMPLETE_RESPONSE": "Provider JSON lacks the expected envelope.",
    "RESPONSE_TOO_LARGE": "Payload exceeds the configured response or output limit.",
    "PROVIDER_ERROR": "Provider returned errors; see provider_errors.",
    "INTERNAL_ERROR": "Unexpected module failure, isolated from the MCP server.",
}
