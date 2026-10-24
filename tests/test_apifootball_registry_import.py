"""PR-04: pinned, read-only FPT manifest parser and 37-indicator census.

All manifests are synthetic test fixtures, never a fabricated historical
registry or real provider evidence.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from bridge.api_football.registry_import import import_registry_manifest
from bridge.api_football.gap_matrix import build_gap_matrix, GapMatrixError
from bridge.api_football.indicators import INDICATORS, indicator_gap_report


COMMIT = "a" * 40
SOURCE = "docs/reports/fpt-325-field-registry.json"


def _bytes(*, count=325, source=SOURCE, source_repo="zarbopiero963-droid/MatchPilot",
           schema="fpt-schema-4", commit=COMMIT, duplicate=False):
    records = [
        {"field_name": f"Fake_FPT_Field_{i:03d}", "family": "unclassified",
         "timing_class": "POSTMATCH_OUTCOME"}
        for i in range(count)
    ]
    if duplicate and len(records) > 1:
        records[1]["field_name"] = records[0]["field_name"]
    doc = {
        "source_repository": source_repo,
        "source_commit": commit,
        "source_path": source,
        "schema_version": schema,
        "fields": records,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def _parse(data):
    return import_registry_manifest(
        data, expected_sha256=hashlib.sha256(data).hexdigest(),
        expected_source_commit=COMMIT,
    )


def test_source_pinned_complete_325_manifest_is_parsed_without_promoting_a_field():
    payload = _bytes()
    evidence = _parse(payload)
    assert evidence.commit_sha == COMMIT
    assert evidence.file_sha256 == hashlib.sha256(payload).hexdigest()
    assert evidence.source_path == SOURCE
    assert len(evidence.fields) == 325
    report = build_gap_matrix(evidence.fields)
    assert report["counts"]["UNKNOWN"] == 325
    assert report["replacement_certified"] is False
    assert report["real_provider_calls"] == 0


def test_byte_change_without_hash_update_rejected():
    payload = _bytes()
    with pytest.raises(GapMatrixError, match="REGISTRY_DIGEST_MISMATCH"):
        import_registry_manifest(
            payload + b" ",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_source_commit=COMMIT,
        )


@pytest.mark.parametrize("count", [0, 324, 326])
def test_partial_or_extra_rows_rejected(count):
    with pytest.raises(GapMatrixError, match="FPT_FIELD_REGISTRY_INCOMPLETE"):
        _parse(_bytes(count=count))


def test_duplicate_metadata_rows_rejected():
    with pytest.raises(GapMatrixError, match="DUPLICATE_FPT_FIELD"):
        _parse(_bytes(duplicate=True))


@pytest.mark.parametrize("source", [
    "/etc/passwd", "../.env", "src\\secret", "",
])
def test_unsafe_source_path_rejected(source):
    with pytest.raises(GapMatrixError, match="INVALID_MANIFEST_SOURCE_PATH"):
        _parse(_bytes(source=source))


def test_mismatched_source_identity_and_schema_rejected():
    with pytest.raises(GapMatrixError, match="INVALID_SOURCE_REPOSITORY"):
        _parse(_bytes(source_repo="example/not-matchpilot"))
    with pytest.raises(GapMatrixError, match="UNKNOWN_FPT_SCHEMA_VERSION"):
        _parse(_bytes(schema="future-made-up"))


def test_unverified_source_commit_is_not_accepted():
    with pytest.raises(GapMatrixError, match="SOURCE_COMMIT_MISMATCH"):
        _parse(_bytes(commit="b" * 40))
    with pytest.raises(GapMatrixError, match="SOURCE_COMMIT_INVALID"):
        import_registry_manifest(_bytes(), expected_sha256="a" * 64,
                                 expected_source_commit="short")


def test_bad_json_or_oversized_payload_rejected():
    with pytest.raises(GapMatrixError, match="MANIFEST_NOT_VALID_JSON"):
        _parse(b"{broken json")
    with pytest.raises(GapMatrixError, match="MANIFEST_SIZE_INVALID"):
        _parse(b"x" * 262145)


def test_all_37_indicator_names_from_matchpilot_issue_are_preserved():
    report = indicator_gap_report()
    assert report["expected_indicators"] == 37
    assert report["catalogued_indicators"] == 37
    assert len(INDICATORS) == len(set(INDICATORS)) == 37
    assert report["certified_indicators"] == 0
    assert report["real_provider_calls"] == 0
    assert "Pressure Index" in INDICATORS
    assert "Corner Pressure Index" in INDICATORS
    assert "Data Confidence Score" in INDICATORS
    for row in report["records"]:
        assert row["indicator"] in INDICATORS
        assert row["formula"] is None
        assert row["formula_version"] is None
        assert row["status"] == "FORMULA_NOT_IN_OFFLINE_AUDIT"
        assert row["replacement_verdict"] == "UNKNOWN"
        assert not row["point_in_time_verified"]
