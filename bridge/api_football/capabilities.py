"""Conservative MatchPilot critical-capability matrix (offline only).

This is a demand inventory based on MatchPilot owner issue #249,
Strategy Lab #34 and historical-provider audit #248. An endpoint
candidate is NOT evidence of payload field equivalence, coverage,
freshness, betting market availability, or historical PIT validity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .catalog import ALLOWED_PATHS

@dataclass(frozen=True)
class Capability:
    id: str
    candidate_paths: tuple[str, ...]
    needs_totalcorner_evidence: bool = False
    historical_pit_required: bool = False
    document_gap: str | None = None


# Demand-side contracts, not a provider-backed source schema.
CAPABILITIES: tuple[Capability, ...] = (
    Capability("fixture_identity", ("/fixtures", "/teams", "/leagues")),
    Capability("fixture_kickoff_timezone", ("/fixtures", "/timezone")),
    Capability("live_score_status", ("/fixtures",)),
    Capability("goals_events", ("/fixtures/events", "/fixtures")),
    Capability("referees", ("/fixtures",)),
    Capability("lineups", ("/fixtures/lineups",)),
    Capability("injuries", ("/injuries", "/sidelined")),
    Capability("shots_on_target", ("/fixtures/statistics",)),
    Capability("shots_off_target", ("/fixtures/statistics",)),
    Capability("total_shots", ("/fixtures/statistics",)),
    Capability("possession", ("/fixtures/statistics",)),
    Capability("fouls", ("/fixtures/statistics",)),
    Capability("yellow_red_cards", ("/fixtures/statistics", "/fixtures/events")),
    Capability("corners", ("/fixtures/statistics",), True),
    Capability("asian_corner_lines", ("/odds", "/odds/live"), True,
               document_gap="NOT_MARKET_VERIFIED"),
    Capability("attacks", (), True, document_gap="NOT_DOCUMENTED_AS_NATIVE_AF"),
    Capability("dangerous_attacks", (), True,
               document_gap="NOT_DOCUMENTED_AS_NATIVE_AF"),
    Capability("big_chances", (), document_gap="NOT_DOCUMENTED_EQUIVALENT"),
    Capability("shot_based_xg", (), document_gap="NOT_DOCUMENTED_EQUIVALENT"),
    Capability("xgot", (), document_gap="NOT_DOCUMENTED_EQUIVALENT"),
    Capability("xpts", (), document_gap="NOT_DOCUMENTED_EQUIVALENT"),
    Capability("ht_2t_period_statistics", ("/fixtures/statistics",),
               document_gap="INTERVAL_SEMANTICS_UNVERIFIED"),
    Capability("live_statistics_freshness", ("/fixtures/statistics",),
               document_gap="PROVIDER_LATENCY_NOT_MEASURED"),
    Capability("prematch_odds", ("/odds",), historical_pit_required=True),
    Capability("live_odds", ("/odds/live",), historical_pit_required=True),
    Capability("odds_movement", ("/odds", "/odds/live"), historical_pit_required=True,
               document_gap="REQUIRES_SAMPLED_OBSERVATIONS"),
    Capability("opening_closing_odds", ("/odds",), historical_pit_required=True,
               document_gap="FIRST_LAST_OBSERVED_NOT_OFFICIAL_OPEN_CLOSE"),
    Capability("bookmaker_market_mapping",
               ("/odds/bookmakers", "/odds/bets", "/odds/live/bets", "/odds/mapping")),
    Capability("predictions", ("/predictions",),
               document_gap="NOT_EQUIVALENT_TO_NATIVE_XG_OR_MARKET_PROBABILITIES"),
    Capability("historical_fixture_depth", ("/fixtures",),
               historical_pit_required=True),
    Capability("league_season_coverage", ("/leagues", "/leagues/seasons"),
               document_gap="LEAGUE_FLAG_NOT_FIXTURE_LEVEL_CERTIFICATION"),
    Capability("provider_identity_mapping", ("/fixtures", "/teams", "/leagues"),
               historical_pit_required=True),
    Capability("licensing_retention", (),
               document_gap="LEGAL_TERMS_NOT_VERIFIED"),
    Capability("original_observed_at_pit", (),
               historical_pit_required=True,
               document_gap="RETROSPECTIVE_PIT_CANNOT_BE_RECONSTRUCTED"),
)
assert len(CAPABILITIES) == len({x.id for x in CAPABILITIES})
assert all(set(c.candidate_paths).issubset(ALLOWED_PATHS) for c in CAPABILITIES)


def capability_gap_report() -> dict[str, Any]:
    rows = [
        {
            "requirement": c.id,
            "api_football_candidate_paths": list(c.candidate_paths),
            "totalcorner_candidate_only": c.needs_totalcorner_evidence,
            "totalcorner_enabled": False,
            "pit_proof_required": c.historical_pit_required,
            "documentation_gap": c.document_gap,
            "replacement_status": "UNKNOWN",
            "evidence_level": "DOCUMENTARY_ONLY_NO_REAL_API_DATA",
            "semantic_equivalence_verified": False,
            "season_league_field_coverage_verified": False,
            "freshness_verified": False,
            "license_verified": False,
        }
        for c in CAPABILITIES
    ]
    return {
        "source_matchpilot_issues": [12, 20, 29, 34, 40, 97, 102, 248, 249],
        "requirements": len(rows),
        "verified_replacements": 0,
        "totalcorner_enabled": False,
        "real_provider_calls": 0,
        "rows": rows,
        "verdict": "INSUFFICIENT_EVIDENCE",
    }
