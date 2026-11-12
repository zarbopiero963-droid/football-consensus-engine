"""Issue #187 S2 (offline): D7 stale ACTIVE rows and decoded fixture statuses; D4 formal closure.

DERIVED views only: raw provider rows stay in `data`, nothing is removed or re-labelled, no extra calls.
"""
import time

import httpx
import pytest

from bridge.sxbet import tools as sx
from test_sxbet_hardening_187 import _ok, make, run, universal

NOW = int(time.time())


# ---------------------------------------------------------------- D7: fixture statuses (official table)
@pytest.mark.parametrize("code,label,phase,final,sweeps", [
    (1, "Not started yet", "NOT_STARTED", False, False), (2, "In progress", "LIVE", False, True),
    (3, "Finished", "FINISHED", True, True), (4, "Cancelled", "CANCELLED", True, True),
    (5, "Postponed", "POSTPONED", False, False), (6, "Interrupted", "INTERRUPTED", False, False),
    (7, "Abandoned", "ABANDONED", True, False), (8, "Coverage lost", "COVERAGE_LOST", False, False),
    (9, "About to start", "NOT_STARTED", False, False),
])
def test_d7_every_official_status_is_decoded(code, label, phase, final, sweeps):
    d = sx.decode_fixture_status(code)
    assert (d["label"], d["phase"], d["final"], d["sweeps_resting_orders"]) == (label, phase, final, sweeps)
    assert d["final_basis"] == {7: "OFFICIAL", 3: "INFERRED_FROM_LABEL", 4: "INFERRED_FROM_LABEL"}.get(code)


@pytest.mark.parametrize("code", [0, 10, "2", 2.0, True])
def test_d7_unknown_status_is_never_guessed(code):
    assert sx.decode_fixture_status(code) == {"code": code, "label": None, "phase": "UNDOCUMENTED_STATUS"}


def test_d7_fixtures_active_flags_final_and_overdue_rows_and_keeps_raw():
    rows = [{"eventId": "L1", "status": 3, "startDate": "2024-09-09T18:00:00Z"},
            {"eventId": "L2", "status": 1, "startDate": "2024-09-09T18:00:00Z"},
            {"eventId": "L3", "status": 1, "startDate": "2096-10-01T18:00:00Z"},
            {"eventId": "L4", "status": 2, "startDate": "2024-09-09T18:00:00Z"}]
    t, calls, _ = make(universal(overrides={"/fixture/active": lambda r: _ok(rows)}))
    out = run(t["sxbet_fixtures_active"](league_id=1))
    v = out["fixture_status_view"]
    assert out["data"] == rows and len(calls) == 1
    assert [x["eventId"] for x in v["final_status_listed_as_active"]["items"]] == ["L1"]
    assert [x["eventId"] for x in v["not_started_but_start_long_past"]["items"]] == ["L2"]
    assert v["counts_by_status"] == {"3": 1, "1": 2, "2": 1} and "upcoming" not in out and "decoded" not in v


def test_d7_fixture_status_decodes_each_event():
    t, _, _ = make(universal(overrides={"/fixture/status": lambda r: _ok({"L1": {"status": 4}, "L2": {"status": 42}})}))
    out = run(t["sxbet_fixture_status"](sportx_event_ids=["L1", "L2"]))
    sd = out["status_decoded"]
    assert sd["label"] == "DERIVED" and sd["by_event"]["L1"]["label"] == "Cancelled"
    assert sd["by_event"]["L2"]["phase"] == "UNDOCUMENTED_STATUS" and out["data"]["L2"] == {"status": 42}


def test_d7_markets_active_flags_stale_active_rows_on_the_page():
    page = {"markets": [{"marketHash": "0x" + "aa" * 32, "status": "ACTIVE", "gameTime": NOW - 400 * 86400},
                        {"marketHash": "0x" + "bb" * 32, "status": "ACTIVE", "gameTime": NOW - 3600},
                        {"marketHash": "0x" + "cc" * 32, "status": "ACTIVE", "gameTime": NOW + 3600},
                        {"marketHash": "0x" + "dd" * 32, "status": "ACTIVE", "gameTime": None}], "nextKey": None}
    t, _, _ = make(universal(overrides={"/markets/active": lambda r: _ok(page)}))
    out = run(t["sxbet_markets_active"](live_only=True))
    v = out["stale_active_view"]
    assert [x["marketHash"] for x in v["rows"]["items"]] == ["0x" + "aa" * 32] and v["scope"] == "THIS_PAGE_ONLY"
    assert out["data"] == page  # nothing removed


def test_d7_unexpected_shapes_are_explicit():
    assert sx.fixtures_view({"x": 1})["fixture_status_view"]["status"] == "UNEXPECTED_SHAPE"
    assert sx.stale_active_markets([1])["stale_active_view"]["status"] == "UNEXPECTED_SHAPE"


# ---------------------------------------------------------------- D4: formal closure (owner-approved, 12-11-2026)
def test_d4_early_expiry_refusal_declares_a_lower_bound_never_an_exact_minimum():
    t, calls, _ = make()
    out = run(t["sxbet_simulate_order"](
        market_hash="0x" + "ab" * 32, betting_outcome_one=True, stake_base_units=5_000_000,
        percentage_odds=str(50 * 10 ** 18), time_in_force="GTC", expiry=NOW + 1, idempotency_key="key-d4-00001"))
    diag = out["expiry_diagnostics"]
    assert out["error"]["code"] == "EXPIRY_TOO_SOON" and calls == []        # refused before any provider call
    assert diag["min_is_lower_bound_only"] is True
    assert diag["components"]["max_betting_delay_ms"] is None              # unknown, never 0
    assert "LOWER BOUND" in sx.sim.expiry_bounds.__doc__


def test_review_af9d5e9_p2_2_missing_and_duplicate_event_ids_are_reported_not_collapsed():
    rows = [{"eventId": "A", "status": 3, "startDate": "2096-01-01T00:00:00Z"},
            {"eventId": "A", "status": 1, "startDate": "2096-01-01T00:00:00Z"},
            {"status": 1}, {"eventId": None, "status": 1}, {"eventId": "None", "status": 1}, "junk"]
    v = sx.fixtures_view(rows, now_unix=NOW)["fixture_status_view"]
    assert v["duplicate_event_ids"]["items"] == ["A"] and v["rows_without_event_id"] == 3 and v["rows"] == 6


def test_review_af9d5e9_p2_3_view_is_compact_for_large_leagues():
    import json
    rows = [{"eventId": f"L{i}", "status": 1, "startDate": "2096-10-01T18:00:00Z", "leagueId": 1,
             "participantOneName": "Team A", "participantTwoName": "Team B", "sportId": 5, "leagueLabel": "X"}
            for i in range(1200)]
    view = sx.fixtures_view(rows, now_unix=NOW)
    assert len(json.dumps(view)) < 8_000  # independent of the number of ordinary rows


def test_review_af9d5e9_p3_2_unjudgeable_rows_are_counted():
    v = sx.fixtures_view([{"eventId": "N", "status": 1, "startDate": "2020-01-01T00:00:00"}],
                         now_unix=NOW)["fixture_status_view"]
    assert v["start_unresolved"]["items"] == [{"eventId": "N", "reason": "TIMEZONE_UNRESOLVED"}]
    assert v["not_started_but_start_long_past"]["total"] == 0  # a naive time is never treated as UTC
    s = sx.stale_active_markets({"markets": [{"status": "ACTIVE", "gameTime": None}, "x"]}, now_unix=NOW)
    assert s["stale_active_view"]["rows_without_int_game_time"] == 2


def test_review_af9d5e9_p3_5_status_9_overdue_is_flagged():
    v = sx.fixtures_view([{"eventId": "S", "status": 9, "startDate": "2024-01-01T00:00:00Z"}],
                         now_unix=NOW)["fixture_status_view"]
    assert [x["eventId"] for x in v["not_started_but_start_long_past"]["items"]] == ["S"]


def test_review_52fcee6_p2a_a_league_full_of_stale_rows_is_not_made_unreadable_by_the_view():
    rows = [{"eventId": f"L{i}", "status": 1, "startDate": "2024-01-01T00:00:00Z", "leagueId": 1,
             "participantOneName": "Team A", "participantTwoName": "Team B", "sportId": 5, "leagueLabel": "League X"}
            for i in range(800)]
    t, _, _ = make(universal(overrides={"/fixture/active": lambda r: _ok(rows)}))
    out = run(t["sxbet_fixtures_active"](league_id=1))
    assert out["status"] == "REAL_API_RESPONSE"  # through call(): raw + view stay under the cap
    lst = out["fixture_status_view"]["not_started_but_start_long_past"]
    assert lst["total"] == 800 and len(lst["items"]) == 50 and lst["omitted"] == 750


def test_review_52fcee6_p3_missing_status_int_vs_str_and_zero_game_time():
    assert sx.decode_fixture_status(None)["phase"] == "STATUS_MISSING"
    v = sx.fixtures_view([{"eventId": "A", "status": 1}, {"eventId": "B", "status": "1"}],
                         now_unix=NOW)["fixture_status_view"]
    assert v["counts_by_status"] == {"1": 1, "non_int:str": 1}
    s = sx.stale_active_markets({"markets": [{"status": "ACTIVE", "gameTime": 0}]}, now_unix=NOW)["stale_active_view"]
    assert s["count"] == 0 and s["rows_without_int_game_time"] == 1


def test_review_52fcee6_p3_4_no_pagination_hint_for_fixture_active():
    big = [{"eventId": f"L{i}", "status": 1, "startDate": "2096-01-01T00:00:00Z", "pad": "x" * 400}
           for i in range(800)]
    t, _, _ = make(universal(overrides={"/fixture/active": lambda r: _ok(big)}))
    out = run(t["sxbet_fixtures_active"](league_id=1))
    assert out["status"] == "PAYLOAD_TOO_LARGE" and "no pagination" in out["error"]["hint"]


def _rows(n, status=3, pad=180):
    return [{"eventId": f"L{i:05d}", "status": status, "startDate": "2024-01-01T00:00:00Z", "leagueId": 1,
             "sportId": 5, "leagueLabel": "League " + "x" * pad} for i in range(n)]


def test_review_d7f186b_p2_1_view_is_omitted_never_the_data_at_the_cap_boundary(monkeypatch):
    """Cap set between the raw answer and raw + view (RED on d7f186b: PAYLOAD_TOO_LARGE)."""
    import json
    rows = _rows(300)
    t, _, _ = make(universal(overrides={"/fixture/active": lambda r: _ok(rows)}))
    full = run(t["sxbet_fixtures_active"](league_id=1))
    with_view = len(json.dumps(full, separators=(",", ":"), ensure_ascii=False).encode())
    raw_only = len(json.dumps({k: v for k, v in full.items() if k != "fixture_status_view"},
                              separators=(",", ":"), ensure_ascii=False).encode())
    monkeypatch.setattr(sx, "_MAX_MCP_BYTES", (raw_only + with_view) // 2)
    out = run(t["sxbet_fixtures_active"](league_id=1))
    assert out["status"] == "REAL_API_RESPONSE" and out["data"] == rows
    assert "fixture_status_view" not in out and out["views_omitted"] == ["fixture_status_view"]
    monkeypatch.setattr(sx, "_MAX_MCP_BYTES", raw_only - 10)  # the data itself does not fit: explicit error
    assert run(t["sxbet_fixtures_active"](league_id=1))["status"] == "PAYLOAD_TOO_LARGE"


def test_review_d7f186b_p3_bounded_count_keys_and_missing_status_apart():
    rows = [{"eventId": "A", "status": {"big": "x" * 500}}, {"eventId": "B"}, {"eventId": "C", "status": None}]
    v = sx.fixtures_view(rows, now_unix=NOW)["fixture_status_view"]
    assert v["counts_by_status"] == {"non_int:dict": 1, "missing": 2}
    assert v["status_missing"]["total"] == 2 and v["undocumented_status"]["items"] == [
        {"eventId": "A", "provider_status": "<dict>"}]


def test_review_79da023_p2_2_upcoming_survives_when_only_the_s2_view_is_too_much(monkeypatch):
    import json
    rows = [{"eventId": f"U{i}", "status": 1, "startDate": "2096-01-01T00:00:00Z", "pad": "y" * 120}
            for i in range(300)]
    t, _, _ = make(universal(overrides={"/fixture/active": lambda r: _ok(rows)}))
    full = run(t["sxbet_fixtures_active"](league_id=1, upcoming_only=True))
    big = len(json.dumps(full, separators=(",", ":"), ensure_ascii=False).encode())
    view = len(json.dumps(full["fixture_status_view"], separators=(",", ":"), ensure_ascii=False).encode())
    monkeypatch.setattr(sx, "_MAX_MCP_BYTES", big - view // 2)  # only the S2 view must go
    out = run(t["sxbet_fixtures_active"](league_id=1, upcoming_only=True))
    assert out["views_omitted"] == ["fixture_status_view"] and out["upcoming"]["rows"] == rows


def test_review_79da023_p2_1_lookup_fields_keep_their_types_under_size_pressure(monkeypatch):
    import json
    hashes = ["0x" + f"{i:064x}" for i in range(30)]
    rows = [{"marketHash": h, "status": "ACTIVE", "pad": "z" * 12000} for h in hashes[:10]]  # 20 of 30 missing
    t, _, _ = make(universal(overrides={"/markets/find": lambda r: _ok(rows)}))
    full = run(t["sxbet_markets_find"](market_hashes=hashes))
    monkeypatch.setattr(sx, "_MAX_MCP_BYTES",
                        len(json.dumps(full, separators=(",", ":"), ensure_ascii=False).encode()) - 5)
    out = run(t["sxbet_markets_find"](market_hashes=hashes))
    # nothing omittable: the typed fields are never replaced, so the answer is the explicit PAYLOAD_TOO_LARGE
    assert out["status"] == "PAYLOAD_TOO_LARGE" and "not_found" not in out
    monkeypatch.undo()
    ok = run(t["sxbet_markets_find"](market_hashes=hashes))
    assert ok["not_found"] == hashes[10:] and ok["lookup_complete"] is True and "views_omitted" not in ok


def _s1_shape(out):
    return {k: v for k, v in out.items() if k not in ("fixture_status_view", "stale_active_view", "status_decoded")}


@pytest.mark.parametrize("upcoming", [False, True])
@pytest.mark.parametrize("extra", [60, 150, 300])
def test_review_e51b7bb_s2_returns_what_s1_returned_just_above_the_s1_size(monkeypatch, upcoming, extra):
    """Cap = size of the S1-shaped answer + a few bytes: S2 returns the data, and the S1 upcoming view stays."""
    import json
    rows = [{"eventId": f"U{i}", "status": 1, "startDate": "2096-01-01T00:00:00Z", "pad": "y" * 120}
            for i in range(300)]
    t, _, _ = make(universal(overrides={"/fixture/active": lambda r: _ok(rows)}))
    full = run(t["sxbet_fixtures_active"](league_id=1, upcoming_only=upcoming))
    s1 = len(json.dumps(_s1_shape(full), separators=(",", ":"), ensure_ascii=False).encode())
    monkeypatch.setattr(sx, "_MAX_MCP_BYTES", s1 + extra)
    out = run(t["sxbet_fixtures_active"](league_id=1, upcoming_only=upcoming))
    assert out["status"] == "REAL_API_RESPONSE" and out["views_omitted"] == ["fixture_status_view"]
    if upcoming:
        assert out["upcoming"]["rows"] == rows
