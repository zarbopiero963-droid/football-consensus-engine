from staging.result_sources.scores365 import build_current_games_url, normalize_current_payload


def test_365scores_observed_ended_game():
    payload = {
        "games": [{
            "id": 4861758,
            "competitionId": 321,
            "competitionDisplayName": "Club Friendlies",
            "startTime": "2026-09-29T15:00:00+02:00",
            "statusGroup": 4,
            "statusText": "Ended",
            "homeCompetitor": {"id": 2220, "name": "Jagiellonia", "score": 5.0},
            "awayCompetitor": {"id": 2316, "name": "Suduva", "score": 0.0},
        }]
    }
    row = normalize_current_payload(payload)[0]
    assert row["provider_match_id"] == "4861758"
    assert row["kickoff_utc"] == "2026-09-29T13:00:00+00:00"
    assert row["final_home_goals"] == 5
    assert row["final_away_goals"] == 0
    assert row["result_1x2"] == "1"
    assert row["ht_home_goals"] is None
    assert row["corners_home"] is None


def test_365scores_final_result_only_is_not_fabricated():
    payload = {
        "games": [{
            "id": 4867559,
            "competitionId": 321,
            "startTime": "2026-09-30T16:00:00+02:00",
            "statusGroup": 2,
            "statusText": "Final Result Only",
            "homeCompetitor": {"name": "Segesta", "score": -1.0},
            "awayCompetitor": {"name": "Zagreb", "score": -1.0},
        }]
    }
    row = normalize_current_payload(payload)[0]
    assert row["status"] == "UNRESOLVED"
    assert row["final_home_goals"] is None
    assert row["result_1x2"] is None


def test_365scores_url_is_explicit_and_public_feed_only():
    url = build_current_games_url(321)
    assert "webws.365scores.com/web/games/current/" in url
    assert "competitions=321" in url
    assert "timezoneName=Europe%2FRome" in url
