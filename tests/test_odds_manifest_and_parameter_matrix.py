from bridge.api_football.odds_manifest import odds_page_manifest
from bridge.api_football.contracts import ENDPOINTS
from scripts.print_api_football_parameter_matrix import matrix
import pytest

def sample(n=3):
    return {"data":{"get":"odds","paging":{"current":1,"total":2},
      "response":[{"fixture":{"id":100+i},"bookmakers":[{"id":4,"bets":[
        {"id":5,"values":[{"value":"Over 2.5","odd":"1.80"},
                          {"value":"Under 2.5","odd":"2.00"}]}]}]} for i in range(n)]}}

def test_manifest_accurate_counts_and_partial_flag():
    result=odds_page_manifest(sample())
    assert result["partial"] is True
    assert result["prices_included"] is False
    assert result["fixtures_in_page"]==3
    assert result["market_groups_in_page"]==3
    assert result["selections_in_page"]==6
    assert result["fixture_list_complete"] is True
    assert result["paging"]=={"current":1,"total":2}
    assert "1.80" not in str(result)

def test_manifest_does_not_pretend_complete_if_capped():
    result=odds_page_manifest(sample(),max_fixtures=2)
    assert result["fixture_list_complete"] is False
    assert len(result["fixture_ids"])==2

def test_malformed_fails_closed():
    with pytest.raises(ValueError):
        odds_page_manifest({"data":{"get":"odds","response":[{}],"paging":{}}})

def test_existing_official_fields_present_in_source():
    assert "ids" in ENDPOINTS["apifootball_fixtures"].fields
    assert "half" in ENDPOINTS["apifootball_fixture_statistics"].fields
    rows={entry["tool"]:entry for entry in matrix()}
    assert rows["apifootball_fixtures"]["connected_plugin_schema"]=="REQUIRES_INDEPENDENT_DISCOVERY"
    assert all(x["provider_real_coverage"]=="NOT_EXHAUSTIVELY_VERIFIED" for x in rows.values())
