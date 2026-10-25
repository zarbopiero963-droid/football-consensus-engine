"""Read-only MatchPilot Strategy Lab indicator census from issue #34.

The issue enumerates 37 candidate indicator labels, but a label alone
is NOT a mathematical formula. Every row is deliberately UNSPECIFIED
until owner-authorized code/formula version evidence is supplied.
"""
from __future__ import annotations

from typing import Any

MATCHPILOT_SOURCE = (
    "https://github.com/zarbopiero963-droid/MatchPilot/issues/34"
)
INDICATORS = (
    "Pressure Index",
    "Goal Pressure",
    "Momentum",
    "Draw Risk",
    "Vulnerable Score",
    "Attack Efficiency Index",
    "Territorial Dominance",
    "Shot Quality Index",
    "Conversion Pressure",
    "Defensive Stress Index",
    "Comeback Pressure",
    "Lead Protection Index",
    "Equalizer Risk",
    "Next Goal Bias",
    "Goal Imminence Score",
    "Market-Pressure Divergence",
    "Odds Momentum",
    "Market Shock Index",
    "Fair Price Gap",
    "Lay Value Index",
    "Back Value Index",
    "Dutching Opportunity Score",
    "Score Stability Index",
    "Late Goal Index",
    "First Goal Risk",
    "Second Goal Risk",
    "Red Card Impact Index",
    "Corner Pressure Index",
    "Card Pressure Index",
    "Game State Volatility",
    "Pace Index",
    "Chaos Index",
    "Dead Match Index",
    "False Pressure Index",
    "Finishing Underperformance",
    "Finishing Overperformance",
    "Data Confidence Score",
)
assert len(INDICATORS) == len(set(INDICATORS)) == 37


def indicator_gap_report() -> dict[str, Any]:
    """Inventory names with explicit absence of independently verified math.

    A real algorithm may exist elsewhere in MatchPilot source. This
    function makes no claim to have searched all files or proved
    nonexistence. It states only that this offline dataset contains no
    certified definition/version and cannot validate computability.
    """
    records = [
        {
            "indicator": name,
            "source": MATCHPILOT_SOURCE,
            "formula": None,
            "formula_version": None,
            "input_fields": None,
            "required_time_window": None,
            "provider_coverage_verified": False,
            "point_in_time_verified": False,
            "status": "FORMULA_NOT_IN_OFFLINE_AUDIT",
            "replacement_verdict": "UNKNOWN",
        }
        for name in INDICATORS
    ]
    return {
        "expected_indicators": 37,
        "catalogued_indicators": len(records),
        "certified_indicators": 0,
        "source": MATCHPILOT_SOURCE,
        "real_provider_calls": 0,
        "records": records,
        "disclaimer": (
            "Names sourced from MatchPilot #34, not verified formulas; "
            "do not derive inputs by intuition or claim API equivalence."
        ),
    }
