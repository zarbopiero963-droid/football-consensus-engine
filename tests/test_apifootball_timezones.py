"""FIX-06: Europe/Rome and every IANA zone accepted (owner-approved removal of
TIMEZONE_NOT_CERTIFIED). No captured /timezone catalog exists in the repo, so Python zoneinfo
is the validation source and the provider stays the final authority. The bridge never
converts provider times; a DERIVED check verifies DST offsets without touching the data."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from bridge.api_football import timezones as tz
from bridge.api_football.client import MockFootballClient
from bridge.api_football.config import Settings
from bridge.api_football.contracts import ParameterError, validate
from bridge.api_football.tools import register_tools

TZ_TOOLS = {  # every tool that documents `timezone`, with a minimal valid filter
    "apifootball_fixtures": {"id": 1},
    "apifootball_fixture_rounds": {"league": 39, "season": 2026},
    "apifootball_fixture_headtohead": {"h2h": "33-34"},
    "apifootball_injuries": {"fixture": 1},
    "apifootball_odds": {"fixture": 1},
}


@pytest.mark.parametrize("tool,base", TZ_TOOLS.items())
@pytest.mark.parametrize("zone", ["Europe/Rome", "UTC", "America/Argentina/Buenos_Aires", "Asia/Kathmandu",
                                  "Australia/Lord_Howe", "Pacific/Chatham", "Etc/GMT-14"])
def test_iana_zones_are_accepted_on_every_timezone_tool(tool, base, zone):
    _, params = validate(tool, {**base, "timezone": zone})
    assert params["timezone"] == zone


@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "Europe/rome", "Europe/Rome ", "CEST", "+01:00",
                                  "../../etc/passwd", "Factory", "localtime", "posixrules", "Etc/GMT+13"])
def test_unknown_or_non_zone_values_are_refused(zone):
    with pytest.raises(ParameterError) as info:
        validate("apifootball_fixtures", {"id": 1, "timezone": zone})
    assert str(info.value) in ("TIMEZONE_NOT_IN_CATALOG", "INVALID_TEXT_PARAMETER")


def test_timezone_not_certified_code_is_gone():
    with pytest.raises(ParameterError) as info:
        validate("apifootball_fixtures", {"id": 1, "timezone": "Mars/Base"})
    assert str(info.value) != "TIMEZONE_NOT_CERTIFIED"


def test_empty_catalog_fails_closed_but_utc_still_works(monkeypatch):
    monkeypatch.setattr(tz, "timezone_catalog", lambda: frozenset())
    assert tz.validate_timezone("UTC") is None
    assert tz.validate_timezone("Europe/Rome") == "TIMEZONE_CATALOG_UNAVAILABLE"


def _fixture(fid, ts, zone_name):
    local = datetime.fromtimestamp(ts, ZoneInfo(zone_name) if zone_name != "UTC" else timezone.utc)
    return {"fixture": {"id": fid, "timezone": zone_name, "date": local.isoformat(), "timestamp": ts}}


# 2026 EU DST: starts 2026-03-29 01:00 UTC, ends 2026-10-25 01:00 UTC.
BEFORE_SPRING = int(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc).timestamp())
AFTER_SPRING = int(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc).timestamp())
FIRST_0230 = int(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp())   # 02:30 CEST
SECOND_0230 = int(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).timestamp())  # 02:30 CET


def test_dst_offsets_are_checked_without_ambiguity():
    data = {"response": [_fixture(1, BEFORE_SPRING, "Europe/Rome"), _fixture(2, AFTER_SPRING, "Europe/Rome"),
                         _fixture(3, FIRST_0230, "Europe/Rome"), _fixture(4, SECOND_0230, "Europe/Rome")]}
    offsets = [d["fixture"]["date"][-6:] for d in data["response"]]
    assert offsets == ["+01:00", "+02:00", "+02:00", "+01:00"]
    # Both 02:30 local times of the autumn change are distinct instants, each checked exactly.
    assert data["response"][2]["fixture"]["date"][:19] == data["response"][3]["fixture"]["date"][:19]
    check = tz.offset_consistency(data, "Europe/Rome")
    assert check["consistent"] == 4 and check["inconsistent"] == 0 and check["label"] == "DERIVED"


def test_a_wrong_published_offset_is_reported_not_corrected():
    item = _fixture(9, AFTER_SPRING, "Europe/Rome")
    item["fixture"]["date"] = item["fixture"]["date"][:-6] + "+01:00"  # CET after the switch: wrong
    check = tz.offset_consistency({"response": [item]}, "Europe/Rome")
    assert check["inconsistent"] == 1 and check["inconsistent_fixture_ids"] == [9]


def test_missing_fields_are_not_checkable_never_filled():
    data = {"response": [{"fixture": {"id": 1, "timestamp": None, "date": None}},
                         {"fixture": {"id": 2, "timestamp": 1, "date": "2026-01-01T00:00:00"}}]}
    check = tz.offset_consistency(data, "Europe/Rome")
    assert check["not_checkable"] == 2 and check["consistent"] == 0
    assert data["response"][0]["fixture"]["timestamp"] is None


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


def test_tool_forwards_the_zone_keeps_data_raw_and_labels_the_context():
    payload = {"get": "fixtures", "errors": [], "results": 2, "paging": {"current": 1, "total": 1},
               "response": [_fixture(1, BEFORE_SPRING, "Europe/Rome"), _fixture(2, AFTER_SPRING, "Europe/Rome")]}
    mock = MockFootballClient({"/fixtures": payload})
    reg = _Reg()
    register_tools(reg, Settings(enabled=True, api_key="FAKE"), test_mock=mock)
    out = asyncio.run(reg.tools["apifootball_fixtures"](date="2026-03-29", league=135, season=2025,
                                                         timezone="Europe/Rome"))
    assert out["status"] == "MOCK_VERIFIED"
    assert mock.calls[0][1]["timezone"] == "Europe/Rome"
    assert out["data"] == payload  # raw, unconverted
    assert out["timezone_context"]["requested"] == "Europe/Rome"
    assert out["timezone_context"]["validated_against"] == "PYTHON_ZONEINFO_IANA"
    assert out["timezone_context"]["bridge_conversion"] == "NONE"
    assert out["timezone_check"]["consistent"] == 2


def test_no_timezone_means_no_context():
    mock = MockFootballClient({"/fixtures": {"response": []}})
    reg = _Reg()
    register_tools(reg, Settings(enabled=True, api_key="FAKE"), test_mock=mock)
    out = asyncio.run(reg.tools["apifootball_fixtures"](id=1))
    assert "timezone_context" not in out and "timezone_check" not in out
