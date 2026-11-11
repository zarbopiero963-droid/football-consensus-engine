"""FIX-03 (G02): /fixtures combinations. Documentary evidence only: the captured official
contract lists independent optional filters and no combination rule, so a combination is
DOCUMENTED_VALID only for a single documented anchor (plus timezone), league+season (season is
documented as "the season of the league") and league+season+from+to (also real PASS). Every
other scoped combination is DOCS_UNVERIFIED (forwarded, labelled); unscoped or contradictory
sets are MCP_REJECTED with a fixed code. last/next <= 99."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bridge.api_football.client import MockFootballClient
from bridge.api_football.config import Settings
from bridge.api_football.contracts import ParameterError, fixtures_combination, validate
from bridge.api_football.tools import register_tools

MATRIX = Path(__file__).resolve().parents[1] / "docs/api_football/official_parameter_matrix_v3.9.3.json"
D = "2026-11-10"

DOCUMENTED = [
    ({"id": 1}, "F-ID"), ({"id": 1, "timezone": "Europe/Rome"}, "F-ID"),
    ({"ids": "1-2-3"}, "F-IDS"), ({"live": "all"}, "F-LIVE"), ({"live": "39-61"}, "F-LIVE"),
    ({"live": "all", "timezone": "UTC"}, "F-LIVE"), ({"date": D}, "F-DATE"),
    ({"date": D, "timezone": "Europe/Rome"}, "F-DATE"),
    ({"league": 39, "season": 2026}, "F-LEAGUE-SEASON"),
    ({"league": 39, "season": 2026, "from": D, "to": "2026-11-17", "timezone": "UTC"}, "F-LEAGUE-SEASON-RANGE"),
]

UNVERIFIED = [  # the combinations the old guard blocked, and the ones it allowed without evidence
    {"team": 33, "season": 2026}, {"venue": 556, "season": 2026},
    {"league": 39, "next": 10}, {"league": 39, "last": 10}, {"team": 33, "next": 5}, {"team": 33, "last": 5},
    {"venue": 556, "next": 3}, {"next": 15}, {"last": 15}, {"league": 39, "last": 10, "status": "FT"},
    {"team": 33, "last": 10, "timezone": "Europe/London"},
    {"team": 33, "season": 2026, "from": D, "to": "2026-12-31"},
    {"team": 33, "from": D, "to": "2026-12-31"},
    {"league": 39, "season": 2026, "round": "Regular Season - 1"},
    {"league": 39, "season": 2026, "status": "NS-PST-FT"}, {"league": 39, "season": 2026, "team": 33},
    {"date": D, "league": 39, "season": 2026}, {"date": D, "status": "FT"}, {"date": D, "league": 39},
    {"live": "all", "status": "1H"}, {"live": "39-61", "league": 39}, {"live": "39"},
    {"id": 1, "ids": "2-3"},
]

REJECTED = [
    ({}, "FIXTURE_FILTER_REQUIRED"), ({"league": 39}, "FIXTURE_FILTER_REQUIRED"),
    ({"team": 9}, "FIXTURE_FILTER_REQUIRED"), ({"venue": 9}, "FIXTURE_FILTER_REQUIRED"),
    ({"season": 2026}, "FIXTURE_FILTER_REQUIRED"), ({"status": "NS"}, "FIXTURE_FILTER_REQUIRED"),
    ({"round": "Regular Season - 1"}, "FIXTURE_FILTER_REQUIRED"), ({"timezone": "UTC"}, "FIXTURE_FILTER_REQUIRED"),
    ({"from": D, "to": "2026-11-12"}, "FIXTURE_FILTER_REQUIRED"), ({"team": 9, "from": D}, "FIXTURE_FILTER_REQUIRED"),
    ({"league": 39, "team": 33}, "FIXTURE_FILTER_REQUIRED"),
    ({"team": 33, "last": 5, "next": 5}, "FIXTURE_LAST_NEXT_EXCLUSIVE"),
    ({"league": 39, "season": 2026, "from": "2026-12-01", "to": D}, "INVALID_DATE_RANGE"),
    ({"team": 33, "last": 100}, "PAGE_OR_BATCH_TOO_LARGE"), ({"next": 100}, "PAGE_OR_BATCH_TOO_LARGE"),
    ({"last": 101, "team": 9}, "PAGE_OR_BATCH_TOO_LARGE"),
]


@pytest.mark.parametrize("args,rule", DOCUMENTED)
def test_documented_valid(args, rule):
    _, params = validate("apifootball_fixtures", args)
    assert fixtures_combination(params) == {"status": "DOCUMENTED_VALID", "rule": rule}


@pytest.mark.parametrize("args", UNVERIFIED)
def test_docs_unverified_are_forwarded_and_labelled(args):
    _, params = validate("apifootball_fixtures", args)
    out = fixtures_combination(params)
    assert out["status"] == "DOCS_UNVERIFIED" and out["rule"].startswith("F-COMBINATION-")


@pytest.mark.parametrize("args,code", REJECTED)
def test_mcp_rejected(args, code):
    with pytest.raises(ParameterError) as info:
        validate("apifootball_fixtures", args)
    assert str(info.value) == code


@pytest.mark.parametrize("field", ["last", "next"])
def test_last_next_boundary_is_99(field):
    assert validate("apifootball_fixtures", {"team": 33, field: 99})[1][field] == 99
    assert validate("apifootball_fixtures", {"team": 33, field: 1})[1][field] == 1
    for bad in (0, -1, 100, True, "5"):
        with pytest.raises(ParameterError):
            validate("apifootball_fixtures", {"team": 33, field: bad})


def test_documented_rules_rest_on_the_capture_only():
    doc = json.loads(MATRIX.read_text())
    fixtures = next(e for e in doc["endpoints"] if e["path"] == "/fixtures")
    params = {p["name"]: p["constraints_and_description"] for p in fixtures["query_parameters"]}
    for args, _ in DOCUMENTED:
        assert set(args) <= set(params)
    assert "The season of the league" in params["season"]
    assert "<= 2 characters" in params["last"] and "<= 2 characters" in params["next"]
    # The capture records no combination rule at all: nothing beyond these is DOCUMENTED_VALID.
    assert not any(k in json.dumps(fixtures).lower() for k in ("combin", "requires", "required with"))


def test_odds_rules_are_unchanged():
    # FIX-03 touches /fixtures only: /odds still needs fixture, league+season or date.
    for args in ({"league": 39}, {"season": 2026}, {"bet": 5}):
        with pytest.raises(ParameterError) as info:
            validate("apifootball_odds", args)
        assert str(info.value) == "ODDS_FILTER_REQUIRED"
    assert validate("apifootball_odds", {"league": 39, "season": 2026})[1] == {"league": 39, "season": 2026}


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


def test_the_reply_carries_the_combination_label():
    mock = MockFootballClient({"/fixtures": {"response": []}})
    reg = _Reg()
    register_tools(reg, Settings(enabled=True, api_key="FAKE"), test_mock=mock)
    out = asyncio.run(reg.tools["apifootball_fixtures"](team=33, season=2026))
    assert out["status"] == "MOCK_VERIFIED"
    assert out["combination"] == {"status": "DOCS_UNVERIFIED", "rule": "F-COMBINATION-season+team"}
    assert mock.calls == [("/fixtures", {"team": 33, "season": 2026})]
    documented = asyncio.run(reg.tools["apifootball_fixtures"](id=5))
    assert documented["combination"]["status"] == "DOCUMENTED_VALID"
    refused = asyncio.run(reg.tools["apifootball_fixtures"](league=39))
    assert refused["status"] == "INVALID_INPUT" and refused["network_calls_made"] == 0
