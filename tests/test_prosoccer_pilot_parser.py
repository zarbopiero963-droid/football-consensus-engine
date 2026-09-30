from staging.browser_use_shortlist.pilot48 import (
    parse_prosoccer,
    prosoccer_status_counts,
)


def test_prosoccer_parser_ignores_ft_but_keeps_scheduled_rows():
    text = """
FT
United States
4
Chile
2
Friendly International
58%
22%
20%
57%
43%
14:00
Mauritius
Djibouti
Friendly International
54%
24%
22%
51%
49%
1X
78%
"""
    rows = parse_prosoccer(text)
    assert len(rows) == 1
    row = rows[0]
    assert row["home_team"] == "Mauritius"
    assert row["away_team"] == "Djibouti"
    assert row["p1"] == 54
    assert row["px"] == 24
    assert row["p2"] == 22
    assert row["kickoff_raw"] == "14:00"
    assert row["source_timezone"] == ""
    assert row["timezone_status"] == "TIMEZONE_UNRESOLVED"
    assert "kickoff_utc" not in row
    assert prosoccer_status_counts(text)["ft"] == 1


def test_prosoccer_browser_locale_does_not_create_verified_timezone():
    rows = parse_prosoccer("""
21:00
Team A
Team B
Test League
50%
25%
25%
55%
45%
""")
    assert len(rows) == 1
    row = rows[0]
    assert row["kickoff_raw"] == "21:00"
    assert row["source_timezone"] == ""
    assert row["timezone_status"] == "TIMEZONE_UNRESOLVED"
    assert row.get("kickoff_utc") is None
