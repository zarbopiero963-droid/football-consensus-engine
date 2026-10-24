"""Offline MatchPilot provider-gap matrix engine (PR-04 PREPARATION ONLY).

Inputs must be supplied from an explicitly verified, read-only FPT
schema export. No user data, API-FOOTBALL key, MatchPilot write connection
or external HTTP client is accessed or imported by this module.

IMPORTANT: lexically similar field names are NEVER equivalent evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping

FIELD_COUNT_REQUIRED = 325
VALID_TIMING = frozenset({
    "PREMATCH_IDENTITY", "PREMATCH_MARKET_UNTIMED", "POSTMATCH_OUTCOME"
})
VALID_REPLACEMENT = frozenset({
    "DIRECT_REPLACEMENT", "REPLACEMENT_WITH_TRANSFORMATION",
    "PARTIAL_REPLACEMENT", "TOTALCORNER_REQUIRED", "UNAVAILABLE", "UNKNOWN",
})
VALID_FPT_FAMILIES = frozenset({
    "identity", "market", "expected_goals", "shooting", "corners",
    "possession_creation", "defense", "discipline", "goals",
    "period_split", "set_pieces", "unclassified",
})
SOURCE_FPT_CERT = "MatchPilot/docs/futpython-certification-2026-10-05.md"
SOURCE_FPT_SCHEMA = "MatchPilot/src/providers/futpython/schema.mjs"
SOURCE_MATCHPILOT_ISSUE = "MatchPilot#249"
SOURCE_API_DOCS = "https://www.api-football.com/documentation_v3"
COVERAGE_STATUS = "DOCUMENTARY_ONLY_NO_PROVIDER_CALLS"

# Endpoint candidates only. No row may be certified DIRECT by this table.
FAMILY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "identity": ("/fixtures", "/teams", "/leagues"),
    "market": ("/odds", "/odds/live", "/odds/bets", "/odds/live/bets"),
    "expected_goals": (),  # /predictions is NOT historical shot-based xG
    "shooting": ("/fixtures/statistics", "/fixtures/players"),
    "corners": ("/fixtures/statistics", "/fixtures/events"),
    "possession_creation": ("/fixtures/statistics", "/teams/statistics"),
    "defense": ("/fixtures/statistics", "/fixtures/players"),
    "discipline": ("/fixtures/statistics", "/fixtures/events"),
    "goals": ("/fixtures", "/fixtures/events"),
    "period_split": ("/fixtures/statistics", "/fixtures/events"),
    "set_pieces": ("/fixtures/statistics",),
    "unclassified": (),
}

TOTALCORNER_CANDIDATE_FAMILIES = frozenset({
    "corners", "possession_creation", "period_split", "market"
})


class GapMatrixError(ValueError):
    """Sanitized, non-sensitive validation error."""


@dataclass(frozen=True)
class FptField:
    """Metadata only; never copies the historical FPT payload itself."""

    name: str
    family: str
    timing_class: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not (
            1 <= len(self.name) <= 150
        ) or any(ord(ch) < 33 or ord(ch) == 127 for ch in self.name):
            raise GapMatrixError("INVALID_FPT_FIELD_NAME")
        if self.family not in VALID_FPT_FAMILIES:
            raise GapMatrixError("UNRECOGNIZED_FPT_FAMILY")
        if self.timing_class not in VALID_TIMING:
            raise GapMatrixError("UNRECOGNIZED_FPT_TIMING_CLASS")


def _caution(field: FptField) -> tuple[str, ...]:
    flags: list[str] = ["NO_REAL_PROVIDER_EVIDENCE"]
    if field.timing_class == "PREMATCH_MARKET_UNTIMED":
        flags.append("HISTORICAL_MARKET_PIT_NOT_PROVEN")
    if field.timing_class == "POSTMATCH_OUTCOME":
        flags.append("NOT_SAFE_AS_PREKICKOFF_INPUT_WITHOUT_PRIOR_OBSERVATION")
    if field.family == "expected_goals":
        flags.append("PROVIDER_PREDICTIONS_NOT_SHOT_BASED_XG_EQUIVALENT")
    if field.family in ("market", "corners"):
        flags.append("EXACT_BOOKMAKER_MARKET_LINE_AND_TIME_MUST_MATCH")
    if field.family == "period_split":
        flags.append("HT_2T_CUMULATIVE_VS_INTERVAL_SEMANTICS_UNVERIFIED")
    return tuple(flags)


def _field_record(field: FptField) -> dict[str, Any]:
    return {
        "fpt_field": field.name,
        "fpt_family": field.family,
        "fpt_timing_class": field.timing_class,
        "replacement_status": "UNKNOWN",
        "candidate_apifootball_endpoints": list(FAMILY_CANDIDATES[field.family]),
        "totalcorner_candidate_only": field.family in TOTALCORNER_CANDIDATE_FAMILIES,
        "totalcorner_operational": False,
        "provider_field_path": None,
        "unit_equivalence_verified": False,
        "semantic_equivalence_verified": False,
        "league_season_coverage_verified": False,
        "timestamp_pit_verified": False,
        "license_retention_verified": False,
        "evidence_level": COVERAGE_STATUS,
        "warnings": list(_caution(field)),
    }


def build_gap_matrix(fields: Iterable[FptField], *, expect_count: int = FIELD_COUNT_REQUIRED) -> dict[str, Any]:
    """Exactly one UNKNOWN row per real FPT registry field.

    Abort rather than silently report 325/325 completeness when the
    actual field registry is absent, truncated, duplicated or mismatched.
    """
    if type(expect_count) is not int or expect_count != FIELD_COUNT_REQUIRED:
        raise GapMatrixError("FPT_EXPECTED_COUNT_MUST_BE_325")
    material = list(fields)
    if len(material) != expect_count:
        raise GapMatrixError("FPT_FIELD_REGISTRY_INCOMPLETE")
    if not all(isinstance(f, FptField) for f in material):
        raise GapMatrixError("INVALID_FPT_FIELD_RECORD")
    names = [f.name for f in material]
    if len(set(names)) != len(names):
        raise GapMatrixError("DUPLICATE_FPT_FIELD")
    records = [_field_record(f) for f in sorted(material, key=lambda f: f.name)]
    canonical = json.dumps(
        [(f.name, f.family, f.timing_class) for f in sorted(material, key=lambda f: f.name)],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "provider_candidate": "API-FOOTBALL",
        "source_provider": "FutPythonTrader",
        "expected_fields": FIELD_COUNT_REQUIRED,
        "fields_inspected": len(records),
        "registry_metadata_sha256": hashlib.sha256(canonical).hexdigest(),
        "source_documents": [
            SOURCE_FPT_CERT, SOURCE_FPT_SCHEMA, SOURCE_MATCHPILOT_ISSUE
        ],
        "evidence_level": COVERAGE_STATUS,
        "real_provider_calls": 0,
        "totalcorner_enabled": False,
        "replacement_certified": False,
        "counts": {state: sum(r["replacement_status"] == state for r in records)
                   for state in sorted(VALID_REPLACEMENT)},
        "records": records,
        "warnings": [
            "This is an offline candidate mapping, not an empirical provider comparison.",
            "FPT field names and timing classes must be loaded from a verified read-only registry export.",
            "No field is promoted to DIRECT/TRANSFORMED/PARTIAL without empirical semantic and PIT proof.",
            "Never revive TotalCorner or modify historical MatchPilot #40 archive.",
        ],
    }


def audit_candidate_coverage(report: Mapping[str, Any]) -> dict[str, Any]:
    """Report analysis completeness only, not a positive replacement verdict."""
    if report.get("fields_inspected") != FIELD_COUNT_REQUIRED:
        raise GapMatrixError("FPT_FIELD_REGISTRY_INCOMPLETE")
    rows = report.get("records")
    if not isinstance(rows, list) or len(rows) != FIELD_COUNT_REQUIRED:
        raise GapMatrixError("FPT_MATRIX_INCOMPLETE")
    if any(row.get("replacement_status") != "UNKNOWN" for row in rows):
        raise GapMatrixError("UNSUPPORTED_POSITIVE_REPLACEMENT_CLAIM")
    candidate_rows = sum(bool(row.get("candidate_apifootball_endpoints")) for row in rows)
    return {
        "matrix_rows": FIELD_COUNT_REQUIRED,
        "candidate_endpoint_rows": candidate_rows,
        "unmapped_or_unverified_rows": FIELD_COUNT_REQUIRED,
        "direct_replacements_certified": 0,
        "replacement_verdict": "INSUFFICIENT_EVIDENCE",
        "totalcorner_status": "SUSPENDED",
        "real_provider_calls": 0,
    }
