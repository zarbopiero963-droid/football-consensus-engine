"""Regression for official API-Football halftime fixture-statistics query parameter."""
from bridge.api_football.contracts import validate, ParameterError
import pytest

def test_fixture_statistics_half_boolean_mcp_contract():
    spec, params = validate("apifootball_fixture_statistics", {"fixture": 1548550, "half": True})
    assert spec.path == "/fixtures/statistics"
    assert params == {"fixture": 1548550, "half": "true"}

def test_fixture_statistics_half_invalid_fails_closed():
    with pytest.raises(ParameterError):
        validate("apifootball_fixture_statistics", {"fixture": 1548550, "half": "true"})
