"""FIX-01 (in-memory replies between the 256 KiB MCP cap and the 512 KiB read cap) with the
review P2/P3 fixes: a data-free id manifest whose follow-ups are plain, re-validated provider
parameters (MCP argument names), complete only when every follow-up is provably under the read
cap, never on detailed `ids` slices; invalid ids never listed; undocumented combinations
labelled; order not asserted, changes detected on id sets."""
from __future__ import annotations

import json

import pytest

from apifootball_continuation_kit import (
    PAD, Provider, boot, call, fixture_row, follow_all, validate_follow_up,
)
from bridge.api_football.continuation import (
    IDS_DETAIL_SLICE, STRATEGIES, id_manifest, mcp_arguments, read_cap_navigation, sorted_ids_sha256,
)

D = "2026-11-10"


def three_leagues(n=600):
    leagues = [(39, 2026), (140, 2026), (71, 2026)]
    return [fixture_row(5000 + i, D, *leagues[i % 3]) for i in range(n)]


def test_fixtures_by_date_is_split_by_league_season_and_retrieved_completely():
    provider = Provider(three_leagues())
    tools = boot(provider)
    first = call(tools, "apifootball_fixtures", {"date": D})
    assert first["status"] == "PARTIAL_ID_MANIFEST" and first["network_calls_made"] == 1
    m = first["data"]
    assert m["mode"] == "IN_MEMORY" and m["data_included"] is False and PAD not in json.dumps(first)
    assert m["rows_in_reply"] == m["results_declared"] == 600 and m["results_match"] is True
    assert m["row_ids"] == [5000 + i for i in range(600)]
    assert m["follow_up"]["split_by"] == "league_season" and len(m["follow_up"]["calls"]) == 3
    assert m["complete_retrieval_available"] is True
    assert m["completeness_basis"] == "SUBSET_OF_A_REPLY_UNDER_THE_READ_CAP"
    # date+league+season is not a documented combination: labelled, and a stated condition.
    assert m["follow_up"]["combination_evidence"] == "DOCS_UNVERIFIED"
    assert any("DOCS_UNVERIFIED" in c for c in m["completeness_conditions"])
    assert all(c["arguments"].keys() == {"date", "league", "season"} for c in m["follow_up"]["calls"])
    assert m["order"] == "NOT_ASSERTED" and len(json.dumps(first).encode()) <= 262_144
    rows, problems = follow_all(tools, m)
    assert problems == [] and sorted(rows) == sorted(m["row_ids"]) and len(rows) == len(set(rows)) == 600
    assert len(provider.requests) == 4


def test_follow_ups_survive_a_restart_and_carry_no_token():
    provider = Provider(three_leagues())
    manifest = call(boot(provider), "apifootball_fixtures", {"date": D})["data"]
    blob = json.dumps(manifest["follow_up"])
    assert "token" not in blob and "signature" not in blob
    for c in manifest["follow_up"]["calls"]:
        validate_follow_up("apifootball_fixtures", c["arguments"])
    rows, problems = follow_all(boot(provider), manifest)  # a brand-new process
    assert problems == [] and len(rows) == 600


def test_two_requests_never_mix_their_rows():
    rows_a = three_leagues()
    rows_b = [fixture_row(20_000 + i, "2026-11-11", 39 if i % 2 else 61) for i in range(600)]
    provider = Provider(rows_a + rows_b)
    tools = boot(provider)
    a = call(tools, "apifootball_fixtures", {"date": D})["data"]
    b = call(tools, "apifootball_fixtures", {"date": "2026-11-11"})["data"]
    got_a, pa = follow_all(tools, a)
    got_b, pb = follow_all(tools, b)
    assert pa == pb == [] and not set(got_a) & set(got_b)
    assert set(got_a) == {r["fixture"]["id"] for r in rows_a} and set(got_b) == {r["fixture"]["id"] for r in rows_b}


def test_a_change_between_reads_is_detected_but_a_reorder_is_not_a_change():
    provider = Provider(three_leagues(), reverse_follow_ups=True)
    tools = boot(provider)
    m = call(tools, "apifootball_fixtures", {"date": D})["data"]
    rows, problems = follow_all(tools, m)
    assert problems == [] and len(rows) == 600            # provider reorder: no false alarm
    provider.fixtures = [r for r in provider.fixtures if r["fixture"]["id"] != 5003]
    rows, problems = follow_all(tools, m)
    assert len(problems) == 1 and 5003 not in rows       # the removal is visible, never silent
    reread = call(tools, "apifootball_fixtures", {"date": D})["data"]
    assert reread["row_ids_sorted_sha256"] != m["row_ids_sorted_sha256"] and reread["results_declared"] == 599


def test_league_season_listing_is_split_by_round():
    rows = [fixture_row(i, D, 39, 2026, f"Regular Season - {1 + i % 4}") for i in range(1, 601)]
    m = id_manifest("apifootball_fixtures", {"league": 39, "season": 2026}, {"response": rows, "results": 600})
    assert m["follow_up"]["split_by"] == "round" and len(m["follow_up"]["calls"]) == 4
    assert m["complete_retrieval_available"] is True


# ---- P2: detailed `ids` slices are never claimed complete ------------------------------------

def test_single_group_listing_falls_back_to_detail_slices_never_complete():
    rows = [fixture_row(i, D, 39, 2026, "Regular Season - 1") for i in range(1, 601)]
    m = id_manifest("apifootball_fixtures", {"league": 39, "season": 2026}, {"response": rows, "results": 600})
    f = m["follow_up"]
    assert f["split_by"] == "ids_detail_slices" and f["reason"] == "IDS_DETAIL_REPLY_SIZE_UNPROVEN"
    assert all(len(c["expect_ids"]) <= IDS_DETAIL_SLICE and c["size_provable"] is False for c in f["calls"])
    assert m["complete_retrieval_available"] is False and m["completeness_basis"] is None


def test_an_oversized_ids_request_is_halved_and_a_single_id_is_not_splittable():
    ids = list(range(1, 21))
    m = id_manifest("apifootball_fixtures", {"ids": "-".join(map(str, ids))},
                    {"response": [fixture_row(i) for i in ids], "results": 20})
    assert [c["expect_ids"] for c in m["follow_up"]["calls"]] == [ids[:10], ids[10:]]
    assert m["complete_retrieval_available"] is True  # the same detailed rows, a subset of a reply under the cap
    one = id_manifest("apifootball_fixtures", {"id": 7}, {"response": [fixture_row(7)], "results": 1})
    assert one["complete_retrieval_available"] is False and one["follow_up"]["reason"] == "NOT_SPLITTABLE_SINGLE_FIXTURE_ID"


def test_navigation_for_an_ids_request_halves_it():
    nav = read_cap_navigation("apifootball_fixtures", {"ids": "1-2-3-4-5", "timezone": "UTC"}, reason="SCAN_BOUND_MAX_BYTES")
    assert nav["complete_retrieval_available"] is False and nav["reason"] == "SCAN_BOUND_MAX_BYTES"
    assert [c["arguments"] for c in nav["calls"]] == [{"ids": "1-2-3", "timezone": "UTC"}, {"ids": "4-5", "timezone": "UTC"}]
    single = read_cap_navigation("apifootball_fixtures", {"ids": "9"})
    assert single["calls"] is None and "cannot be split" in single["narrowing"]
    listing = read_cap_navigation("apifootball_fixtures", {"date": D})
    assert listing["calls"] is None and "league" in listing["narrowing"]


# ---- P3-1: ids outside the accepted range are rows without id ---------------------------------

@pytest.mark.parametrize("bad", [0, -5, 2_000_000_001, True, "12", 1.5, None])
def test_invalid_provider_ids_are_never_listed_as_follow_ups(bad):
    rows = [fixture_row(1), fixture_row(2)]
    rows[1]["fixture"]["id"] = bad
    m = id_manifest("apifootball_fixtures", {"date": D}, {"response": rows, "results": 2})
    assert m["rows_without_id"] == 1 and m["complete_retrieval_available"] is False
    assert m["follow_up"]["calls"] == [] and m["follow_up"]["reason"] == "ROWS_WITHOUT_VALID_IDENTIFIER"


# ---- P3-2: undocumented follow-up combinations are labelled -----------------------------------

@pytest.mark.parametrize("tool,request_args,rows,args,status", [
    ("apifootball_players", {"search": "messi", "league": 39},
     [{"player": {"id": 1}}, {"player": {"id": 2}}], {"league": 39, "id": 1}, "DOCS_UNVERIFIED"),
    ("apifootball_injuries", {"league": 39, "season": 2026},
     [{"fixture": {"id": 1}}, {"fixture": {"id": 2}}], {"season": 2026, "fixture": 1}, "DOCS_UNVERIFIED"),
    ("apifootball_leagues", {}, [{"league": {"id": 1}}, {"league": {"id": 2}}], {"id": 1}, "DOCUMENTED_VALID"),
])
def test_follow_up_combinations_are_labelled(tool, request_args, rows, args, status):
    m = id_manifest(tool, request_args, {"response": rows, "results": len(rows)})
    first = m["follow_up"]["calls"][0]
    assert first["arguments"] == args and first["combination_status"] == status
    assert m["follow_up"]["combination_evidence"] == status


def test_order_is_not_asserted_and_the_set_hash_ignores_order():
    a = id_manifest("apifootball_leagues", {}, {"response": [{"league": {"id": 1}}, {"league": {"id": 2}}], "results": 2})
    b = id_manifest("apifootball_leagues", {}, {"response": [{"league": {"id": 2}}, {"league": {"id": 1}}], "results": 2})
    assert a["order"] == "NOT_ASSERTED" and a["row_ids_sorted_sha256"] == b["row_ids_sorted_sha256"]
    assert a["reply_sha256"] != b["reply_sha256"]  # the raw reply did change: both facts are kept
    assert "AS_LISTED" not in json.dumps(a)
    assert sorted_ids_sha256([3, 1, 2]) == sorted_ids_sha256([1, 2, 3])


# ---- P3-6: follow-ups use the MCP argument names ---------------------------------------------

def test_from_to_are_emitted_as_mcp_argument_names_and_the_call_works():
    rows = [fixture_row(i, D, 39, 2026, f"Regular Season - {1 + i % 2}") for i in range(1, 601)]
    provider = Provider(rows)
    tools = boot(provider)
    out = call(tools, "apifootball_fixtures", {"league": 39, "season": 2026, "from_date": D, "to_date": "2026-11-12"})
    calls = out["data"]["follow_up"]["calls"]
    assert all({"from_date", "to_date"} <= c["arguments"].keys() and "from" not in c["arguments"] for c in calls)
    got, problems = follow_all(tools, out["data"])
    assert problems == [] and len(got) == 600
    assert mcp_arguments({"from": D, "to": D, "page": 2}) == {"from_date": D, "to_date": D, "page": 2}


def test_pagination_state_next_parameters_use_mcp_names():
    from bridge.api_football.tools import pagination_state
    st = pagination_state({"current": 1, "total": 2}, {"from": D, "to": D, "fixture": 1})
    assert st["next_parameters"] == {"from_date": D, "to_date": D, "fixture": 1, "page": 2}


@pytest.mark.parametrize("tool,request_args,rows", [
    ("apifootball_leagues", {"season": 2026}, [{"league": {"id": i}} for i in range(1, 30)]),
    ("apifootball_teams", {"league": 39, "season": 2026}, [{"team": {"id": i}} for i in range(1, 30)]),
    ("apifootball_venues", {"country": "Italy"}, [{"id": i} for i in range(1, 30)]),
    ("apifootball_coaches", {"search": "smith"}, [{"id": i} for i in range(1, 30)]),
    ("apifootball_player_profiles", {"page": 101}, [{"player": {"id": i}} for i in range(1, 30)]),
    ("apifootball_injuries", {"league": 39, "season": 2026}, [{"fixture": {"id": i // 2 + 1}} for i in range(30)]),
    ("apifootball_transfers", {"team": 33}, [{"player": {"id": i}} for i in range(1, 30)]),
    ("apifootball_fixtures", {"date": D}, [fixture_row(i, D, 39 + i % 3) for i in range(1, 30)]),
])
def test_every_strategy_yields_valid_calls_covering_each_row_once(tool, request_args, rows):
    m = id_manifest(tool, request_args, {"response": rows, "results": len(rows)})
    assert m["complete_retrieval_available"] is True
    covered = []
    for c in m["follow_up"]["calls"]:
        validate_follow_up(tool, c["arguments"])
        covered += [c["expect_ids"][0]] * c["expect_rows"] if len(c["expect_ids"]) == 1 else c["expect_ids"]
    assert sorted(covered) == sorted(m["row_ids"])


def test_results_mismatch_or_unknown_endpoint_is_never_complete():
    m = id_manifest("apifootball_leagues", {}, {"response": [{"league": {"id": 1}}, {"league": {"id": 2}}], "results": 3})
    assert m["results_match"] is False and m["complete_retrieval_available"] is False
    assert m["follow_up"]["reason"] == "RESULTS_COUNT_MISMATCH"
    n = id_manifest("apifootball_trophies", {"player": 1}, {"response": [{"league": "x"}] * 3, "results": 3})
    assert n["complete_retrieval_available"] is False and n["follow_up"]["reason"] == "NO_DOCUMENTED_SPLIT_FOR_THIS_ENDPOINT"
    dup = id_manifest("apifootball_fixtures", {"date": D}, {"response": [fixture_row(1), fixture_row(1)], "results": 2})
    assert dup["follow_up"]["reason"] == "DUPLICATE_FIXTURE_IDS_IN_REPLY"
    with pytest.raises(ValueError):
        id_manifest("apifootball_fixtures", {}, {"response": {"not": "a list"}})


def test_strategies_cover_the_scannable_paths():
    from bridge.api_football.continuation import ROW_FIELDS
    assert {s.path for s in STRATEGIES.values()} == set(ROW_FIELDS)


def test_e2e_runner_treats_an_id_manifest_as_partial_never_pass():
    from scripts import mcp_e2e_render as e2e
    m = id_manifest("apifootball_fixtures", {"date": D}, {"response": [fixture_row(1, D, 39), fixture_row(2, D, 61)], "results": 2})
    p = {"status": "PARTIAL_ID_MANIFEST", "data": m, "http_status": 200, "endpoint": "/fixtures"}
    assert e2e._classify(p) == "PARTIAL"
    facts = {}
    assert e2e.check_id_manifest(p, facts) == [] and facts["follow_up_calls"] == 2
    assert e2e.check_id_manifest({"status": "PARTIAL_ID_MANIFEST", "data": {"partial": True}}, {}) == ["ID_MANIFEST_NOT_EXPLICIT"]
    assert e2e.stop_reason_for(p) is None
