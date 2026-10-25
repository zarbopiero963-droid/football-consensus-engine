import pytest
from bridge.api_football.capability_inventory import fields, classify_market, inventory_payload

def test_null_zero_missing_and_bool_remain_distinct():
    x=fields([{"odd":"0","main":None,"goals":0,"active":False}, {"odd":"2.5","main":True}])
    assert x["$[].main"] == {"null", "boolean"}
    assert x["$[].goals"] == {"int"}
    assert x["$[].odd"] == {"str"}

@pytest.mark.parametrize("v,expected",[(None,"NULL"),(True,"TRUE"),(False,"FALSE")])
def test_live_tri_state(v,expected):
    assert classify_market({"id":3,"main":v},live=True)["main"] == expected

def test_missing_main_never_assumed_false():
    assert classify_market({"id":3},live=True)["main"] == "MISSING"

def test_prematch_catalog_does_not_assert_live_availability():
    x=inventory_payload("/odds/bookmakers",{"response":[{"id":3,"name":"Betfair"},{"id":4,"name":"Pinnacle"}],"errors":{}})
    assert x["phase"]=="PREMATCH"
    assert x["catalog"]==[{"id":3,"name":"Betfair"},{"id":4,"name":"Pinnacle"}]
    assert "live" not in str(x).lower()

def test_empty_response_is_no_data_not_pass():
    assert inventory_payload("/odds/live/bets",{"response":[],"errors":{}})["status"]=="NO_DATA_OBSERVED"

def test_provider_error_never_passes():
    assert inventory_payload("/odds/bets",{"response":[{"id":1}],"errors":{"plan":"denied"}})["status"]=="PROVIDER_ERROR"

def test_unknown_key_types_are_not_projected():
    x=fields({5:"bad","ok":3})
    assert "$.ok" in x and "$.5" not in x
