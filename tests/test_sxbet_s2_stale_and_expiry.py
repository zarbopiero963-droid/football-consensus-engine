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


@pytest.mark.parametrize("code", [0, 10, "2", 2.0, None, True])
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
    assert [x["eventId"] for x in v["final_status_listed_as_active"]] == ["L1"]
    assert [x["eventId"] for x in v["not_started_but_start_long_past"]] == ["L2"]
    assert v["decoded"]["L4"]["phase"] == "LIVE" and "upcoming" not in out


def test_d7_fixture_status_decodes_each_event():
    t, _, _ = make(universal(overrides={"/fixture/status": lambda r: _ok({"L1": {"status": 4}, "L2": {"status": 42}})}))
    out = run(t["sxbet_fixture_status"](sportx_event_ids=["L1", "L2"]))
    assert out["status_decoded"]["L1"]["label"] == "Cancelled"
    assert out["status_decoded"]["L2"]["phase"] == "UNDOCUMENTED_STATUS" and out["data"]["L2"] == {"status": 42}


def test_d7_markets_active_flags_stale_active_rows_on_the_page():
    page = {"markets": [{"marketHash": "0x" + "aa" * 32, "status": "ACTIVE", "gameTime": NOW - 400 * 86400},
                        {"marketHash": "0x" + "bb" * 32, "status": "ACTIVE", "gameTime": NOW - 3600},
                        {"marketHash": "0x" + "cc" * 32, "status": "ACTIVE", "gameTime": NOW + 3600},
                        {"marketHash": "0x" + "dd" * 32, "status": "ACTIVE", "gameTime": None}], "nextKey": None}
    t, _, _ = make(universal(overrides={"/markets/active": lambda r: _ok(page)}))
    out = run(t["sxbet_markets_active"](live_only=True))
    v = out["stale_active_view"]
    assert [x["marketHash"] for x in v["rows"]] == ["0x" + "aa" * 32] and v["scope"] == "THIS_PAGE_ONLY"
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
