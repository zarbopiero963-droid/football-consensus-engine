"""Coverage accounting for provider-observed catalog pages and live/prematch quotes.

Only complete provider-reported pagination can earn COMPLETE; bounded scans with
remaining pages are explicitly INCOMPLETE, never relabeled 100%.
"""
from __future__ import annotations
from typing import Any

def pagination_verdict(records: list[dict[str, Any]], path: str) -> dict[str, Any]:
    pages = [r for r in records if r.get("path") == path and type(r.get("page")) is int
             and r.get("status") in ("OBSERVED", "NO_DATA_OBSERVED")]
    if not pages:
        return {"path": path, "status": "NOT_VERIFIED", "observed_pages": 0}
    total_values = {p.get("pagination", {}).get("total") for p in pages
                    if isinstance(p.get("pagination"), dict)}
    if len(total_values) != 1:
        return {"path": path, "status": "UNKNOWN_TOTAL", "observed_pages": len(pages)}
    total = next(iter(total_values))
    page_numbers = {p["page"] for p in pages}
    complete = type(total) is int and total > 0 and page_numbers == set(range(1, total + 1))
    return {"path": path, "status": "COMPLETE" if complete else "INCOMPLETE",
            "observed_pages": len(page_numbers), "provider_total_pages": total}

def odds_observations(payload: dict[str, Any], *, phase: str) -> list[dict[str, Any]]:
    """Extract factual selections with bookmaker provenance, never assume BACK/LAY."""
    observations = []
    body = payload.get("response")
    if not isinstance(body, list):
        return observations
    for fixture in body[:100]:
        if not isinstance(fixture, dict):
            continue
        fixture_id = fixture.get("fixture")
        bookmakers = fixture.get("bookmakers")
        if phase == "LIVE":
            bookmakers = fixture.get("bookmakers") or [fixture]
        if not isinstance(bookmakers, list):
            continue
        for book in bookmakers[:100]:
            if not isinstance(book, dict):
                continue
            bets = book.get("bets")
            if not isinstance(bets, list):
                continue
            for bet in bets[:100]:
                if not isinstance(bet, dict):
                    continue
                selections = bet.get("values")
                if not isinstance(selections, list):
                    continue
                for selection in selections[:100]:
                    if not isinstance(selection, dict):
                        continue
                    observations.append({
                        "phase": phase, "fixture_id": fixture_id,
                        "bookmaker_id": book.get("id"), "bookmaker_name": book.get("name"),
                        "market_id": bet.get("id"), "market_name": bet.get("name"),
                        "selection": selection.get("value"), "odd": selection.get("odd"),
                        "handicap": selection.get("handicap"),
                        "main": bet.get("main", "MISSING"),
                        "suspended": bet.get("suspended", "MISSING"),
                        "updated": bet.get("update", fixture.get("update", "MISSING")),
                    })
    return observations
