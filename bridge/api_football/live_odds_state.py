"""FIX-09: a DERIVED, non-mutating summary of a /odds/live reply.

The provider data passes through untouched (null stays null). Next to it, this module says
which of these the reply shows, using only the selection fields evidenced in the repository
(`odd`, `suspended`, `main`, `handicap`; tests/test_api_football_five_point_evidence.py,
tests/_apifootball_fake_provider.py, docs/api_football/live_five_point_sample_2026-10-25.md):

  ACTIVE              at least one selection published `suspended: false` with a non-null odd
  ALL_SUSPENDED       every selection published `suspended: true`
  ALL_NULL_ODDS       every selection published `odd: null`
  NO_OPEN_PRICED_SELECTION  selections exist but none is explicitly open and priced (e.g. no
                      `suspended` field at all): never assumed open
  EMPTY               no fixture, or fixtures without any market/selection
  MALFORMED           not the evidenced shape (`response` list, `odds` list, `values` list), or a
                      selection without `value`/`odd`, or a non-null odd that is not a decimal
                      string >= 1.0 ("0" only on a suspended selection), as in the E2E runner
  UNAVAILABLE         no provider reply (set by the tool on an error, with its fixed code)

A selection without `suspended` is UNKNOWN, never open; an odd is never read as a number.
"""
from __future__ import annotations

import re
from typing import Any

BASIS_FIELDS = ("odd", "suspended", "main", "handicap")
EVIDENCE = {
    "odds[] per fixture, market id/name/values": "REAL_SAMPLE docs/api_football/live_five_point_sample_2026-10-25.md",
    "selection odd/suspended/main/handicap": "REPO_FIXTURES (no captured real /odds/live payload in the repo)",
}


# A provider decimal price as published: digits with an optional fraction (no sign, exponent, nan).
_DECIMAL = re.compile(r"^[0-9]{1,6}(?:\.[0-9]{1,6})?$")


def _selection_ok(v: dict[str, Any]) -> bool:
    """`value` and `odd` published; a non-null odd is a decimal string >= 1.0, or "0" on a
    selection flagged suspended (repo fixture). Anything else is malformed, never a price."""
    if "value" not in v or "odd" not in v:
        return False
    odd = v["odd"]
    if odd is None:
        return True
    if not isinstance(odd, str) or not _DECIMAL.fullmatch(odd):
        return False
    return float(odd) >= 1.0 or (float(odd) == 0 and v.get("suspended") is True)


def classify(envelope: Any) -> dict[str, Any]:
    counts = {"fixtures": 0, "markets": 0, "selections": 0, "open_priced": 0, "suspended_true": 0,
              "suspended_false": 0, "suspended_unknown": 0, "odd_null": 0, "odd_missing": 0, "main_true": 0}
    out = {"label": "DERIVED", "basis_fields": list(BASIS_FIELDS), "evidence": EVIDENCE}
    response = envelope.get("response") if isinstance(envelope, dict) else None
    if not isinstance(response, list):
        return {**out, "state": "MALFORMED", **counts}
    for item in response:
        odds = item.get("odds") if isinstance(item, dict) else None
        if not isinstance(odds, list):
            return {**out, "state": "MALFORMED", **counts}
        counts["fixtures"] += 1
        for bet in odds:
            values = bet.get("values") if isinstance(bet, dict) else None
            if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
                return {**out, "state": "MALFORMED", **counts}
            counts["markets"] += 1
            for v in values:
                if not _selection_ok(v):
                    # Same rule as the E2E runner (_live_odd): never ACTIVE on a bad price.
                    return {**out, "state": "MALFORMED", **counts}
                counts["selections"] += 1
                suspended = v.get("suspended")
                if suspended is True:
                    counts["suspended_true"] += 1
                elif suspended is False:
                    counts["suspended_false"] += 1
                else:
                    counts["suspended_unknown"] += 1
                if "odd" not in v:
                    counts["odd_missing"] += 1
                elif v["odd"] is None:
                    counts["odd_null"] += 1
                if v.get("main") is True:
                    counts["main_true"] += 1
                if suspended is False and v.get("odd") is not None:
                    counts["open_priced"] += 1
    n = counts["selections"]
    if n == 0:
        state = "EMPTY"
    elif counts["open_priced"]:
        state = "ACTIVE"
    elif counts["suspended_true"] == n:
        state = "ALL_SUSPENDED"
    elif counts["odd_null"] == n:
        state = "ALL_NULL_ODDS"
    else:
        state = "NO_OPEN_PRICED_SELECTION"
    return {**out, "state": state, **counts}


def unavailable(code: Any) -> dict[str, Any]:
    return {"label": "DERIVED", "state": "UNAVAILABLE", "reason": code if isinstance(code, str) else None}
