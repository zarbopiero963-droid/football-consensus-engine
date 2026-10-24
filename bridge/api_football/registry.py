"""Canonical API-FOOTBALL contract registry (docs/api_football/*.json).

The registry is the only source for tools, validators, tests and coverage.
It is a versioned local snapshot: nothing here depends on the provider's
documentation site being reachable at runtime.

`python -m bridge.api_football.registry` prints the coverage matrix JSON.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from bridge.api_football.validators import KINDS, Param

REGISTRY_DIR = Path(__file__).resolve().parents[2] / "docs" / "api_football"
PENDING = "PENDING_OFFICIAL_VERIFICATION"
# Parameter statuses that allow generating an MCP tool. Names must come from an
# official or owner-verified reading; nothing reconstructed from memory.
IMPLEMENTABLE = frozenset({"OWNER_VERIFIED_NAMES", "OFFICIAL_VERIFIED"})
CONTRACT_STATUSES = frozenset({PENDING, "PARTIAL_OWNER_BASELINE", "OFFICIAL_VERIFIED", "UNAVAILABLE", "UNKNOWN"})
IMPLEMENTATION_STATUSES = frozenset({"NOT_IMPLEMENTED_CONTRACT_PENDING", "IMPLEMENTED_PROVISIONAL", "IMPLEMENTED"})
MOCK_TEST = "tests/api_football/test_contracts.py"

_ODDS_WARNING = "Bookmaker odds are market data, not a model probability; implied probabilities are not provider predictions."
_PREDICTION_WARNING = ("Provider prediction: methodology, market dependency and publication time are unverified; "
                       "not point-in-time certified.")
WARNINGS = {"MARKET_ODDS": (_ODDS_WARNING,), "PROVIDER_PREDICTION": (_PREDICTION_WARNING,)}


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class Endpoint:
    id: str
    path: str
    method: str
    category: str
    tool_name: str
    params: tuple[Param, ...]
    parameter_status: str
    contract_status: str
    cache_ttl: int
    require_any: bool
    data_class: str
    warnings: tuple[str, ...]
    page_size: int | None = None   # unknown until the official contract is acquired


def _read(name: str, directory: Path) -> dict:
    try:
        return json.loads((directory / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"{name}: {type(exc).__name__}") from None


def load_raw(directory: Path = REGISTRY_DIR) -> dict:
    return {name: _read(name + ".json", directory)
            for name in ("endpoints", "parameters", "response_schemas", "sources")}


def check(raw: dict) -> list[str]:
    """Structural and completeness checks. Returns problems; empty means consistent."""
    problems: list[str] = []
    endpoints = raw["endpoints"].get("endpoints", [])
    params = raw["parameters"].get("endpoints", {})
    schemas = raw["response_schemas"].get("endpoints", {})
    source_ids = {source["id"] for source in raw["sources"].get("sources", [])}
    ids, paths, tools = set(), set(), set()
    for endpoint in endpoints:
        eid, path = endpoint.get("id"), endpoint.get("path")
        where = f"{eid} {path}"
        if eid in ids:
            problems.append(f"{where}: duplicate id")
        if path in paths:
            problems.append(f"{where}: duplicate path")
        ids.add(eid)
        paths.add(path)
        if endpoint.get("method") != "GET":
            problems.append(f"{where}: unsupported method {endpoint.get('method')}")
        if not isinstance(path, str) or not path.startswith("/") or "?" in path:
            problems.append(f"{where}: invalid path")
        if endpoint.get("inventory_source") not in source_ids:
            problems.append(f"{where}: unknown inventory source")
        if endpoint.get("contract_status") not in CONTRACT_STATUSES:
            problems.append(f"{where}: invalid contract_status")
        if endpoint.get("implementation_status") not in IMPLEMENTATION_STATUSES:
            problems.append(f"{where}: invalid implementation_status")
        if endpoint.get("real_test_status") == "REAL_VERIFIED" and not endpoint.get("last_verified_utc"):
            problems.append(f"{where}: REAL_VERIFIED without last_verified_utc")
        entry = params.get(eid)
        if entry is None or entry.get("path") != path:
            problems.append(f"{where}: parameters.json entry missing or path mismatch")
            continue
        if schemas.get(eid, {}).get("path") != path:
            problems.append(f"{where}: response_schemas.json entry missing or path mismatch")
        tool = endpoint.get("mcp_tool")
        implementable = entry.get("status") in IMPLEMENTABLE and isinstance(entry.get("params"), list)
        if entry.get("status") in IMPLEMENTABLE and entry.get("source") not in source_ids:
            problems.append(f"{where}: parameter source is not a registered source")
        if tool is None:
            if endpoint.get("implementation_status") != "NOT_IMPLEMENTED_CONTRACT_PENDING":
                problems.append(f"{where}: implementation status without a tool")
            continue
        if not implementable:
            problems.append(f"{where}: tool without a verified parameter contract")
            continue
        expected = "apifootball_" + path.strip("/").replace("/", "_")
        if tool != expected or tool in tools:
            problems.append(f"{where}: tool name must be unique and equal {expected}")
        tools.add(tool)
        if endpoint.get("implementation_status") == "IMPLEMENTED" and endpoint.get("contract_status") != "OFFICIAL_VERIFIED":
            problems.append(f"{where}: IMPLEMENTED requires an OFFICIAL_VERIFIED contract")
        policy = endpoint.get("client_policy") or {}
        if type(policy.get("cache_ttl_seconds")) is not int or policy["cache_ttl_seconds"] < 0:
            problems.append(f"{where}: client_policy.cache_ttl_seconds must be a non-negative integer")
        names, wires = set(), set()
        for param in entry["params"]:
            name, wire = param.get("name"), param.get("wire")
            if not isinstance(name, str) or not name.isidentifier() or name == "use_cache" or name in names:
                problems.append(f"{where}: invalid or duplicate parameter name {name!r}")
            if not isinstance(wire, str) or not wire or wire in wires:
                problems.append(f"{where}: invalid or duplicate wire name {wire!r}")
            names.add(name)
            wires.add(wire)
            if (param.get("client_validation") or {}).get("kind") not in KINDS:
                problems.append(f"{where}: parameter {name} has an unknown validation kind")
    return problems


def _param(item: dict) -> Param:
    rules = item["client_validation"]
    limits = {key: rules[key] for key in ("min_len", "max_len", "low", "high", "max_items") if key in rules}
    return Param(name=item["name"], wire=item["wire"], kind=rules["kind"], description=item["description"], **limits)


@lru_cache(maxsize=1)
def implemented(directory: Path = REGISTRY_DIR) -> tuple[Endpoint, ...]:
    raw = load_raw(directory)
    problems = check(raw)
    if problems:
        raise RegistryError("; ".join(problems[:5]))
    params = raw["parameters"]["endpoints"]
    result = []
    for endpoint in raw["endpoints"]["endpoints"]:
        if not endpoint.get("mcp_tool"):
            continue
        policy = endpoint["client_policy"]
        data_class = policy.get("data_class", "PROVIDER_DATA")
        result.append(Endpoint(
            id=endpoint["id"], path=endpoint["path"], method=endpoint["method"], category=endpoint["category"],
            tool_name=endpoint["mcp_tool"], params=tuple(_param(p) for p in params[endpoint["id"]]["params"]),
            parameter_status=params[endpoint["id"]]["status"], contract_status=endpoint["contract_status"],
            cache_ttl=policy["cache_ttl_seconds"], require_any=bool(policy.get("require_any_filter", True)),
            data_class=data_class, warnings=WARNINGS.get(data_class, ()),
        ))
    return tuple(result)


def coverage(raw: dict | None = None) -> dict:
    """Coverage matrix and dashboard with explicit denominators."""
    raw = raw or load_raw()
    params = raw["parameters"]["endpoints"]
    schemas = raw["response_schemas"]["endpoints"]
    endpoints = raw["endpoints"]["endpoints"]
    rows, matrix = [], []
    known_params = implemented_params = 0
    for endpoint in endpoints:
        entry = params[endpoint["id"]]
        tool = endpoint.get("mcp_tool")
        listed = entry.get("params") or []
        known_params += len(listed)
        implemented_params += len(listed) if tool else 0
        rows.append({
            "id": endpoint["id"], "path": endpoint["path"], "mcp_tool": tool,
            "parameter_status": entry["status"], "contract_status": endpoint["contract_status"],
            "response_schema_status": schemas[endpoint["id"]]["status"],
            "implementation_status": endpoint["implementation_status"],
            "mock_test": MOCK_TEST if tool else None, "real_test_status": endpoint["real_test_status"],
        })
        if not listed:
            matrix.append({"endpoint": endpoint["path"], "parameter": None, "official_source": None,
                           "mcp_schema": None, "test": None, "status": entry["status"]})
        for param in listed:
            matrix.append({
                "endpoint": endpoint["path"], "parameter": param["wire"], "mcp_argument": param["name"],
                "official_source": entry["source"], "official_type": param["official"]["type"],
                "official_required": param["official"]["required"],
                "mcp_schema": param["client_validation"]["kind"] if tool else None,
                "client_validation_status": param["client_validation"]["status"],
                "test": MOCK_TEST if tool else None,
                "status": "MOCK_VERIFIED_NAMES_ONLY" if tool else entry["status"],
            })
    total = len(endpoints)
    verified_contracts = sum(1 for e in endpoints if e["contract_status"] == "OFFICIAL_VERIFIED")
    tools = sum(1 for e in endpoints if e.get("mcp_tool"))
    return {
        "registry_version": raw["endpoints"]["registry_version"],
        "dashboard": {
            "endpoints_catalogued": {"value": total, "denominator_note": "MatchPilot #248 baseline; official catalogue not acquired"},
            "official_contracts_verified": {"numerator": verified_contracts, "denominator": total},
            "owner_verified_parameter_names": {"numerator": sum(1 for e in endpoints if params[e["id"]]["status"] in IMPLEMENTABLE), "denominator": total},
            "endpoints_implemented_as_tools": {"numerator": tools, "denominator": total},
            "parameters_covered": {"numerator": implemented_params, "denominator": known_params,
                                   "denominator_note": "known parameter names only; total official parameter count is UNKNOWN"},
            "response_schemas_verified": {"numerator": sum(1 for s in schemas.values() if s["status"] == "OFFICIAL_VERIFIED"), "denominator": total},
            "real_tests_passed": {"numerator": sum(1 for e in endpoints if e["real_test_status"] == "REAL_VERIFIED"), "denominator": total},
            "incomplete_contracts": {"value": total - verified_contracts},
            "breaking_changes_detected": {"value": 0, "note": "no official snapshot acquired yet"},
        },
        "endpoints": rows,
        "matrix": matrix,
    }


def render_coverage() -> str:
    return json.dumps(coverage(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render_coverage())
