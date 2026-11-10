"""FIX-02 (G01): /fixtures `live` accepts "all" or the documented dash-separated league id list
("Enum: all | id-id", official parameter matrix v3.9.3). Format, duplicates and invalid values
are refused before any request; the maximum number of ids is not documented (DOCS_UNVERIFIED)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridge.api_football import contracts
from bridge.api_football.contracts import ParameterError, validate

MATRIX = Path(__file__).resolve().parents[1] / "docs/api_football/official_parameter_matrix_v3.9.3.json"


def test_the_official_capture_documents_all_and_id_list():
    doc = json.loads(MATRIX.read_text())
    fixtures = next(e for e in doc["endpoints"] if e["path"] == "/fixtures")
    live = next(p for p in fixtures["query_parameters"] if p["name"] == "live")
    assert '"all"' in live["constraints_and_description"] and '"id-id"' in live["constraints_and_description"]
    assert "Maximum" not in live["constraints_and_description"]  # no documented id limit
    assert contracts.LIVE_IDS_MAXIMUM == "DOCS_UNVERIFIED"


@pytest.mark.parametrize("value", ["all", "39-61", "39-61-135-140-78", "39", "1-2", "2000000000-1"])
def test_accepted(value):
    assert validate("apifootball_fixtures", {"live": value})[1]["live"] == value


@pytest.mark.parametrize("value,code", [
    ("ALL", "INVALID_LIVE_FILTER"), ("private", "INVALID_LIVE_FILTER"), ("", "INVALID_TEXT_PARAMETER"),
    ("39-", "INVALID_LIVE_FILTER"), ("-39", "INVALID_LIVE_FILTER"), ("39--61", "INVALID_LIVE_FILTER"),
    ("39,61", "INVALID_LIVE_FILTER"), ("39 61", "INVALID_LIVE_FILTER"), ("039-61", "INVALID_LIVE_FILTER"),
    ("0-61", "INVALID_LIVE_FILTER"), ("39-61a", "INVALID_LIVE_FILTER"), ("all-39", "INVALID_LIVE_FILTER"),
    ("12345678901-2", "INVALID_LIVE_FILTER"), ("39-61-39", "LIVE_LEAGUE_IDS_DUPLICATE"),
    ("7-7", "LIVE_LEAGUE_IDS_DUPLICATE"), ("39\n61", "INVALID_TEXT_PARAMETER"),
])
def test_refused(value, code):
    with pytest.raises(ParameterError) as info:
        validate("apifootball_fixtures", {"live": value})
    assert str(info.value) == code


def test_no_invented_count_limit_but_the_text_bound_holds():
    many = "-".join(str(i) for i in range(1, 33))  # 32 distinct ids, 86 characters
    assert validate("apifootball_fixtures", {"live": many})[1]["live"] == many
    too_long = "-".join(str(1_000_000 + i) for i in range(20))  # 159 characters
    with pytest.raises(ParameterError) as info:
        validate("apifootball_fixtures", {"live": too_long})
    assert str(info.value) == "INVALID_TEXT_PARAMETER"
