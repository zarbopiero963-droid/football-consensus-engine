from bridge.api_football.capability_inventory import inventory_payload

def test_team_statistics_response_is_valid_object():
    payload={"errors":[],"response":{"league":{"id":210,"season":2026},"team":{"id":616},
              "fixtures":{"played":{"total":5}},"goals":{"for":{"total":{"total":8}}}}}
    result=inventory_payload("/teams/statistics",payload)
    assert result["status"]=="OBSERVED"
    assert result["count"]==1
    assert result["response_kind"]=="object"
    assert "$.fixtures.played.total" in result["fields"]

def test_unexpected_dict_for_bookmakers_does_not_claim_coverage():
    assert inventory_payload("/odds/bookmakers",{"errors":[],"response":{"id":1}})["status"]=="NO_LIST_RESPONSE"
