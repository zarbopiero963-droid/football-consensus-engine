"""Official v3.9.3 documented fixture batch filters: offline contract regression."""
import pytest
from bridge.api_football.contracts import ParameterError, validate


def test_fixture_ids_max_20():
    ids = "-".join(str(i) for i in range(1, 21))
    endpoint, params = validate("apifootball_fixtures", {"ids": ids})
    assert endpoint.path == "/fixtures"
    assert params["ids"] == ids


@pytest.mark.parametrize("value", [
    "", "1-", "-1", "1--2", "0", "01", "1,2",
    "-".join(str(i) for i in range(1, 22)),
    "1?url=http://localhost", True, 12,
])
def test_invalid_fixture_batch_ids_rejected(value):
    with pytest.raises(ParameterError):
        validate("apifootball_fixtures", {"ids": value})


def test_injuries_batch_is_a_valid_filter():
    _, params = validate("apifootball_injuries", {"ids": "123-456"})
    assert params == {"ids": "123-456"}


def test_fixture_ranges_and_round():
    _, params = validate("apifootball_fixtures", {
        "from": "2026-10-24", "to": "2026-10-25",
        "league": 39, "season": 2026, "round": "Regular Season - 9",
        "venue": 10,
    })
    assert params["from"] == "2026-10-24"
    assert params["round"] == "Regular Season - 9"


def test_team_additional_official_filters():
    _, params = validate("apifootball_teams", {"code": "ROM", "venue": 123})
    assert params["venue"] == 123


def test_h2h_additional_official_filters():
    _, params = validate("apifootball_fixture_headtohead", {
        "h2h": "10-20", "venue": 90, "status": "FT"
    })
    assert params["status"] == "FT"
