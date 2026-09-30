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
    assert rows[0]["source_timezone"] == "Europe/Rome"


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
