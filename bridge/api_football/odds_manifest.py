"""Fail-closed, bounded, price-free manifests of oversized /odds and /odds/live replies.

Never truncate quoted prices and never present a partial price list as complete: a manifest
carries identifiers, names, counts and the provider's own selection fields (label, handicap,
main, suspended exactly as published, null kept null, absent kept absent), but never `odd`.

FIX-05. Pre-match and live markets stay in SEPARATE namespaces (pre-match bet ids belong to
apifootball_bets, live bet ids to apifootball_live_bets; the two are never mixed). Every
manifest lists the next, strictly narrower follow-up calls as plain, re-validated provider
parameters (fixture -> fixture+bookmaker -> fixture+bookmaker+bet for pre-match; fixture ->
fixture+bet for live), each with the counts it must return, plus the next provider page.
A request already at the narrowest level is NOT_SPLITTABLE: the manifest says so instead of
suggesting the same call again (G08 loop).

Detail levels, most detailed first: SELECTIONS (labels and published status fields per
selection), MARKETS (per market counts), BOOKMAKERS (per bookmaker counts), FIXTURES (per
fixture counts). The caller keeps the most detailed one that fits the MCP size limit; every
level is complete at its own granularity.
"""
from __future__ import annotations

from typing import Any, Mapping

DETAIL_LEVELS = ("SELECTIONS", "MARKETS", "BOOKMAKERS", "FIXTURES")
# Provider selection fields copied as published (never `odd`). Evidence: repo fixtures and
# tests (tests/test_api_football_five_point_evidence.py, tests/_apifootball_fake_provider.py).
_SELECTION_FIELDS = ("value", "handicap", "main", "suspended")
_CARRIED = ("timezone",)  # request parameters a narrower follow-up keeps


def _envelope(data: dict[str, Any], get: str, code: str) -> tuple[list, dict]:
    envelope = data.get("data", data)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("get"), str) \
            or envelope["get"].lstrip("/") != get:
        raise ValueError(code)
    response = envelope.get("response")
    paging = envelope.get("paging")
    if not isinstance(response, list) or not isinstance(paging, dict):
        raise ValueError(code)
    return response, paging


def _selection(v: dict[str, Any]) -> dict[str, Any]:
    return {k: v[k] for k in _SELECTION_FIELDS if k in v}


def _status_counts(values: list[dict[str, Any]]) -> dict[str, int]:
    # Only an explicit boolean is a status; a missing or null `suspended` stays unknown.
    return {"suspended": sum(1 for v in values if v.get("suspended") is True),
            "open": sum(1 for v in values if v.get("suspended") is False),
            "suspension_unknown": sum(1 for v in values if not isinstance(v.get("suspended"), bool))}


def _lines(values: list[dict[str, Any]]) -> list[Any]:
    """Distinct published `handicap` values (Over/Under and handicap lines), raw, in order."""
    seen: list[Any] = []
    for v in values:
        if "handicap" in v and v["handicap"] not in seen:
            seen.append(v["handicap"])
    return seen


def _next_page(request: Mapping[str, Any], paging: dict[str, Any]) -> dict[str, Any] | None:
    current, total = paging.get("current"), paging.get("total")
    if type(current) is int and type(total) is int and current < total:
        from .continuation import mcp_arguments
        return mcp_arguments({**request, "page": current + 1})
    return None


def odds_page_manifest(data: dict[str, Any], *, max_fixtures: int = 200, request: Mapping[str, Any] | None = None,
                       detail: str = "SELECTIONS") -> dict[str, Any]:
    if detail not in DETAIL_LEVELS:
        raise ValueError("INVALID_MANIFEST_DETAIL")
    response, paging = _envelope(data, "odds", "INVALID_ODDS_ENVELOPE")
    request = dict(request or {})
    fixtures, follow = [], []
    total_markets = total_selections = 0
    level = ("bet" if {"fixture", "bookmaker"} <= request.keys() else
             "bookmaker" if "fixture" in request else "fixture")
    splittable = not {"fixture", "bookmaker", "bet"} <= request.keys()
    carried = {k: request[k] for k in _CARRIED if k in request}
    for item in response:
        if not isinstance(item, dict) or not isinstance(item.get("fixture"), dict):
            raise ValueError("INVALID_ODDS_FIXTURE")
        fixture_id = item["fixture"].get("id")
        if type(fixture_id) is not int or fixture_id < 1:
            raise ValueError("INVALID_ODDS_FIXTURE_ID")
        # Missing stays missing: an absent list is malformed, never zero.
        bookmakers = item.get("bookmakers")
        if not isinstance(bookmakers, list):
            raise ValueError("INVALID_ODDS_BOOKMAKERS")
        markets = selections = 0
        books = []
        for book in bookmakers:
            if not isinstance(book, dict) or not isinstance(book.get("bets"), list):
                raise ValueError("INVALID_ODDS_MARKETS")
            book_id = book.get("id")
            if type(book_id) is not int:
                raise ValueError("INVALID_ODDS_BOOKMAKER_ID")
            bets = []
            book_selections = 0
            for bet in book["bets"]:
                if not isinstance(bet, dict) or not isinstance(bet.get("values"), list) \
                        or not all(isinstance(v, dict) for v in bet["values"]):
                    raise ValueError("INVALID_ODDS_SELECTIONS")
                if type(bet.get("id")) is not int:
                    raise ValueError("INVALID_ODDS_BET_ID")
                values = bet["values"]
                markets += 1
                selections += len(values)
                book_selections += len(values)
                entry = {"bet_id": bet["id"], "name": bet.get("name"), "selections": len(values),
                         **_status_counts(values)}
                if _lines(values):
                    entry["lines"] = _lines(values)
                if detail == "SELECTIONS":
                    entry["values"] = [_selection(v) for v in values]
                bets.append(entry)
                if splittable and level == "bet":
                    follow.append({"arguments": {"fixture": fixture_id, "bookmaker": book_id, "bet": bet["id"], **carried},
                                   "expect": {"fixtures": 1, "bookmakers": 1, "markets": 1, "selections": len(values)}})
            book_entry = {"bookmaker_id": book_id, "name": book.get("name"), "markets": len(bets),
                          "selections": book_selections}
            if detail in ("SELECTIONS", "MARKETS"):
                book_entry["bets"] = bets
            books.append(book_entry)
            if splittable and level == "bookmaker":
                narrowed = {"fixture": fixture_id, "bookmaker": book_id, **carried}
                if "bet" in request:
                    narrowed["bet"] = request["bet"]
                follow.append({"arguments": narrowed,
                               "expect": {"fixtures": 1, "bookmakers": 1, "markets": len(bets),
                                          "selections": book_selections}})
        total_markets += markets
        total_selections += selections
        if splittable and level == "fixture":
            narrowed = {"fixture": fixture_id, **carried}
            for k in ("bookmaker", "bet"):
                if k in request:
                    narrowed[k] = request[k]
            follow.append({"arguments": narrowed, "expect": {"fixtures": 1, "bookmakers": len(books),
                                                              "markets": markets, "selections": selections}})
        if len(fixtures) < max_fixtures:
            fx = {"fixture_id": fixture_id, "update": item.get("update"), "bookmakers": len(bookmakers),
                  "markets": markets, "selections": selections}
            if detail != "FIXTURES":
                fx["bookmaker_list"] = books
            fixtures.append(fx)
    return {"phase": "PRE_MATCH", "market_namespace": "PRE_MATCH", "bet_id_catalog": "apifootball_bets",
            "partial": True, "prices_included": False, "reason": "ODDS_PAGE_EXCEEDS_MCP_SIZE_LIMIT",
            "detail": detail, "fixture_list_complete": len(fixtures) == len(response),
            "fixture_ids": fixtures, "fixtures_in_page": len(response),
            "market_groups_in_page": total_markets, "selections_in_page": total_selections,
            "paging": paging, "next_page_arguments": _next_page(request, paging),
            "follow_up": {"tool": "apifootball_odds", "split_by": level if splittable else None,
                          "splittable": splittable, "calls": follow if splittable else [],
                          "reason": None if splittable else "NOT_SPLITTABLE_FIXTURE_BOOKMAKER_BET_IS_THE_NARROWEST_FILTER"},
            "retrieval": "Call each follow_up.calls[*].arguments with apifootball_odds; each reply must match its "
                         "expect counts. Nothing is fetched automatically; respect quota."}


def live_odds_manifest(data: dict[str, Any], *, max_fixtures: int = 200,
                       max_markets_per_fixture: int = 300, request: Mapping[str, Any] | None = None,
                       detail: str = "SELECTIONS") -> dict[str, Any]:
    """Price-free, navigable summary of an oversized /odds/live reply.

    Live replies carry `odds[]` per fixture (no bookmaker list). Every market keeps its LIVE
    bet id so the client can fetch exact prices with apifootball_live_odds(fixture=<id>,
    bet=<bet_id>). Missing lists are malformed (fail closed), never counted as zero.
    """
    if detail not in DETAIL_LEVELS:
        raise ValueError("INVALID_MANIFEST_DETAIL")
    response, paging = _envelope(data, "odds/live", "INVALID_LIVE_ODDS_ENVELOPE")
    request = dict(request or {})
    splittable = not {"fixture", "bet"} <= request.keys()
    level = "bet" if "fixture" in request else "fixture"
    fixtures, follow = [], []
    total_markets = total_selections = total_suspended = 0
    markets_complete = True
    for item in response:
        if not isinstance(item, dict) or not isinstance(item.get("fixture"), dict):
            raise ValueError("INVALID_LIVE_ODDS_FIXTURE")
        fixture_id = item["fixture"].get("id")
        if type(fixture_id) is not int or fixture_id < 1:
            raise ValueError("INVALID_LIVE_ODDS_FIXTURE_ID")
        odds = item.get("odds")
        if not isinstance(odds, list):
            raise ValueError("INVALID_LIVE_ODDS_MARKETS")
        markets = []
        selections = suspended = 0
        for bet in odds:
            if not isinstance(bet, dict) or not isinstance(bet.get("values"), list):
                raise ValueError("INVALID_LIVE_ODDS_SELECTIONS")
            bet_id = bet.get("id")
            if type(bet_id) is not int:
                raise ValueError("INVALID_LIVE_ODDS_BET_ID")
            values = bet["values"]
            if not all(isinstance(v, dict) for v in values):
                raise ValueError("INVALID_LIVE_ODDS_SELECTIONS")
            counts = _status_counts(values)
            selections += len(values)
            suspended += counts["suspended"]
            if splittable and level == "bet":
                follow.append({"arguments": {"fixture": fixture_id, "bet": bet_id},
                               "expect": {"fixtures": 1, "markets": 1, "selections": len(values)}})
            if len(markets) < max_markets_per_fixture:
                entry = {"bet_id": bet_id, "name": bet.get("name"), "selections": len(values),
                         "suspended": counts["suspended"], "open": counts["open"],
                         "suspension_unknown": counts["suspension_unknown"]}
                if _lines(values):
                    entry["lines"] = _lines(values)
                if detail == "SELECTIONS":
                    entry["values"] = [_selection(v) for v in values]
                markets.append(entry)
            else:
                markets_complete = False
        total_markets += len(odds)
        total_selections += selections
        total_suspended += suspended
        if splittable and level == "fixture":
            narrowed = {"fixture": fixture_id, **({"bet": request["bet"]} if "bet" in request else {})}
            follow.append({"arguments": narrowed, "expect": {"fixtures": 1, "markets": len(odds),
                                                             "selections": selections}})
        if len(fixtures) < max_fixtures:
            status = item["fixture"].get("status") if isinstance(item["fixture"].get("status"), dict) else {}
            fx = {"fixture_id": fixture_id, "elapsed": status.get("elapsed"),
                  "update": item.get("update"), "markets_count": len(odds),
                  "selections": selections, "suspended": suspended}
            if detail in ("SELECTIONS", "MARKETS", "BOOKMAKERS"):
                fx["markets"] = markets
            fixtures.append(fx)
    return {"phase": "LIVE", "market_namespace": "LIVE", "bet_id_catalog": "apifootball_live_bets",
            "partial": True, "prices_included": False,
            "reason": "LIVE_ODDS_EXCEEDS_MCP_SIZE_LIMIT", "detail": detail,
            "fixture_list_complete": len(fixtures) == len(response),
            "market_list_complete": markets_complete and detail != "FIXTURES",
            "fixtures": fixtures, "fixtures_in_page": len(response),
            "markets_in_page": total_markets, "selections_in_page": total_selections,
            "suspended_in_page": total_suspended, "paging": paging,
            "follow_up": {"tool": "apifootball_live_odds", "split_by": level if splittable else None,
                          "splittable": splittable, "calls": follow if splittable else [],
                          "reason": None if splittable else "NOT_SPLITTABLE_FIXTURE_BET_IS_THE_NARROWEST_FILTER"},
            "retrieval": "Use apifootball_live_odds(fixture=<fixture_id>, bet=<bet_id>) for prices; respect quota."}
