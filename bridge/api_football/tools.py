"""`apifootball_*` MCP tools generated from the canonical contract registry.

Only endpoints whose parameter names are owner- or officially verified get a
tool; the rest stay catalogued as PENDING_OFFICIAL_VERIFICATION. Tools are
registered whether or not the module is enabled, so the ChatGPT tool list stays
stable when the owner toggles APIFOOTBALL_ENABLED: a disabled module answers
with a structured DISABLED result and makes no network call.
"""
from __future__ import annotations

import inspect
import logging
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from bridge.api_football import registry
from bridge.api_football.client import ApiFootballClient

LOG = logging.getLogger(__name__)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

_client: ApiFootballClient | None = None


def client() -> ApiFootballClient:
    global _client
    if _client is None:
        _client = ApiFootballClient()
    return _client


def _description(endpoint: registry.Endpoint) -> str:
    names = ", ".join(param.wire for param in endpoint.params)
    text = (f"API-FOOTBALL v3 GET {endpoint.path} (football only, read-only). Provider parameters: {names}. "
            f"Parameter names {endpoint.parameter_status}; types, required-ness and allowed values are pending "
            "official verification, so the provider's own validation errors are returned verbatim. "
            "Returns the provider envelope unmodified in `raw` plus provenance, cache, quota and paging; "
            "errors are structured in `error`. At least one parameter is required (quota policy).")
    if endpoint.warnings:
        text += " " + " ".join(endpoint.warnings)
    return text


def _signature(endpoint: registry.Endpoint) -> inspect.Signature:
    parameters = [
        inspect.Parameter(param.name, inspect.Parameter.KEYWORD_ONLY, default=None,
                          annotation=Annotated[param.python_type | None, Field(description=param.description)])
        for param in endpoint.params
    ]
    parameters.append(inspect.Parameter(
        "use_cache", inspect.Parameter.KEYWORD_ONLY, default=True,
        annotation=Annotated[bool, Field(description="False forces a fresh provider request (consumes quota).")]))
    return inspect.Signature(parameters, return_annotation=dict[str, Any])


def _endpoint_tool(endpoint: registry.Endpoint):
    async def tool(**arguments) -> dict[str, Any]:
        use_cache = arguments.pop("use_cache", True)
        try:
            return await client().call(endpoint, arguments, use_cache=use_cache)
        except Exception as exc:  # isolation: never let the module raise into MCP
            LOG.error("API-FOOTBALL tool failure: tool=%s error=%s", endpoint.tool_name, type(exc).__name__)
            return {"provider": "API-FOOTBALL", "endpoint": endpoint.path, "ok": False, "status": "INTERNAL_ERROR",
                    "error": {"code": "INTERNAL_ERROR", "message": type(exc).__name__, "retryable": False}}

    tool.__name__ = endpoint.tool_name
    tool.__signature__ = _signature(endpoint)
    return tool


async def apifootball_health() -> dict[str, Any]:
    """API-FOOTBALL module configuration, circuit breaker and process scope. No provider call; no secrets."""
    return client().health()


async def apifootball_quota_report() -> dict[str, Any]:
    """Local request accounting plus the last provider-reported quota headers. No provider call."""
    return client().quota_report()


async def apifootball_endpoint_registry() -> dict[str, Any]:
    """Contract registry coverage: every catalogued endpoint, its MCP tool (if any), verification and test status."""
    try:
        return registry.coverage()
    except Exception as exc:
        return {"ok": False, "status": "INTERNAL_ERROR", "error": {"code": "INTERNAL_ERROR", "message": type(exc).__name__}}


def register(server) -> list[str]:
    """Register every apifootball_* tool on `server`. Builds all tools before adding any."""
    specs = [(_endpoint_tool(endpoint), endpoint.tool_name, _description(endpoint), READ_ONLY)
             for endpoint in registry.implemented()]
    specs += [(fn, fn.__name__, None, LOCAL_ONLY)
              for fn in (apifootball_health, apifootball_quota_report, apifootball_endpoint_registry)]
    for fn, name, description, annotations in specs:
        server.add_tool(fn, name=name, description=description, annotations=annotations)
    return [spec[1] for spec in specs]
