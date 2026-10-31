"""Fail-closed, bounded metadata-only representation of oversized /odds pages.

Do not silently truncate quoted prices. Clients should fetch individual fixtures
with the existing /odds?fixture= tool to obtain complete price data.
"""
from __future__ import annotations
from typing import Any

def odds_page_manifest(data: dict[str, Any], *, max_fixtures: int = 200) -> dict[str, Any]:
    envelope=data.get("data",data)
    if not isinstance(envelope,dict) or not isinstance(envelope.get("get"),str) \
            or envelope["get"].lstrip("/")!="odds":
        raise ValueError("INVALID_ODDS_ENVELOPE")
    response=envelope.get("response")
    paging=envelope.get("paging")
    if not isinstance(response,list) or not isinstance(paging,dict):
        raise ValueError("INVALID_ODDS_ENVELOPE")
    fixtures=[]
    total_markets=0
    total_selections=0
    for item in response:
        if not isinstance(item,dict) or not isinstance(item.get("fixture"),dict):
            raise ValueError("INVALID_ODDS_FIXTURE")
        fixture_id=item["fixture"].get("id")
        if type(fixture_id) is not int or fixture_id<1:
            raise ValueError("INVALID_ODDS_FIXTURE_ID")
        # Missing stays missing: an absent list is malformed, never zero.
        bookmakers=item.get("bookmakers")
        if not isinstance(bookmakers,list):
            raise ValueError("INVALID_ODDS_BOOKMAKERS")
        markets=0
        selections=0
        for book in bookmakers:
            if not isinstance(book,dict) or not isinstance(book.get("bets"),list):
                raise ValueError("INVALID_ODDS_MARKETS")
            for bet in book["bets"]:
                if not isinstance(bet,dict) or not isinstance(bet.get("values"),list):
                    raise ValueError("INVALID_ODDS_SELECTIONS")
                markets+=1
                selections+=len(bet["values"])
        total_markets+=markets
        total_selections+=selections
        if len(fixtures)<max_fixtures:
            fixtures.append({"fixture_id":fixture_id,"bookmakers":len(bookmakers),
                             "markets":markets,"selections":selections})
    return {"partial":True,"prices_included":False,"reason":"ODDS_PAGE_EXCEEDS_MCP_SIZE_LIMIT",
        "fixture_list_complete":len(fixtures)==len(response),
        "fixture_ids":fixtures,"fixtures_in_page":len(response),
        "market_groups_in_page":total_markets,"selections_in_page":total_selections,
        "paging":paging,"retrieval":"Use apifootball_odds(fixture=<fixture_id>, page=1) for prices; respect quota."}


def live_odds_manifest(data: dict[str, Any], *, max_fixtures: int = 200,
                       max_markets_per_fixture: int = 300) -> dict[str, Any]:
    """Price-free, navigable summary of an oversized /odds/live reply.

    Live replies carry `odds[]` per fixture (no bookmaker list). Every market
    keeps its bet id so the client can fetch exact prices with
    apifootball_live_odds(fixture=<id>, bet=<bet_id>). Missing lists are
    malformed (fail closed), never counted as zero.
    """
    envelope = data.get("data", data)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("get"), str) \
            or envelope["get"].lstrip("/") != "odds/live":
        raise ValueError("INVALID_LIVE_ODDS_ENVELOPE")
    response = envelope.get("response")
    paging = envelope.get("paging")
    if not isinstance(response, list) or not isinstance(paging, dict):
        raise ValueError("INVALID_LIVE_ODDS_ENVELOPE")
    fixtures = []
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
            # Only an explicit True counts as suspended; missing stays unknown.
            susp = sum(1 for v in values if v.get("suspended") is True)
            unknown = sum(1 for v in values if not isinstance(v.get("suspended"), bool))
            selections += len(values)
            suspended += susp
            if len(markets) < max_markets_per_fixture:
                markets.append({"bet_id": bet_id, "name": bet.get("name"), "selections": len(values),
                                "suspended": susp, "suspension_unknown": unknown})
            else:
                markets_complete = False
        total_markets += len(odds)
        total_selections += selections
        total_suspended += suspended
        if len(fixtures) < max_fixtures:
            status = item["fixture"].get("status") if isinstance(item["fixture"].get("status"), dict) else {}
            fixtures.append({"fixture_id": fixture_id, "elapsed": status.get("elapsed"),
                             "update": item.get("update"), "markets_count": len(odds),
                             "selections": selections, "suspended": suspended, "markets": markets})
    return {"phase": "LIVE", "partial": True, "prices_included": False,
            "reason": "LIVE_ODDS_EXCEEDS_MCP_SIZE_LIMIT",
            "fixture_list_complete": len(fixtures) == len(response),
            "market_list_complete": markets_complete,
            "fixtures": fixtures, "fixtures_in_page": len(response),
            "markets_in_page": total_markets, "selections_in_page": total_selections,
            "suspended_in_page": total_suspended, "paging": paging,
            "retrieval": "Use apifootball_live_odds(fixture=<fixture_id>, bet=<bet_id>) for prices; respect quota."}
