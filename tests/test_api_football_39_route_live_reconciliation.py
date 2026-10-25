from pathlib import Path
from bridge.api_football.five_point_verdict import assess_five_points
from scripts.verify_api_football_evidence import verify_archive

FILE=Path("evidence/api_football/route_inventory_40211464376.b64")
SHA="08b34b6f438a62b0b8917ee151df991a921a8ff8fa253867066716fa752044d4"

def test_39_route_historical_evidence_is_sha_verified_and_not_falsely_full():
    report=verify_archive(FILE,SHA)
    assert report["route_count"]==39
    assert len(report["verified_paths"])==38
    assert "/odds/live" not in report["verified_paths"]
    verdict=assess_five_points([],historical_verified_paths=report["verified_paths"])
    assert verdict["coverage"]["status"]=="PARTIAL"
    assert verdict["coverage"]["missing_known_routes"]==["/odds/live"]

def test_actual_live_http200_closes_only_the_historical_path_gap():
    report=verify_archive(FILE,SHA)
    records=[{"path":"/odds/live","http_status":200,"status":"OBSERVED",
              "odds_observations":[{"phase":"LIVE","fixture_id":1}]}]
    verdict=assess_five_points(records,historical_verified_paths=report["verified_paths"])
    assert verdict["coverage"]["status"]=="PASS_SCOPE"
    assert verdict["coverage"]["verified_known_routes"]==39
    assert verdict["coverage"]["parameter_documentation_certified"] is False

def test_bookmakers_cannot_pass_without_any_observed_prices():
    records=[{"path":"/odds/bookmakers","status":"OBSERVED","http_status":200,
              "page":1,"pagination":{"current":1,"total":1},
              "catalog":[{"id":3,"name":"Betfair"}]}]
    verdict=assess_five_points(records)
    assert verdict["bookmakers"]["status"]=="PARTIAL"
