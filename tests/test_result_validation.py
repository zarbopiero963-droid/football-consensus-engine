from app.result_validation import compute_consensus, observation_id


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


def test_pilot_result_observations_do_not_enter_production_consensus():
    rows = [
        {"provider": "pilot:smart_soccer_stats", "final_home_goals": 3, "final_away_goals": 0,
         "ht_home_goals": 2, "ht_away_goals": 0, "corners_home": None, "corners_away": None,
         "ht_corners_home": None, "ht_corners_away": None}
    ]
    values, fields, status = compute_consensus(rows)
    assert status == "UNVERIFIED"
    assert values["final_home_goals"] is None
    assert fields["final_home_goals"]["status"] == "MISSING"


def test_pilot_observation_cannot_override_production_provider():
    rows = [
        {"provider": "fifa-calendar-v3", "final_home_goals": 1, "final_away_goals": 0,
         "ht_home_goals": None, "ht_away_goals": None, "corners_home": None, "corners_away": None,
         "ht_corners_home": None, "ht_corners_away": None},
        {"provider": "pilot:smart_soccer_stats", "final_home_goals": 9, "final_away_goals": 9,
         "ht_home_goals": 4, "ht_away_goals": 4, "corners_home": None, "corners_away": None,
         "ht_corners_home": None, "ht_corners_away": None},
    ]
    values, _fields, status = compute_consensus(rows)
    assert status == "SINGLE_SOURCE"
    assert values["final_home_goals"] == 1
    assert values["final_away_goals"] == 0


def test_observation_id_is_stable_for_same_semantic_result():
    payload_a = {
        "final_home_goals": 2,
        "final_away_goals": 0,
        "ht_home_goals": 1,
        "ht_away_goals": 0,
        "corners_home": None,
        "corners_away": None,
        "ht_corners_home": None,
        "ht_corners_away": None,
        "raw_page_sha256": "first-fetch",
    }
    payload_b = dict(payload_a)
    payload_b["raw_page_sha256"] = "later-fetch"

    a = observation_id("match-1", "provider-a", "pm-1", "FINISHED", payload_a)
    b = observation_id("match-1", "provider-a", "pm-1", "FINISHED", payload_b)
    assert a == b


def test_observation_id_changes_for_official_correction():
    original = {
        "final_home_goals": 2,
        "final_away_goals": 0,
        "ht_home_goals": 1,
        "ht_away_goals": 0,
        "corners_home": None,
        "corners_away": None,
        "ht_corners_home": None,
        "ht_corners_away": None,
    }
    corrected = dict(original)
    corrected["final_home_goals"] = 1
    corrected["final_away_goals"] = 1

    a = observation_id("match-1", "provider-a", "pm-1", "FINISHED", original)
    b = observation_id("match-1", "provider-a", "pm-1", "FINISHED", corrected)
    assert a != b


def test_observation_id_changes_when_provider_match_identity_changes():
    payload = {
        "final_home_goals": 2,
        "final_away_goals": 0,
        "ht_home_goals": None,
        "ht_away_goals": None,
        "corners_home": None,
        "corners_away": None,
        "ht_corners_home": None,
        "ht_corners_away": None,
    }
    a = observation_id("match-1", "provider-a", "pm-1", "FINISHED", payload)
    b = observation_id("match-1", "provider-a", "pm-2", "FINISHED", payload)
    assert a != b
