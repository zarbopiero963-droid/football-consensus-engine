from app.result_validation import compute_consensus, normalize_api_football, normalize_footystats


def test_footystats_normalization():
    row = normalize_footystats({
        "id": 123,
        "status": "complete",
        "homeGoalCount": 2,
        "awayGoalCount": 1,
        "ht_goals_team_a": 1,
        "ht_goals_team_b": 0,
        "team_a_corners": 6,
        "team_b_corners": 5,
    })
    assert row["provider_match_id"] == "123"
    assert row["final_home_goals"] == 2
    assert row["ht_home_goals"] == 1
    assert row["corners_away"] == 5


def test_api_football_normalization():
    fixture = {
        "fixture": {"id": 99, "status": {"short": "FT"}},
        "score": {"fulltime": {"home": 3, "away": 2}, "halftime": {"home": 1, "away": 1}},
    }
    stats = [
        {"_side": "home", "statistics": [{"type": "Corner Kicks", "value": 7}]},
        {"_side": "away", "statistics": [{"type": "Corner Kicks", "value": 4}]},
    ]
    htstats = [
        {"_side": "home", "statistics": [{"type": "Corner Kicks", "value": 3}]},
        {"_side": "away", "statistics": [{"type": "Corner Kicks", "value": 2}]},
    ]
    row = normalize_api_football(fixture, stats, htstats)
    assert row["final_home_goals"] == 3
    assert row["ht_away_goals"] == 1
    assert row["corners_home"] == 7
    assert row["ht_corners_away"] == 2


def test_consensus_verified_when_two_sources_agree():
    rows = [
        {"provider": "a", "final_home_goals": 2, "final_away_goals": 1,
         "ht_home_goals": 1, "ht_away_goals": 0, "corners_home": 6, "corners_away": 5,
         "ht_corners_home": None, "ht_corners_away": None},
        {"provider": "b", "final_home_goals": 2, "final_away_goals": 1,
         "ht_home_goals": 1, "ht_away_goals": 0, "corners_home": 6, "corners_away": 5,
         "ht_corners_home": None, "ht_corners_away": None},
    ]
    values, fields, status = compute_consensus(rows)
    assert status == "VERIFIED"
    assert values["final_home_goals"] == 2
    assert fields["corners_home"]["status"] == "VERIFIED"


def test_consensus_conflict_blocks_value():
    rows = [
        {"provider": "a", "final_home_goals": 2, "final_away_goals": 1,
         "ht_home_goals": None, "ht_away_goals": None, "corners_home": None, "corners_away": None,
         "ht_corners_home": None, "ht_corners_away": None},
        {"provider": "b", "final_home_goals": 1, "final_away_goals": 1,
         "ht_home_goals": None, "ht_away_goals": None, "corners_home": None, "corners_away": None,
         "ht_corners_home": None, "ht_corners_away": None},
    ]
    values, fields, status = compute_consensus(rows)
    assert status == "CONFLICT"
    assert values["final_home_goals"] is None
    assert fields["final_home_goals"]["status"] == "CONFLICT"


def test_single_source_is_not_called_verified():
    rows = [
        {"provider": "fifa", "final_home_goals": 1, "final_away_goals": 0,
         "ht_home_goals": None, "ht_away_goals": None, "corners_home": None, "corners_away": None,
         "ht_corners_home": None, "ht_corners_away": None}
    ]
    _, fields, status = compute_consensus(rows)
    assert status == "SINGLE_SOURCE"
    assert fields["final_home_goals"]["status"] == "SINGLE_SOURCE"
