from app.api import apply_decision_filter, frozen_snapshot_view, _settle_ticket


def sample(probability=0.62, odds=1.72, edge=0.041):
    return {
        "match_id": "match-1",
        "kickoff_utc": "2026-09-30T20:00:00+00:00",
        "snapshot_timestamp_utc": "2026-09-30T15:00:00+00:00",
        "league": "Test League",
        "home": "Team A",
        "away": "Team B",
        "model_pick": "1",
        "model_probability": probability,
        "p1": probability,
        "px": 0.23,
        "p2": 1 - probability - 0.23,
        "pinnacle_odds": {"1": odds, "X": 3.5, "2": 5.2},
        "pinnacle_fair": {"1": 0.579, "X": 0.25, "2": 0.171},
        "picked_odds": odds,
        "picked_edge": edge,
        "strategies": ["S1", "S2", "S5"],
        "status": "BET",
    }


def test_decision_snapshot_is_frozen_when_later_monitor_snapshot_changes():
    at_17 = sample(probability=0.62, odds=1.72, edge=0.041)
    ticket = frozen_snapshot_view(at_17)

    # Simulate the 20:00 monitoring snapshot changing after the user already saw 17:00.
    at_17["model_probability"] = 0.58
    at_17["picked_odds"] = 1.80
    at_17["picked_edge"] = -0.01
    at_17["strategies"] = ["S1"]

    assert ticket["model_probability"] == 0.62
    assert ticket["picked_odds"] == 1.72
    assert ticket["picked_edge"] == 0.041
    assert ticket["strategies"] == ["S1", "S2", "S5"]


def test_ticket_settlement_uses_frozen_pick_and_odds():
    ticket = frozen_snapshot_view(sample(probability=0.62, odds=1.72))
    result = {"payload": {"result_1x2": "1", "final_home_goals": "2", "final_away_goals": "0"}}
    settled = _settle_ticket(ticket, result)
    assert settled["correct"] is True
    assert round(settled["profit_flat_1"], 2) == 0.72


def test_pending_ticket_stays_pending():
    settled = _settle_ticket(frozen_snapshot_view(sample()), None)
    assert settled["settlement_status"] == "PENDING"
    assert settled["correct"] is None
    assert settled["profit_flat_1"] is None


def test_signal_filter_freezes_only_what_is_shown():
    matches = [
        sample(),
        {**sample(), "match_id": "match-2", "strategies": ["S1"], "status": "BET"},
        {**sample(), "match_id": "match-3", "strategies": [], "status": "NO_BET"},
    ]
    visible = apply_decision_filter(matches, "signals", ["S2", "S4", "S6", "S8", "S9"])
    assert [m["match_id"] for m in visible] == ["match-1"]
