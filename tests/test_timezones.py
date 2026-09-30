from datetime import datetime, timezone

import pytest

from app.timezones import normalize_source_datetime, to_rome_iso


def test_utc_to_italy_summer_time():
    value = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
    assert to_rome_iso(value) == "2026-09-30T22:00:00+02:00"


def test_utc_to_italy_winter_time():
    value = datetime(2026, 12, 15, 20, 0, tzinfo=timezone.utc)
    assert to_rome_iso(value) == "2026-12-15T21:00:00+01:00"


def test_source_timezone_normalizes_before_italy_display():
    utc_value = normalize_source_datetime("2026-09-30T16:00:00", "America/New_York")
    assert utc_value.isoformat() == "2026-09-30T20:00:00+00:00"
    assert to_rome_iso(utc_value) == "2026-09-30T22:00:00+02:00"


def test_naive_source_time_requires_timezone():
    with pytest.raises(ValueError):
        normalize_source_datetime("2026-09-30T16:00:00", "")
