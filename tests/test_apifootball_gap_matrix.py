"""PR-04 prep: offline 325-row contract; no provider or MatchPilot connections."""
from __future__ import annotations

from copy import deepcopy

import pytest

from bridge.api_football.gap_matrix import (
    FptField, GapMatrixError, FIELD_COUNT_REQUIRED, VALID_REPLACEMENT,
    COVERAGE_STATUS, build_gap_matrix, audit_candidate_coverage,
)


def _fake_registry():
    # Synthetic field names only: NOT an assertion we recovered 325 actual
    # MatchPilot field names. True FR-01 must load a verified read-only export.
    rows = [
        FptField(f"Sample_Field_{i:03d}", "unclassified", "POSTMATCH_OUTCOME")
        for i in range(325)
    ]
    rows[0] = FptField("xG_Home_FT", "expected_goals", "POSTMATCH_OUTCOME")
    rows[1] = FptField("Odd_1_FT", "market", "PREMATCH_MARKET_UNTIMED")
    rows[2] = FptField("Corners_Home_HT", "corners", "POSTMATCH_OUTCOME")
    rows[3] = FptField("Home", "identity", "PREMATCH_IDENTITY")
    rows[4] = FptField("Total_Shots_Home_FT", "shooting", "POSTMATCH_OUTCOME")
    rows[5] = FptField("Big_Chances_Home_2T", "expected_goals", "POSTMATCH_OUTCOME")
    return rows


def test_all_325_fields_are_preserved_and_never_auto_certified():
    result = build_gap_matrix(_fake_registry())
    assert result["expected_fields"] == result["fields_inspected"] == 325
    assert len(result["records"]) == 325
    assert result["real_provider_calls"] == 0
    assert result["totalcorner_enabled"] is False
    assert result["replacement_certified"] is False
    assert result["counts"]["UNKNOWN"] == 325
    assert set(result["counts"]) == VALID_REPLACEMENT
    assert all(result["counts"][name] == 0 for name in VALID_REPLACEMENT if name != "UNKNOWN")
    assert result["evidence_level"] == COVERAGE_STATUS
    assert len(result["registry_metadata_sha256"]) == 64
    summary = audit_candidate_coverage(result)
    assert summary["direct_replacements_certified"] == 0
    assert summary["replacement_verdict"] == "INSUFFICIENT_EVIDENCE"


def test_market_is_never_pit_verified_due_to_unknown_prematch_quote_timestamp():
    report = build_gap_matrix(_fake_registry())
    row = next(row for row in report["records"] if row["fpt_field"] == "Odd_1_FT")
    assert row["replacement_status"] == "UNKNOWN"
    assert row["candidate_apifootball_endpoints"] == [
        "/odds", "/odds/live", "/odds/bets", "/odds/live/bets",
    ]
    assert row["timestamp_pit_verified"] is False
    assert "HISTORICAL_MARKET_PIT_NOT_PROVEN" in row["warnings"]


def test_xg_and_big_chances_not_converted_to_provider_prediction_or_odds():
    report = build_gap_matrix(_fake_registry())
    for name in ("xG_Home_FT", "Big_Chances_Home_2T"):
        row = next(x for x in report["records"] if x["fpt_field"] == name)
        assert row["candidate_apifootball_endpoints"] == []
        assert row["replacement_status"] == "UNKNOWN"
        assert "PROVIDER_PREDICTIONS_NOT_SHOT_BASED_XG_EQUIVALENT" in row["warnings"]


def test_totalcorner_is_only_candidate_and_remains_suspended():
    report = build_gap_matrix(_fake_registry())
    row = next(x for x in report["records"] if x["fpt_field"] == "Corners_Home_HT")
    assert row["totalcorner_candidate_only"] is True
    assert row["totalcorner_operational"] is False
    assert row["replacement_status"] == "UNKNOWN"
    assert report["totalcorner_enabled"] is False


def test_matrix_generation_is_deterministic_under_input_reordering():
    one = build_gap_matrix(_fake_registry())
    two = build_gap_matrix(list(reversed(_fake_registry())))
    assert one == two


@pytest.mark.parametrize("count", [0, 1, 324, 326])
def test_partial_or_inflated_registry_fails_closed(count):
    with pytest.raises(GapMatrixError, match="FPT_FIELD_REGISTRY_INCOMPLETE"):
        rows = _fake_registry()
        if count > len(rows):
            rows.extend(FptField(f"Extra_Field_{i}", "unclassified", "POSTMATCH_OUTCOME")
                        for i in range(count - len(rows)))
        build_gap_matrix(rows[:count])


def test_duplicate_fields_fail_closed():
    rows = _fake_registry()
    rows[1] = rows[0]
    with pytest.raises(GapMatrixError, match="DUPLICATE_FPT_FIELD"):
        build_gap_matrix(rows)


@pytest.mark.parametrize("field", [
    FptField("xg_Home_FT", "expected_goals", "POSTMATCH_OUTCOME"),
    FptField("Corners_Away_2T", "corners", "POSTMATCH_OUTCOME"),
])
def test_family_timing_metadata_alone_never_certifies_equivalence(field):
    rows = _fake_registry()
    rows[0] = field
    report = build_gap_matrix(rows)
    assert all(r["replacement_status"] == "UNKNOWN" for r in report["records"])


def test_reject_invalid_metadata_and_untrusted_field_name():
    for name in ("", "bad\nfield", "bad field", "x" * 151):
        with pytest.raises(GapMatrixError, match="INVALID_FPT_FIELD_NAME"):
            FptField(name, "market", "PREMATCH_MARKET_UNTIMED")
    with pytest.raises(GapMatrixError, match="UNRECOGNIZED_FPT_FAMILY"):
        FptField("Test", "magic_xg", "POSTMATCH_OUTCOME")
    with pytest.raises(GapMatrixError, match="UNRECOGNIZED_FPT_TIMING_CLASS"):
        FptField("Test", "market", "PREKICKOFF_CERTIFIED")


def test_positive_claim_in_unverified_matrix_must_be_rejected():
    result = build_gap_matrix(_fake_registry())
    result["records"][0]["replacement_status"] = "DIRECT_REPLACEMENT"
    with pytest.raises(GapMatrixError, match="UNSUPPORTED_POSITIVE_REPLACEMENT_CLAIM"):
        audit_candidate_coverage(result)


def test_empty_or_corrupted_matrix_prohibited():
    report = build_gap_matrix(_fake_registry())
    report["records"] = deepcopy(report["records"][:200])
    with pytest.raises(GapMatrixError, match="FPT_MATRIX_INCOMPLETE"):
        audit_candidate_coverage(report)
