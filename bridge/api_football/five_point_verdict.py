"""Five independent truth-preserving API-FOOTBALL certification verdicts.

A workflow exit code 0 is not a global coverage certification.
"""
from collections import Counter
from typing import Any
from .five_point_evidence import pagination_coverage

CATALOGS = ("/odds/bookmakers", "/odds/bets", "/odds/live/bets")


def assess_five_points(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_path: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        if isinstance(row, dict):
            by_path.setdefault(row.get("path", ""), []).append(row)

    coverage = {path: pagination_coverage(by_path.get(path, [])) for path in CATALOGS + ("/odds",)}
    bookmaker_pages_ok = coverage["/odds/bookmakers"]["status"] == "COMPLETE"
    bookmakers = {str(entry["id"]) for row in by_path.get("/odds/bookmakers", [])
                  for entry in row.get("catalog", []) if isinstance(entry, dict)
                  and entry.get("id") is not None}
    prematch_odds = [ob for row in by_path.get("/odds", [])
                     for ob in row.get("odds_observations", [])
                     if isinstance(ob, dict) and ob.get("phase") == "PREMATCH"]
    actual_bookmakers = {str(o.get("bookmaker_id")) for o in prematch_odds if o.get("bookmaker_id") is not None}
    bookmaker_status = "PASS_SCOPE" if bookmaker_pages_ok and bookmakers else "PARTIAL"
    market_catalogs_ok = all(coverage[p]["status"] == "COMPLETE" for p in ("/odds/bets", "/odds/live/bets"))
    live_odds = [ob for row in by_path.get("/odds/live", [])
                 for ob in row.get("odds_observations", []) if isinstance(ob, dict) and ob.get("phase") == "LIVE"]
    market_status = "PASS_SCOPE" if market_catalogs_ok and prematch_odds and live_odds else "PARTIAL"
    statistical = [r for r in by_path.get("_statistics_samples", []) if r.get("status") == "OBSERVATION_SUMMARY"]
    sample_rows = [x for r in statistical for x in r.get("samples", []) if isinstance(x, dict)]
    stat_paths = {r.get("path") for r in sample_rows}
    fixture_ids = {s["parameters"].get("fixture") for s in sample_rows if
                   isinstance(s.get("parameters"), dict) and s["parameters"].get("fixture") is not None}
    team_ids = {s["parameters"].get("team") for s in sample_rows if
                isinstance(s.get("parameters"), dict) and s["parameters"].get("team") is not None}
    stats_ok = (len(fixture_ids) >= 2 and len(team_ids) >= 2 and
                {"/fixtures/statistics", "/fixtures/players", "/teams/statistics", "/players"} <= stat_paths)
    stats_status = "PASS_SCOPE" if stats_ok else "PARTIAL"
    deltas = [r for r in by_path.get("/odds/live", []) if r.get("status") == "LIVE_DELTA_OBSERVED"]
    changed = any(r.get("delta", {}).get("status") == "CHANGE_OBSERVED" for r in deltas)
    live_status = "PASS_SCOPE" if changed else ("PARTIAL" if deltas else "BLOCKED")
    blocked = [r for r in records if r.get("status", "").startswith("BLOCKED")]
    return {
        "scope": "OBSERVED_PROVIDER_SAMPLES_ONLY",
        "global_completeness_claim": False,
        "bookmakers": {"status": bookmaker_status, "catalog_entries": len(bookmakers),
                       "bookmakers_with_prices_observed": len(actual_bookmakers & bookmakers),
                       "catalog_pagination": coverage["/odds/bookmakers"]},
        "markets": {"status": market_status, "prematch_values_observed": len(prematch_odds),
                    "live_values_observed": len(live_odds),
                    "market_pagination": {p: coverage[p] for p in ("/odds/bets", "/odds/live/bets", "/odds")}},
        "statistics": {"status": stats_status, "distinct_fixture_samples": len(fixture_ids),
                       "distinct_team_samples": len(team_ids), "observed_endpoints": sorted(stat_paths)},
        "live": {"status": live_status, "comparable_snapshots": len(deltas),
                 "actual_change_observed": changed},
        "coverage": {"status": "PARTIAL", "blocked_or_incomplete": [
            {"path": r.get("path"), "status": r.get("status"), "page": r.get("page")}
            for r in blocked],
            "note": "Full 39-route coverage must also be verified from the signed inventory archives"},
    }
