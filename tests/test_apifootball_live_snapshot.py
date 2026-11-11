"""LIVE FULL MATCH SNAPSHOT (owner requirement 2026-11-11): offline tests on synthetic replies.

The snapshot runs the REAL bridge tools over httpx.MockTransport (synthetic provider bodies, synthetic
ids and prices), under the same write-ahead campaign ledger as AF-05. No network, no database.
"""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from apifootball_campaign_kit import FakeLedger, Provider, Quota, Store, envelope, load, settings

batch = load("apifootball_cert_batch1_snap", "apifootball_cert_batch1.py")
snap = load("apifootball_live_snapshot_t", "apifootball_live_snapshot.py")
FID = 900001
NULL_SCORE = {"home": None, "away": None}
FIXTURE = {"fixture": {"id": FID, "timezone": "UTC", "status": {"long": "Second Half", "short": "2H", "elapsed": 67, "extra": None}},
           "league": {"id": 39, "season": 2026}, "teams": {"home": {"id": 33, "name": "Home"}, "away": {"id": 34, "name": "Away"}},
           "goals": {"home": 1, "away": 0},
           "score": {"halftime": NULL_SCORE, "fulltime": NULL_SCORE, "extratime": NULL_SCORE, "penalty": NULL_SCORE}}
STATS = [{"team": {"id": 33}, "statistics": [{"type": "Shots on Goal", "value": 3}, {"type": "expected_goals", "value": None}]}]
EVENTS = [{"time": {"elapsed": 23, "extra": None}, "team": {"id": 33}, "player": {"id": 7, "name": "P"},
           "assist": {"id": None, "name": None}, "type": "Goal", "detail": "Normal Goal", "comments": None}]
PLAYERS = [{"team": {"id": 33}, "players": [{"player": {"id": 7}, "statistics": [{"games": {"rating": None}}]}]}]
LIVE_ODDS = [{"fixture": {"id": FID, "status": {"long": "Second Half", "elapsed": 67, "seconds": "67:10"}},
              "league": {"id": 39, "season": 2026}, "teams": {"home": {"id": 33, "goals": 1}, "away": {"id": 34, "goals": 0}},
              "status": {"stopped": False, "blocked": False, "finished": False}, "update": "2026-11-11T20:15:00+00:00",
              "odds": [{"id": 36, "name": "Over/Under Line", "values": [
                  {"value": "Over", "odd": "1.85", "handicap": "2.5", "main": True, "suspended": False},
                  {"value": "Under", "odd": "1.95", "handicap": "2.5", "main": True, "suspended": True}]}]}]
COVERAGE = [{"league": {"id": 39}, "seasons": [{"year": 2026, "coverage": {
    "fixtures": {"events": True, "lineups": False, "statistics_fixtures": True, "statistics_players": True}, "odds": True}}]}]
BODIES = {"/fixtures": [FIXTURE], "/fixtures/statistics": STATS, "/fixtures/events": EVENTS, "/fixtures/lineups": [],
          "/fixtures/players": PLAYERS, "/odds/live": LIVE_ODDS, "/leagues": COVERAGE}


def provider(overrides=None):
    bodies = {**BODIES, **(overrides or {})}

    def handler_for(path):
        def handle(request):
            body = bodies[path]
            if callable(body):
                return body(request)
            # Number formatting kept on purpose in the text: identity is checked on parsed values.
            return httpx.Response(200, content=json.dumps(envelope(path, body)).encode())
        return handle
    return Provider(script={p: handler_for(p) for p in bodies})


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        self.t += 1
        return f"2026-11-11T20:{15 + self.t // 60:02d}:{self.t % 60:02d}+00:00"


def run(store=None, prov=None, *, cap=8, fixture_id=FID, sid="snap-test-0000001"):
    store = store or Store()
    prov = prov or provider()
    report = asyncio.run(batch.execute_snapshot(snapshot_id=sid, fixture_id=fixture_id, settings=settings(),
                                                ledger=FakeLedger(store), inner_transport=prov.transport(),
                                                quota=Quota(), cap=cap, clock=Clock()))
    return report, store, prov


def test_full_snapshot_is_certified_with_explicit_non_simultaneity():
    report, store, prov = run()
    req = report["requirements"]
    assert report["overall"] == "SNAPSHOT_CERTIFIED", report["incomplete_reasons"]
    assert len(prov.sent) == len(store.attempts) == 7 <= 8
    assert req["SNAP-RAW-PRESERVED"] == req["SNAP-FIELD-COMPLETENESS"] == req["SNAP-NULL-PRESERVED"] == "PASS"
    assert req["SNAP-IDENTITY-WITH-ORIGINAL"] == "PASS" and req["SNAP-TRANSFORMATIONS-LISTED"] == "PASS"
    assert req["SNAP-TIMESTAMPS"] == "PASS" and req["SNAP-NOT-SIMULTANEOUS-DECLARED"] == "PASS"
    sim = report["simultaneity"]
    assert sim["sources_simultaneous"] is False and "NOT simultaneous" in sim["statement"]
    assert sim["collection_window"]["duration_seconds"] > 0
    for entry in report["sources"].values():
        t = entry["timing"]
        assert t["requested_at_utc"].endswith("+00:00") and t["requested_at_europe_rome"].endswith("+01:00")
        assert entry["identity_with_original"] in ("VERIFIED", "NOT_APPLICABLE")
        assert entry["bridge_transformations"]


def test_match_state_is_as_published_nulls_kept_and_ht_never_derived():
    report, _, _ = run()
    state = report["match_state_as_published"]
    assert state["status_elapsed"] == 67 and state["status_extra"] is None
    assert "not real time" in state["minute_note"]
    assert state["score"]["halftime"] == {"home": None, "away": None}  # not derived from the 23' goal
    stats = report["sources"]["statistics"]["mcp_reply_as_received"]["data"]["response"][0]["statistics"]
    assert stats[1]["value"] is None  # xG absent stays null, never 0
    assert report["requirements"]["SNAP-NULL-PRESERVED"] == "PASS"


def test_lineups_not_offered_by_coverage_is_not_an_mcp_defect():
    report, _, _ = run()
    lineups = report["sources"]["lineups"]
    assert lineups["verdict"] == "PROVIDER_NOT_OFFERED" and "coverage" in lineups["availability_evidence"]
    assert report["requirements"]["SNAP-AVAILABILITY-CLASSIFIED"] == "PASS"


def test_empty_lineups_without_coverage_evidence_stay_empty_valid():
    cov = copy.deepcopy(COVERAGE)
    cov[0]["seasons"][0]["coverage"]["fixtures"]["lineups"] = True
    report, _, _ = run(prov=provider({"/leagues": cov}))
    assert report["sources"]["lineups"]["verdict"] == "EMPTY_VALID"


def test_live_odds_as_published_with_suspensions_and_market_label():
    report, _, _ = run()
    odds = report["sources"]["live_odds"]
    assert odds["label"] == "MARKET_PRICES_AS_PUBLISHED_NOT_MODEL_PROBABILITIES"
    values = odds["mcp_reply_as_received"]["data"]["response"][0]["odds"][0]["values"]
    assert values[1]["suspended"] is True and values[0]["odd"] == "1.85"
    assert odds["provider_update_timestamps"] == ["2026-11-11T20:15:00+00:00"]
    assert report["requirements"]["SNAP-LIVE-ODDS-AS-PUBLISHED"] == "PASS"


def test_partial_manifest_source_never_certifies_and_lists_recovery_ids():
    big = copy.deepcopy(LIVE_ODDS)
    market = big[0]["odds"][0]
    big[0]["odds"] = [dict(market, id=1000 + i) for i in range(2500)]
    report, _, _ = run(prov=provider({"/odds/live": big}))
    assert report["sources"]["live_odds"]["verdict"] == "PARTIAL"
    assert report["overall"] == "SNAPSHOT_PARTIAL" and report["partial_sources"] == ["live_odds"]
    assert report["sources"]["live_odds"]["recovery"]["manifest"]
    assert report["requirements"]["SNAP-PARTIAL-NOT-CERTIFIED"] == "PASS"


def test_derived_goal_check_flags_a_mismatch_without_rewriting():
    fixture = copy.deepcopy(FIXTURE)
    fixture["goals"] = {"home": 2, "away": 0}
    report, _, _ = run(prov=provider({"/fixtures": [fixture]}))
    check = report["derived_checks"][0]
    assert check["label"] == "DERIVED" and check["result"] == "MISMATCH" and check["provider_data_modified"] is False
    assert report["match_state_as_published"]["goals"] == {"home": 2, "away": 0}
    assert report["requirements"]["SNAP-DERIVED-CONSISTENCY"] == "PASS"


def test_plan_excluded_source_is_a_provider_limitation():
    def plan_error(request):
        return httpx.Response(200, json=envelope("/fixtures/statistics", [], errors={"plan": "x"}))
    report, _, _ = run(prov=provider({"/fixtures/statistics": plan_error}))
    assert report["sources"]["statistics"]["verdict"] == "PLAN_EXCLUDED"
    assert report["overall"] != "SNAPSHOT_FAILED"


def test_budget_cap_closes_the_snapshot_incomplete_without_more_attempts():
    report, store, prov = run(cap=3)
    assert len(prov.sent) == len(store.attempts) == 3
    assert report["overall"] == "SNAPSHOT_INCOMPLETE"
    assert "STOPPED:BUDGET_CAP_REACHED" in report["incomplete_reasons"]
    assert report["requirements"]["SNAP-BUDGET-CAP-CLOSES-INCOMPLETE"] == "PASS"


def test_timeout_is_counted_and_the_snapshot_is_incomplete():
    def timeout(request):
        raise httpx.ReadTimeout("t", request=request)
    report, store, prov = run(prov=provider({"/fixtures/events": timeout}))
    assert len(store.attempts) == len(prov.sent) == 7
    assert report["overall"] == "SNAPSHOT_INCOMPLETE" and "SOURCE_ERROR:events" in report["incomplete_reasons"]


def test_retry_errors_never_exceed_the_ceiling():
    report, store, prov = run(prov=provider({"/fixtures/players": lambda r: httpx.Response(503)}))
    assert len(store.attempts) == len(prov.sent) <= 8


def test_restart_with_same_snapshot_id_never_gets_a_new_budget():
    store = Store()

    class Crash(BaseException):
        pass

    def crash(request):
        raise Crash()
    with pytest.raises(Crash):
        run(store, provider({"/fixtures/lineups": crash}))
    first = len(store.attempts)
    report, store, prov = run(store)
    assert first + len(prov.sent) == len(store.attempts) <= 8
    assert report["overall"] == "SNAPSHOT_INCOMPLETE"
    assert "STOPPED:RESTART_CALLS_ALREADY_ATTEMPTED_NOT_RETRIED" in report["incomplete_reasons"]
    again = provider()
    run(store, again)
    assert len(store.attempts) <= 8


def _recorded():
    report, _, _ = run()
    return {name: {"tool": e["tool"], "arguments": e["arguments"], "mcp_reply": e["mcp_reply_as_received"],
                   "requested_at_utc": e["timing"]["requested_at_utc"], "completed_at_utc": e["timing"]["completed_at_utc"],
                   **({"provider_body_text": e["provider_original_body"]} if "provider_original_body" in e else {})}
            for name, e in report["sources"].items()}


def test_tampered_reply_is_detected_field_by_field():
    sources = _recorded()
    stats = sources["statistics"]["mcp_reply"]["data"]["response"][0]["statistics"]
    stats[1]["value"] = 0  # null turned into 0
    del sources["events"]["mcp_reply"]["data"]["response"][0]["comments"]  # a field lost
    report = snap.build_report("snap-x-00000001", FID, sources, budget_cap=8, attempts_used=7)
    assert report["sources"]["statistics"]["completeness"]["changed_in_report"]
    assert report["sources"]["events"]["completeness"]["missing_in_report"]
    assert report["requirements"]["SNAP-FIELD-COMPLETENESS"] == "FAIL"
    assert report["requirements"]["SNAP-NULL-PRESERVED"] == "FAIL"
    assert report["overall"] == "SNAPSHOT_FAILED"


def test_without_transport_capture_identity_is_declared_not_verifiable():
    sources = _recorded()
    for src in sources.values():
        src.pop("provider_body_text", None)
    report = snap.build_report("snap-x-00000002", FID, sources, budget_cap=8, attempts_used=7)
    assert report["requirements"]["SNAP-IDENTITY-WITH-ORIGINAL"] == "NOT_VERIFIABLE"
    assert report["overall"] == "SNAPSHOT_INCOMPLETE"
    assert any(r.startswith("IDENTITY_NOT_VERIFIABLE:") for r in report["incomplete_reasons"])


def test_naive_timestamps_are_not_guessed():
    sources = _recorded()
    sources["events"]["requested_at_utc"] = "2026-11-11T20:15:00"
    report = snap.build_report("snap-x-00000003", FID, sources, budget_cap=8, attempts_used=7)
    assert report["requirements"]["SNAP-TIMESTAMPS"] == "FAIL"


def test_every_source_must_be_for_the_same_fixture():
    sources = _recorded()
    sources["events"]["arguments"] = {"fixture": FID + 1}
    with pytest.raises(ValueError):
        snap.build_report("snap-x-00000004", FID, sources, budget_cap=8, attempts_used=7)


def test_snapshot_artifact_holds_no_secret(tmp_path):
    report, _, _ = run()
    path = tmp_path / "live_full_match_snapshot.json"
    path.write_text(json.dumps(report))
    from apifootball_campaign_kit import KEY
    assert batch.scan_for_secrets([path], [KEY]) == []
