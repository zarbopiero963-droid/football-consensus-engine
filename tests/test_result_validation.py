from app.result_validation import compute_consensus


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
