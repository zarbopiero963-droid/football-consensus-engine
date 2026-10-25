from bridge.api_football.five_point_evidence import (
    pagination_coverage, extract_odds, field_types, live_delta,
)


def test_all_provider_pages_required_even_past_ten():
    pages = [{"page": i, "status": "OBSERVED", "pagination": {"total": 12}}
             for i in range(1, 11)]
    assert pagination_coverage(pages)["next_missing_page"] == 11
    assert pagination_coverage(pages)["status"] == "INCOMPLETE"
    pages.extend({"page": i, "status": "OBSERVED", "pagination": {"total": 12}}
                 for i in (11, 12))
    assert pagination_coverage(pages)["status"] == "COMPLETE"


def test_odds_complete_nested_extraction_and_separate_namespaces():
    payload = {"response": [{"fixture": {"id": 20}, "bookmakers": [
        {"id": 2, "name": "A", "bets": [{"id": 1, "name": "Totals", "values": [
            {"value": "Over 2.5", "odd": "1.90"},
            {"value": "Under 2.5", "odd": "1.91", "handicap": "-0.5"},
        ]}]}]}]}
    rows = extract_odds(payload, "PREMATCH")
    assert len(rows) == 2
    assert rows[0]["odd"] == "1.90"
    assert rows[1]["handicap"] == "-0.5"
    live_payload = {"response": [{"fixture": {"id": 20}, "odds": [{"id": 1, "name": "Live totals", "values": [{"value": "Over", "odd": "1.7"}]}]}]}
    assert rows[0]["phase"] != extract_odds(live_payload, "LIVE")[0]["phase"]


def test_field_union_keeps_null_and_zero():
    fields = field_types([{"x": None}, {"x": 0}, {"x": ""}])
    assert fields["$.x"] == ["integer", "null", "string"]


def test_live_delta_distinguishes_quote_and_suspension():
    a = [{"phase": "LIVE", "fixture_id": 1, "bookmaker_id": 2,
          "market_id": 3, "selection": "Home", "odd": "2.1",
          "suspended": False}]
    b = [{**a[0], "odd": "2.2", "suspended": True}]
    result = live_delta(a, b)
    assert result["status"] == "CHANGE_OBSERVED"
    assert {x["field"] for x in result["changes"]} == {"odd", "suspended"}
    assert live_delta(a, a)["status"] == "NOT_OBSERVED_DURING_WINDOW"


def test_live_selection_level_suspend_and_main_are_preserved():
    payload = {"response": [{"fixture": {"id": 77}, "status": {"stopped": False},
        "odds": [{"id": 5, "name": "Live totals", "values": [
            {"value": "Over", "odd": "0", "suspended": True, "main": None}]}]}]}
    rows = extract_odds(payload, "LIVE")
    assert len(rows) == 1
    assert rows[0]["suspended"] is True
    assert rows[0]["main"] is None
    assert rows[0]["odd"] == "0"
    assert rows[0]["fixture_status"] == {"stopped": False}
    assert rows[0]["market_namespace"] == "LIVE"
