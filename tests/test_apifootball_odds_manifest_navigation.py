"""FIX-05 (G08): navigable, price-free odds manifests. Bookmakers, pre-match markets and live
markets in SEPARATE namespaces; ids, names and selections; Over/Under and handicap lines;
selection status only as published; current/total page and strictly narrower follow-up
filters (no loop). Never an invented selection, status or odd."""
from __future__ import annotations

import asyncio
import json

import pytest

from bridge.api_football.config import Settings
from bridge.api_football.contracts import validate
from bridge.api_football.odds_manifest import DETAIL_LEVELS, live_odds_manifest, odds_page_manifest
from bridge.api_football.tools import register_tools

PRICE = "7.77"


def prematch(n_fixtures=1, books=(8, 11), bets=((5, "Goals Over/Under"), (1, "Match Winner")), page=(1, 3)):
    def values(bet_id):
        if bet_id == 5:
            return [{"value": f"{side} {line}", "odd": PRICE} for line in ("1.5", "2.5", "3.5") for side in ("Over", "Under")]
        return [{"value": v, "odd": PRICE} for v in ("Home", "Draw", "Away")]
    return {"get": "odds", "errors": [], "results": n_fixtures, "paging": {"current": page[0], "total": page[1]},
            "response": [{"fixture": {"id": 900 + f}, "update": "2026-11-10T10:00:00+00:00",
                          "bookmakers": [{"id": b, "name": f"Book{b}", "bets": [
                              {"id": k, "name": name, "values": values(k)} for k, name in bets]} for b in books]}
                         for f in range(n_fixtures)]}


def live(selections=None):
    selections = selections if selections is not None else [
        {"value": "Over", "odd": PRICE, "handicap": "2.5", "main": True, "suspended": False},
        {"value": "Under", "odd": None, "handicap": "2.5", "main": None, "suspended": True},
        {"value": "Over", "odd": PRICE, "handicap": "3.5"},  # no status published
    ]
    return {"get": "odds/live", "errors": [], "results": 1, "paging": {"current": 1, "total": 1},
            "response": [{"fixture": {"id": 77, "status": {"elapsed": 61}}, "update": "2026-11-10T21:00:00+00:00",
                          "odds": [{"id": 36, "name": "Over/Under Line", "values": selections}]}]}


def no_odd_anywhere(node):
    if isinstance(node, dict):
        assert "odd" not in node
        for v in node.values():
            no_odd_anywhere(v)
    elif isinstance(node, list):
        for v in node:
            no_odd_anywhere(v)


def test_prematch_manifest_lists_bookmakers_markets_selections_and_lines_without_prices():
    m = odds_page_manifest(prematch(), request={"fixture": 900})
    assert m["phase"] == m["market_namespace"] == "PRE_MATCH" and m["bet_id_catalog"] == "apifootball_bets"
    books = m["fixture_ids"][0]["bookmaker_list"]
    assert [b["bookmaker_id"] for b in books] == [8, 11] and books[0]["name"] == "Book8"
    ou = books[0]["bets"][0]
    assert ou["bet_id"] == 5 and ou["name"] == "Goals Over/Under" and ou["selections"] == 6
    assert [v["value"] for v in ou["values"]] == ["Over 1.5", "Under 1.5", "Over 2.5", "Under 2.5", "Over 3.5", "Under 3.5"]
    # Pre-match selections publish no status: unknown, never "open" or "suspended".
    assert ou["suspension_unknown"] == 6 and ou["suspended"] == ou["open"] == 0
    assert "suspended" not in ou["values"][0] and "handicap" not in ou["values"][0]
    no_odd_anywhere(m)
    assert PRICE not in json.dumps(m)


def test_prematch_follow_up_narrows_step_by_step_and_never_loops():
    by_fixture = odds_page_manifest(prematch(n_fixtures=3), request={"league": 39, "season": 2026})
    assert by_fixture["follow_up"]["split_by"] == "fixture"
    assert [c["arguments"] for c in by_fixture["follow_up"]["calls"]] == [{"fixture": 900}, {"fixture": 901}, {"fixture": 902}]
    assert by_fixture["next_page_arguments"] == {"league": 39, "season": 2026, "page": 2}
    by_book = odds_page_manifest(prematch(), request={"fixture": 900})
    assert [c["arguments"] for c in by_book["follow_up"]["calls"]] == [{"fixture": 900, "bookmaker": 8},
                                                                        {"fixture": 900, "bookmaker": 11}]
    assert by_book["follow_up"]["calls"][0]["expect"] == {"fixtures": 1, "bookmakers": 1, "markets": 2, "selections": 9}
    by_bet = odds_page_manifest(prematch(books=(8,)), request={"fixture": 900, "bookmaker": 8})
    assert [c["arguments"] for c in by_bet["follow_up"]["calls"]] == [{"fixture": 900, "bookmaker": 8, "bet": 5},
                                                                       {"fixture": 900, "bookmaker": 8, "bet": 1}]
    bet_only = odds_page_manifest(prematch(), request={"fixture": 900, "bet": 5, "timezone": "Europe/Rome"})
    assert bet_only["follow_up"]["calls"][0]["arguments"] == {"fixture": 900, "bookmaker": 8, "bet": 5,
                                                              "timezone": "Europe/Rome"}
    narrowest = odds_page_manifest(prematch(books=(8,), bets=((5, "Goals Over/Under"),)),
                                   request={"fixture": 900, "bookmaker": 8, "bet": 5})
    assert narrowest["follow_up"]["splittable"] is False and narrowest["follow_up"]["calls"] == []
    for m in (by_fixture, by_book, by_bet, bet_only):
        for c in m["follow_up"]["calls"]:
            assert c["arguments"] != {}
            validate("apifootball_odds", c["arguments"])


def test_live_manifest_keeps_live_namespace_status_null_and_lines_as_published():
    m = live_odds_manifest(live(), request={"league": 39})
    assert m["phase"] == m["market_namespace"] == "LIVE" and m["bet_id_catalog"] == "apifootball_live_bets"
    market = m["fixtures"][0]["markets"][0]
    assert market["bet_id"] == 36 and market["lines"] == ["2.5", "3.5"]
    assert (market["suspended"], market["open"], market["suspension_unknown"]) == (1, 1, 1)
    assert market["values"] == [
        {"value": "Over", "handicap": "2.5", "main": True, "suspended": False},
        {"value": "Under", "handicap": "2.5", "main": None, "suspended": True},  # nulls stay null
        {"value": "Over", "handicap": "3.5"},                                    # absent stays absent
    ]
    assert m["follow_up"]["calls"] == [{"arguments": {"fixture": 77}, "expect": {"fixtures": 1, "markets": 1, "selections": 3}}]
    by_bet = live_odds_manifest(live(), request={"fixture": 77})
    assert by_bet["follow_up"]["calls"][0]["arguments"] == {"fixture": 77, "bet": 36}
    assert live_odds_manifest(live(), request={"fixture": 77, "bet": 36})["follow_up"]["splittable"] is False
    no_odd_anywhere(m)


def test_prematch_and_live_ids_never_cross():
    pre = odds_page_manifest(prematch(), request={"fixture": 900})
    lv = live_odds_manifest(live(), request={"fixture": 77})
    assert {c["arguments"].get("bet") for c in lv["follow_up"]["calls"]} == {36}
    assert pre["follow_up"]["tool"] == "apifootball_odds" and lv["follow_up"]["tool"] == "apifootball_live_odds"
    with pytest.raises(ValueError):
        live_odds_manifest(prematch())
    with pytest.raises(ValueError):
        odds_page_manifest(live())


@pytest.mark.parametrize("detail", DETAIL_LEVELS)
def test_every_detail_level_keeps_complete_counts(detail):
    m = odds_page_manifest(prematch(n_fixtures=2), request={"league": 39, "season": 2026}, detail=detail)
    assert m["detail"] == detail and m["fixtures_in_page"] == 2
    assert m["market_groups_in_page"] == 8 and m["selections_in_page"] == 36
    assert len(m["follow_up"]["calls"]) == 2


@pytest.mark.parametrize("broken", ["bookmaker_id", "bet_id", "value_not_dict"])
def test_missing_ids_fail_closed(broken):
    data = prematch()
    if broken == "bookmaker_id":
        del data["response"][0]["bookmakers"][0]["id"]
    elif broken == "bet_id":
        data["response"][0]["bookmakers"][0]["bets"][0]["id"] = "5"
    else:
        data["response"][0]["bookmakers"][0]["bets"][0]["values"][0] = "Over 2.5"
    with pytest.raises(ValueError):
        odds_page_manifest(data)


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


class _Client:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, path, params=None):
        return {"data": self.payload, "http_status": 200, "cache_status": "MISS",
                "quota_remaining_provider_day": 1, "quota_remaining_provider_minute": 1}


def test_tool_picks_the_most_detailed_level_that_fits():
    # 30 bookmakers x 2 bets on 120 fixtures: SELECTIONS does not fit, a coarser level does.
    payload = prematch(n_fixtures=120, books=tuple(range(1, 31)))
    reg = _Registry()
    register_tools(reg, Settings(enabled=True, api_key="fake", live_authorized=True), client=_Client(payload))
    out = asyncio.run(reg.tools["apifootball_odds"](league=39, season=2026))
    assert out["status"] == "PARTIAL_ODDS_MANIFEST"
    assert out["data"]["detail"] in DETAIL_LEVELS[1:]
    assert out["data"]["selections_in_page"] == 120 * 30 * 9
    assert out["data"]["response_sha256"] and out["pagination"] == {"current": 1, "total": 3}
    assert len(json.dumps(out).encode()) <= 262_144
    no_odd_anywhere(out["data"])
