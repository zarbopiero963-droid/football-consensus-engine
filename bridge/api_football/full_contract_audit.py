"""Strict audit of implemented MCP endpoints against documentary provider scope.

Completeness is a verified claim only if *every* route and accepted parameter has
explicit documentary proof and a real bounded observation. Never infer that an
HTTP 200 proves all parameter combinations or data availability.
"""
from __future__ import annotations
from typing import Any
from .catalog import ALLOWED_PATHS
from .contracts import ENDPOINTS
from .registry import build_registry, PENDING


def audit_contract_coverage(*, certified: dict[str, Any] | None = None) -> dict[str, Any]:
    reg = build_registry()
    mapped = {c.path for c in ENDPOINTS.values()}
    official = set(ALLOWED_PATHS)
    proof = certified or {}
    rows: list[dict[str, Any]] = []
    for path in sorted(official | mapped):
        tools = sorted(name for name, spec in ENDPOINTS.items() if spec.path == path)
        parameter_names = sorted({name for name in tools for name in ENDPOINTS[name].fields})
        entry = proof.get(path) if isinstance(proof, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        docs = entry.get("documentation")
        live = entry.get("real_response")
        verified_parameters = set(entry.get("verified_parameters", [])) if isinstance(entry.get("verified_parameters"), list) else set()
        documented = docs == "VERIFIED_OFFICIAL_SECTION"
        real = live == "REAL_HTTP_200_OBSERVED"
        parameters_complete = all(p in verified_parameters for p in parameter_names)
        complete = path in mapped and documented and real and parameters_complete
        rows.append({"path": path, "tools": tools,
                     "implemented": bool(tools),
                     "documentation_verified": documented,
                     "real_response_verified": real,
                     "parameters_total": len(parameter_names),
                     "parameters_verified": sum(p in verified_parameters for p in parameter_names),
                     "status": "CERTIFIED_SCOPE" if complete else "NOT_CERTIFIED"})
    certified_count = sum(r["status"] == "CERTIFIED_SCOPE" for r in rows)
    return {"scope": "KNOWN_DOCUMENTARY_ROUTE_CATALOG",
            "catalog_source": reg["official_documentation"]["url"],
            "catalog_completeness_against_future_versions": "NOT_VERIFIED",
            "route_count": len(rows), "implemented_routes": sum(x["implemented"] for x in rows),
            "certified_routes": certified_count,
            "coverage_percent": round(100 * certified_count / len(rows), 2) if rows else 0,
            "verdict": "PASS" if rows and certified_count == len(rows) else "INCOMPLETE",
            "routes": rows}
