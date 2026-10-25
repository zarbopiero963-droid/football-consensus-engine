"""Safe, lossless discovery of observed API-FOOTBALL bookmaker, market and statistic fields.

This module never accesses the network. Catalog entries are not proof of
fixture-level availability; live/pre-match evidence must remain separate.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Any

def fields(value: Any, path: str = "$") -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    def walk(node, prefix):
        typ = "null" if node is None else ("boolean" if type(node) is bool else type(node).__name__)
        out[prefix].add(typ)
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str) and len(key) <= 128:
                    walk(item, prefix + "." + key)
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix + "[]")
    walk(value, path)
    return dict(out)

def classify_market(value: Any, *, live: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"state": "INVALID"}
    # A bookmaker catalog is prematch-only evidence; do not infer live IDs.
    result = {"state": "CATALOG_ONLY", "id": value.get("id"), "name": value.get("name"),
              "phase": "LIVE" if live else "PREMATCH"}
    if live:
        main = value.get("main", "MISSING")
        if main is None:
            result["main"] = "NULL"
        elif type(main) is bool:
            result["main"] = "TRUE" if main else "FALSE"
        else:
            result["main"] = "MISSING" if main == "MISSING" else "UNVERIFIED"
        result["suspended"] = value.get("suspended", "MISSING")
    return result

def inventory_payload(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Conserve response-field types and observed catalog identifiers only."""
    if not isinstance(payload, dict) or payload.get("errors") not in (None, [], {}):
        return {"path": path, "status": "PROVIDER_ERROR"}
    body = payload.get("response")
    if isinstance(body, dict) and path == "/teams/statistics":
        return {"path": path, "status": "OBSERVED" if body else "NO_DATA_OBSERVED",
                "count": 1 if body else 0, "fields": {k: sorted(v) for k, v in fields(body).items()},
                "response_kind": "object"}
    if not isinstance(body, list):
        return {"path": path, "status": "NO_LIST_RESPONSE"}
    result: dict[str, Any] = {"path": path, "status": "OBSERVED" if body else "NO_DATA_OBSERVED",
        "count": len(body), "fields": {k: sorted(v) for k, v in fields(body).items()}}
    if path in ("/odds/bookmakers", "/odds/bets", "/odds/live/bets"):
        result["catalog"] = [{"id": row.get("id"), "name": row.get("name")}
                             for row in body if isinstance(row, dict) and type(row.get("id")) in (int, str)
                             and isinstance(row.get("name"), str)]
        result["phase"] = "LIVE" if path == "/odds/live/bets" else "PREMATCH"
    return result
