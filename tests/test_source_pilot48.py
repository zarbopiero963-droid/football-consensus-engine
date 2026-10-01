from staging.browser_use_shortlist.pilot48 import parse_prosoccer, parse_statsultra, parse_elofoot


def test_prosoccer_triplet():
    text = """
12:00
Lithuania
Andorra
Friendly International
54%
25%
22%
48%
52%
1X
79%
"""
    rows = parse_prosoccer(text)
    assert len(rows) == 1
    assert rows[0]["home_team"] == "Lithuania"
    assert rows[0]["away_team"] == "Andorra"
    assert rows[0]["p1"] == 54
    assert rows[0]["px"] == 25
    assert rows[0]["p2"] == 22
    assert rows[0]["source_timezone"] == ""
    assert rows[0]["timezone_status"] == "TIMEZONE_UNRESOLVED"


def test_statsultra_triplet_and_extras():
    text = """
ELD
Eldense
49.7
V
OVI
Real Oviedo
61.3
28%
ELD
30%
DRAW
42%
OVI
LIKELIEST SCORELINES
1-1
14.4%
0-0
10.3%
BOTH SCORE
51%
OVER 2.5
44%
EXP. GOALS
1.1–1.4
"""
    rows = parse_statsultra(text)
    assert len(rows) == 1
    row = rows[0]
    assert (row["p1"], row["px"], row["p2"]) == (28, 30, 42)
    assert row["btts_yes"] == 51
    assert row["over25"] == 44
    assert row["under25"] is None
    assert row["xg_home"] == 1.1
    assert row["xg_away"] == 1.4


def test_elofoot_triplet():
    text = """
17:00 UK/Lagos · UEFA Nations League
Azerbaijan
2 – 0
Liechtenstein
Predicted score · 17% probability
1
84 %
X
12 %
2
4 %
"""
    rows = parse_elofoot(text)
    assert len(rows) == 1
    assert rows[0]["home_team"] == "Azerbaijan"
    assert rows[0]["away_team"] == "Liechtenstein"
    assert (rows[0]["p1"], rows[0]["px"], rows[0]["p2"]) == (84,12,4)
    assert rows[0]["correct_score"] == "2-0"


def test_prosoccer_multimarket_parsers():
    from staging.browser_use_shortlist.pilot48 import (
        enrich_prosoccer_predictions,
        parse_prosoccer_corners,
        parse_prosoccer_goal_lines,
        parse_prosoccer_ht_1x2,
        parse_prosoccer_htft,
        parse_prosoccer_scorelines,
    )

    goals = """
08:00
Mauritius
Djibouti
Friendly International
Over 1.5
76%
Under 3.5
71%
Over 2.5
51%
"""
    scores = """
08:00
Mauritius
Djibouti
Friendly International
1 – 1
11.3%
1 – 0
11.2%
2 – 1
9.7%
"""
    ht = """
08:00
Mauritius
Djibouti
Friendly International
HT X
48%
HT 1
34%
HT 2
18%
"""
    hts = """
08:00
Mauritius
Djibouti
Friendly International
0 – 0
40.3%
1 – 0
23.1%
0 – 1
13.6%
"""
    corners = """
08:00
Mauritius
Djibouti
Friendly International
Under 10.5
92%
Under 9.5
86%
Under 8.5
77%
"""
    htft = """
08:00
Mauritius
Djibouti
Friendly International
1/1
28%
X/1
21%
X/X
17%
"""
    assert parse_prosoccer_goal_lines(goals)["mauritius|djibouti"][0] == {
        "selection": "OVER", "line": 1.5, "probability": 76.0
    }
    assert parse_prosoccer_scorelines(scores)["mauritius|djibouti"][0]["score"] == "1-1"
    assert parse_prosoccer_ht_1x2(ht)["mauritius|djibouti"] == {"X": 48.0, "1": 34.0, "2": 18.0}
    assert parse_prosoccer_corners(corners)["mauritius|djibouti"][0]["line"] == 10.5
    assert parse_prosoccer_htft(htft)["mauritius|djibouti"][0]["selection"] == "1/1"

    preds = [{
        "home_team": "Mauritius", "away_team": "Djibouti", "payload_extra": {},
        "correct_score": "",
    }]
    enrich_prosoccer_predictions(preds, {
        "goals": goals, "correct_scores": scores, "half_time": ht,
        "ht_correct_scores": hts, "corners": corners, "htft": htft,
    })
    payload = preds[0]["payload_extra"]
    assert payload["ht_px"] == 48.0
    assert payload["ht_correct_score"] == "0-0"
    assert payload["corners_line"] == 10.5
    assert payload["corners_under"] == 92.0
    assert payload["htft_selection"] == "1/1"
    assert preds[0]["correct_score"] == "1-1"


def test_prosoccer_payload_datetime_is_json_serializable():
    import json
    from datetime import datetime, timezone

    payload = {
        "home_team": "Mauritius",
        "away_team": "Djibouti",
        "kickoff_utc": datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
    }
    encoded = json.dumps(payload, default=str)
    decoded = json.loads(encoded)
    assert decoded["kickoff_utc"] == "2026-09-30 12:00:00+00:00"


def test_result_source_inspection_contract():
    from staging.result_sources.pilot48 import inspect_source

    rows, caps = inspect_source(
        "smart_soccer_stats",
        "30 Sept 2026\nA\n1\n-\n0\nB\n(1-0)\nHalf-Time\nCorners & Fouls",
        "",
    )
    assert rows == 1
    assert caps["ft_score"] is True
    assert caps["ht_score"] is True

    rows, caps = inspect_source(
        "statbunker",
        "LastMatches",
        '<a href="/competitions/MatchDetails?comp_id=785&match_id=128310">x</a>',
    )
    assert rows == 1
    assert caps["ft_score"] is True
    assert caps["ht_score"] is False


def test_statsultra_does_not_infer_under_from_over():
    text = """
AAA
Home Team
50.0
V
BBB
Away Team
50.0
40%
AAA
30%
DRAW
30%
BBB
OVER 2.5
72%
"""
    rows = parse_statsultra(text)
    assert len(rows) == 1
    row = rows[0]
    assert row["over25"] == 72
    assert row["under25"] is None


def test_smart_soccer_stats_parser_produces_real_ft_ht_observations():
    from staging.result_sources.pilot48 import parse_smart_soccer_stats_results

    rows = parse_smart_soccer_stats_results("""
30 Sept 2026
Lithuania
3
-
0
Andorra
(2-0)
Friendlies - World, 2026
30 Sept 2026
Eritrea
0
-
5
South Africa
(0-2)
Africa Cup of Nations - Qualification - World, 2027
""")
    assert len(rows) == 2
    assert rows[0]["home_team"] == "Lithuania"
    assert rows[0]["final_home_goals"] == 3
    assert rows[0]["final_away_goals"] == 0
    assert rows[0]["ht_home_goals"] == 2
    assert rows[0]["ht_away_goals"] == 0
    assert rows[0]["corners_home"] is None
    assert rows[1]["final_home_goals"] == 0
    assert rows[1]["final_away_goals"] == 5


def test_worldfootball_text_without_lifecycle_cannot_certify_finished_scores():
    from staging.result_sources.pilot48 import parse_worldfootball_results

    rows = parse_worldfootball_results("""
Matchday 1
22.08.2026
Inter
4:1
AC Monza
Udinese Calcio
1:1
Como 1907
Matchday 6
10.10.2026
Genoa CFC
15:00
ACF Fiorentina
Standing
AS Roma
5
14:3
11
13
""")
    assert rows == []  # Numeric text alone could also be a scheduled clock.


def test_result_anchor_match_requires_exact_pair_and_date():
    from datetime import datetime, timezone
    from staging.result_sources.pilot48 import match_result_anchor

    row = {
        "home_team": "Lithuania",
        "away_team": "Andorra",
        "source_date": datetime(2026, 9, 30).date(),
        "competition": "Friendlies - World, 2026",
    }
    anchors = [{
        "match_id": "fi:lithuania-vs-andorra-2026-09-30",
        "home_team": "Lithuania",
        "away_team": "Andorra",
        "league": "Friendlies",
        "kickoff_utc": datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc),
    }]
    anchor, status = match_result_anchor(row, anchors)
    assert anchor["match_id"] == "fi:lithuania-vs-andorra-2026-09-30"
    assert status == "EXACT_PAIR_DATE_COMPETITION_DIAGNOSTIC"

    row["source_date"] = datetime(2026, 10, 1).date()
    anchor, status = match_result_anchor(row, anchors)
    assert anchor is None
    assert status == "DATE_MISMATCH"
