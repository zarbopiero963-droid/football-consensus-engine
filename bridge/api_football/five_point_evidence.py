"""Evidence-only five-point certification helpers.

No provider calls, secrets, guessed prices, or global-completeness claims.
Integrate with bounded live probes and verified evidence before certification.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def pagination_coverage(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Provider-advertised pages must be observed in full, without a page-10 shortcut."""
    valid = [p for p in pages if p.get("status") in ("OBSERVED", "NO_DATA_OBSERVED")
             and type(p.get("page")) is int and isinstance(p.get("pagination"), dict)]
    totals = {p["pagination"].get("total") for p in valid}
    seen = {p["page"] for p in valid}
    if len(totals) != 1 or not all(type(t) is int and t > 0 for t in totals):
        return {"status": "UNKNOWN", "observed_pages": sorted(seen)}
    total = next(iter(totals))
    missing = [page for page in range(1, total + 1) if page not in seen]
    return {"status": "COMPLETE" if not missing else "INCOMPLETE",
            "observed_pages": sorted(seen), "total_pages": total,
            "next_missing_page": missing[0] if missing else None}


def extract_odds(payload: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    """Preserve market namespaces, original strings and suspension/null semantics."""
    if phase not in ("PREMATCH", "LIVE"):
        raise ValueError("INVALID_PHASE")
    items = payload.get("response")
    if not isinstance(items, list):
        return []
    observations = []
    for fixture in items:
        if not isinstance(fixture, dict):
            continue
        ident = fixture.get("fixture")
        fixture_id = ident.get("id") if isinstance(ident, dict) else ident
        books = fixture.get("bookmakers")
        if phase == "LIVE" and not isinstance(books, list):
            books = [fixture]
        if not isinstance(books, list):
            continue
        for book in books:
            if not isinstance(book, dict):
                continue
            bets = book.get("bets")
            if not isinstance(bets, list):
                continue
            for bet in bets:
                if not isinstance(bet, dict):
                    continue
                values = bet.get("values")
                if not isinstance(values, list):
                    continue
                for sel in values:
                    if not isinstance(sel, dict):
                        continue
                    observations.append({
                        "phase": phase, "fixture_id": fixture_id,
                        "bookmaker_id": book.get("id"), "bookmaker_name": book.get("name"),
                        "market_id": bet.get("id"), "market_name": bet.get("name"),
                        "selection": sel.get("value"), "odd": sel.get("odd"),
                        "handicap": sel.get("handicap", bet.get("handicap")),
                        "main": sel.get("main", bet.get("main", "MISSING")),
                        "suspended": sel.get("suspended", bet.get("suspended", "MISSING")),
                        "updated": bet.get("update", fixture.get("update")),
                        "fixture_status": fixture.get("status", "MISSING"),
                        "market_namespace": phase,
                    })
    return observations


def field_types(samples: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Union of actual observed JSON types, including explicit null."""
    types: dict[str, set[str]] = defaultdict(set)
    def walk(value: Any, path: str) -> None:
        kind = ("null" if value is None else "boolean" if isinstance(value, bool)
                else "object" if isinstance(value, dict) else "array" if isinstance(value, list)
                else "integer" if isinstance(value, int) else "number" if isinstance(value, float)
                else "string" if isinstance(value, str) else "unknown")
        types[path].add(kind)
        if isinstance(value, dict):
            for key, part in value.items():
                walk(part, path + "." + str(key))
        if isinstance(value, list):
            for part in value:
                walk(part, path + "[]")
    for sample in samples:
        walk(sample, "$")
    return {key: sorted(values) for key, values in sorted(types.items())}


def live_delta(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare two actually captured snapshots; silence is not evidence of no change."""
    def index(rows):
        return {(r["phase"], r["fixture_id"], r["bookmaker_id"],
                 r["market_id"], r["selection"]): r for r in rows}
    old, new = index(before), index(after)
    shared = set(old) & set(new)
    changes = []
    for key in sorted(shared, key=repr):
        for field in ("odd", "suspended", "main", "updated", "handicap"):
            if old[key].get(field) != new[key].get(field):
                changes.append({"key": key, "field": field,
                                "before": old[key].get(field), "after": new[key].get(field)})
    return {"added": len(set(new) - set(old)), "removed": len(set(old) - set(new)),
            "changes": changes, "status": "CHANGE_OBSERVED" if changes or set(old) != set(new)
            else "NOT_OBSERVED_DURING_WINDOW"}
