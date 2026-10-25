from bridge.api_football.coverage_certification import pagination_verdict, odds_observations

def test_pagination_not_complete_when_cap_hit():
    rows=[{"path":"/odds/bets","page":i,"status":"OBSERVED","pagination":{"total":12}} for i in range(1,11)]
    assert pagination_verdict(rows,"/odds/bets")["status"]=="INCOMPLETE"

def test_full_pagination_certified_when_all_pages_observed():
    rows=[{"path":"/odds/bets","page":i,"status":"OBSERVED","pagination":{"total":2}} for i in (1,2)]
    assert pagination_verdict(rows,"/odds/bets")["status"]=="COMPLETE"

def test_no_response_never_complete():
    assert pagination_verdict([],"/odds/live/bets")["status"]=="NOT_VERIFIED"

def test_prematch_odds_are_bookmaker_scoped():
    payload={"response":[{"fixture":{"id":123},"bookmakers":[{"id":3,"name":"Betfair","bets":[{"id":5,"name":"Over/Under Goals","values":[{"value":"Over 2.5","odd":"1.84"}]}]}]}]}
    items=odds_observations(payload,phase="PREMATCH")
    assert len(items)==1
    assert items[0]["bookmaker_id"]==3
    assert items[0]["market_name"]=="Over/Under Goals"
    assert items[0]["odd"]=="1.84"
    assert items[0]["phase"]=="PREMATCH"

def test_no_bookmaker_in_live_does_not_invent_one():
    p={"response":[{"fixture":123,"bets":[{"id":1,"name":"1X2","main":None,"values":[{"value":"Home","odd":"2.10"}]}]}]}
    result=odds_observations(p,phase="LIVE")
    assert result[0]["bookmaker_id"] is None
    assert result[0]["main"] is None
