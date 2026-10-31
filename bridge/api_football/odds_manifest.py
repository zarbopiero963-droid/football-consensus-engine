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
