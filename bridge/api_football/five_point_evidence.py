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
            bets = book.get("odds") if phase == "LIVE" else book.get("bets")
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
                        "market_status": fixture.get("status", "MISSING"),
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


# Exact provider statistic labels (API-FOOTBALL v3 /fixtures/statistics).
# Labels are matched exactly: no fuzzy equivalence between different metrics.
TARGET_FIXTURE_STATISTICS = {
    "xg": "expected_goals",
    "shots_total": "Total Shots",
    "shots_on_target": "Shots on Goal",
    "corners": "Corner Kicks",
    "possession": "Ball Possession",
}
FINAL_STATUSES = ("FT", "AET", "PEN")
LIVE_STATUSES = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE")


def _valid_statistic(field: str, value: Any) -> bool:
    if field == "possession":
        if not isinstance(value, str) or not value.endswith("%"):
            return False
        number = value[:-1]
        return number.isdigit() and 0 <= int(number) <= 100
    if field == "xg":
        if isinstance(value, bool):
            return False
        try:
            return float(value) >= 0
        except (TypeError, ValueError):
            return False
    return type(value) is int and value >= 0


def extract_fixture_statistics(payload: dict[str, Any], fixture_id: int,
                               fixture_status: str | None) -> dict[str, Any]:
    """Real type/value pairs plus per-field verdicts; missing/null never become zero."""
    body = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(body, list):
        raise ValueError("INVALID_STATISTICS_RESPONSE")
    snapshot = ("FINAL" if fixture_status in FINAL_STATUSES else
                "IN_PLAY" if fixture_status in LIVE_STATUSES else "UNKNOWN_OR_NOT_STARTED")
    teams = []
    for entry in body:
        team = entry.get("team") if isinstance(entry, dict) else None
        stats = entry.get("statistics") if isinstance(entry, dict) else None
        if not isinstance(team, dict) or type(team.get("id")) is not int or not isinstance(stats, list):
            raise ValueError("INVALID_STATISTICS_TEAM")
        raw = []
        labels: dict[str, list[Any]] = defaultdict(list)
        for stat in stats:
            if not isinstance(stat, dict) or not isinstance(stat.get("type"), str):
                raise ValueError("INVALID_STATISTICS_ENTRY")
            value = stat.get("value")
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float, str))):
                raise ValueError("INVALID_STATISTICS_VALUE")
            raw.append({"type": stat["type"], "value": value})
            labels[stat["type"]].append(value)
        fields = {}
        for field, label in TARGET_FIXTURE_STATISTICS.items():
            values = labels.get(label)
            if values is None:
                fields[field] = {"label": label, "status": "MISSING", "value": None}
            elif len(values) != 1:
                fields[field] = {"label": label, "status": "AMBIGUOUS_DUPLICATE_LABEL", "value": None}
            elif values[0] is None:
                fields[field] = {"label": label, "status": "NULL_PUBLISHED", "value": None}
            elif not _valid_statistic(field, values[0]):
                fields[field] = {"label": label, "status": "INVALID_VALUE", "value": values[0]}
            else:
                fields[field] = {"label": label, "status": "VERIFIED_REAL", "value": values[0]}
        teams.append({"team_id": team["id"], "fields": fields, "raw_statistics": raw})
    return {"fixture_id": fixture_id, "fixture_status_at_observation": fixture_status,
            "snapshot_kind": snapshot, "teams": teams}


def summarize_fixture_statistics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-field counts over observed team rows; no global coverage claim."""
    summary = {}
    for field in TARGET_FIXTURE_STATISTICS:
        counts: dict[str, int] = defaultdict(int)
        final_verified = 0
        for obs in observations:
            for team in obs.get("teams", []):
                state = team["fields"][field]["status"]
                counts[state] += 1
                if state == "VERIFIED_REAL" and obs.get("snapshot_kind") == "FINAL":
                    final_verified += 1
        rows = sum(counts.values())
        verified = counts.get("VERIFIED_REAL", 0)
        summary[field] = {
            "team_rows": rows, "counts": dict(counts), "verified_on_final_snapshot": final_verified,
            "status": ("NOT_TESTED" if rows == 0 else "VERIFIED_REAL" if verified == rows else
                       "PARTIAL" if verified else "UNAVAILABLE"),
        }
    return {"scope": "OBSERVED_PROVIDER_SAMPLES_ONLY", "global_completeness_claim": False,
            "fixtures_observed": len(observations), "fields": summary}
