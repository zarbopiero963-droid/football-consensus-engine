"""MatchPilot capability matrix: no evidence inflation or side effects."""
from __future__ import annotations

from bridge.api_football.catalog import ALLOWED_PATHS
from bridge.api_football.capabilities import CAPABILITIES, capability_gap_report


def test_34_critical_requirements_are_fully_named_and_never_autocertified():
    result = capability_gap_report()
    assert len(CAPABILITIES) == result["requirements"] == 34
    assert result["real_provider_calls"] == 0
    assert result["verified_replacements"] == 0
    assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert all(x["replacement_status"] == "UNKNOWN" for x in result["rows"])
    assert all(x["evidence_level"] == "DOCUMENTARY_ONLY_NO_REAL_API_DATA"
               for x in result["rows"])
    assert all(x["license_verified"] is False for x in result["rows"])


def test_all_endpoint_candidates_are_allowlisted_but_never_field_verified():
    report = capability_gap_report()
    assert all(set(row["api_football_candidate_paths"]).issubset(ALLOWED_PATHS)
               for row in report["rows"])
    assert all(not row["semantic_equivalence_verified"] for row in report["rows"])


def test_no_xg_xgot_attack_or_big_chance_false_equivalence():
    names = {"shot_based_xg", "xgot", "xpts", "big_chances", "dangerous_attacks", "attacks"}
    rows = {x["requirement"]: x for x in capability_gap_report()["rows"]}
    for name in names:
        assert rows[name]["api_football_candidate_paths"] == []
        assert rows[name]["documentation_gap"].startswith("NOT_DOCUMENTED")
        assert rows[name]["replacement_status"] == "UNKNOWN"


def test_totalcorner_is_not_reactivated_through_gap_analysis():
    r = capability_gap_report()
    assert r["totalcorner_enabled"] is False
    for row in r["rows"]:
        assert row["totalcorner_enabled"] is False
    assert r["rows"]


def test_pit_cannot_be_certified_from_provider_quote_listing():
    rows = {x["requirement"]: x for x in capability_gap_report()["rows"]}
    for key in (
        "prematch_odds", "live_odds", "odds_movement",
        "opening_closing_odds", "historical_fixture_depth",
        "original_observed_at_pit",
    ):
        assert rows[key]["pit_proof_required"] is True
        assert rows[key]["replacement_status"] == "UNKNOWN"
        assert not rows[key]["freshness_verified"]


def test_predictions_are_not_price_probabilities_or_native_xg():
    rows = {x["requirement"]: x for x in capability_gap_report()["rows"]}
    assert rows["predictions"]["api_football_candidate_paths"] == ["/predictions"]
    assert "NOT_EQUIVALENT" in rows["predictions"]["documentation_gap"]
    assert not rows["predictions"]["semantic_equivalence_verified"]
