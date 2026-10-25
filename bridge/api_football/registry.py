"""Fail-closed canonical registry derived from the MCP contract allowlist.

No network I/O occurs here. Documentary evidence is intentionally separate from
runtime implementation: an implemented parameter is never marked DOCUMENTED
until its official source section is acquired and recorded.
"""
from __future__ import annotations
import hashlib
import json
from collections import defaultdict
from typing import Any, Mapping
from .contracts import ENDPOINTS, SOURCE, VERSION

REGISTRY_VERSION = "1.0.0"
PENDING = "PENDING_OFFICIAL_VERIFICATION"
VALID_STATUSES = frozenset({"DOCUMENTED","IMPLEMENTED","MOCK_VERIFIED","REAL_VERIFIED","UNKNOWN","BLOCKED",PENDING})

def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

def _parameter(tool: str, name: str, required: bool) -> dict[str, Any]:
    # Runtime accepts these names, but their provider semantics stay pending
    # until a source-section reference is recorded.
    return {"id": f"{tool}.{name}", "name": name, "required": required,
            "type": "UNKNOWN", "format": None, "enum": [], "minimum": None,
            "maximum": None, "status": PENDING, "implementation_status": "IMPLEMENTED",
            "source": None}

def build_registry() -> dict[str, Any]:
    routes: dict[str, dict[str, Any]] = {}
    route_tools: dict[str, list[str]] = defaultdict(list)
    tools: list[dict[str, Any]] = []
    for tool_name, contract in sorted(ENDPOINTS.items()):
        route_tools[contract.path].append(tool_name)
        tools.append({
            "id": tool_name, "path": contract.path, "method": "GET",
            "runtime_contract_version": VERSION,
            "parameters": [_parameter(tool_name, name, name in contract.required) for name in contract.fields],
            "pagination": {"status": PENDING, "implemented": contract.pagination},
            "response_schema": {"status": PENDING},
            "error_schema": {"status": PENDING},
            "source": SOURCE, "official_status": PENDING,
        })
    for path, tool_names in sorted(route_tools.items()):
        routes[path] = {
            "id": "route:" + path, "method": "GET", "path": path,
            "tool_ids": sorted(tool_names), "official_status": PENDING,
            "source": SOURCE, "source_section": None,
            "response_schema": {"status": PENDING},
            "error_schema": {"status": PENDING},
        }
    return {
        "registry_version": REGISTRY_VERSION,
        "runtime_contract_version": VERSION,
        "official_documentation": {"url": SOURCE, "catalog_status": PENDING},
        "verification_policy": {"real_api_calls_authorized": False, "unknown_is_not_pass": True},
        "endpoints": list(routes.values()),
        "tools": tools,
    }

def validate_registry(registry: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    endpoint_ids: set[str] = set()
    tool_ids: set[str] = set()
    canonical_tools = set(ENDPOINTS)
    for endpoint in registry.get("endpoints", []):
        if endpoint.get("id") in endpoint_ids: errors.append("DUPLICATE_ENDPOINT_ID")
        endpoint_ids.add(endpoint.get("id"))
        if endpoint.get("official_status") not in VALID_STATUSES: errors.append("INVALID_ENDPOINT_STATUS")
    for tool in registry.get("tools", []):
        identifier = tool.get("id")
        if identifier in tool_ids: errors.append("DUPLICATE_TOOL_ID")
        tool_ids.add(identifier)
        runtime = ENDPOINTS.get(identifier)
        if runtime is None or tool.get("path") != runtime.path: errors.append("REGISTRY_MCP_DRIFT:" + str(identifier))
        for parameter in tool.get("parameters", []):
            if parameter.get("status") not in VALID_STATUSES: errors.append("INVALID_PARAMETER_STATUS")
            if parameter.get("status") == "REAL_VERIFIED": errors.append("REAL_VERIFIED_FORBIDDEN")
    if tool_ids != canonical_tools: errors.append("TOOL_SET_DRIFT")
    if {item.get("path") for item in registry.get("endpoints", [])} != {value.path for value in ENDPOINTS.values()}: errors.append("ROUTE_SET_DRIFT")
    return errors
