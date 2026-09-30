from datetime import datetime
from zoneinfo import ZoneInfo

from staging.browser_use_shortlist.pilot48 import (
    parse_prosoccer,
    prosoccer_status_counts,
    resolve_prosoccer_date,
)


ROME = ZoneInfo("Europe/Rome")


def test_prosoccer_parser_ignores_ft_but_keeps_future_rows():
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
    assert rows[0]["home_team"] == "Mauritius"
    assert rows[0]["away_team"] == "Djibouti"
    assert rows[0]["p1"] == 54
    assert rows[0]["px"] == 24
    assert rows[0]["p2"] == 22
    assert prosoccer_status_counts(text)["ft"] == 1


def test_prosoccer_tomorrow_resolves_to_next_calendar_day():
    pred = {
        "kickoff_raw": "02:00",
    }
    base = datetime(2026, 9, 30, 20, 0, tzinfo=ROME)
    resolve_prosoccer_date(pred, day_offset=1, base_time=base)
    assert pred["kickoff_utc"].isoformat() == "2026-10-01T00:00:00+00:00"


def test_prosoccer_today_resolves_in_browser_timezone():
    pred = {
        "kickoff_raw": "21:00",
    }
    base = datetime(2026, 9, 30, 12, 0, tzinfo=ROME)
    resolve_prosoccer_date(pred, day_offset=0, base_time=base)
    assert pred["kickoff_utc"].isoformat() == "2026-09-30T19:00:00+00:00"
