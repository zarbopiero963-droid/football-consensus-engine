import json
from bridge.api_football.full_contract_audit import audit_contract_coverage
from bridge.api_football.catalog import ALLOWED_PATHS


def test_no_unverified_route_can_claim_100_percent():
    result = audit_contract_coverage()
    assert result["route_count"] == len(ALLOWED_PATHS) == 39
    assert result["implemented_routes"] == 39
    assert result["verdict"] == "INCOMPLETE"
    assert result["coverage_percent"] == 0
    assert all(r["status"] == "NOT_CERTIFIED" for r in result["routes"])


def test_real_http_alone_does_not_certify_all_parameters():
    proof = {"/odds": {"documentation": "VERIFIED_OFFICIAL_SECTION",
                       "real_response": "REAL_HTTP_200_OBSERVED",
                       "verified_parameters": ["fixture"]}}
    result = audit_contract_coverage(certified=proof)
    odds = next(r for r in result["routes"] if r["path"] == "/odds")
    assert odds["status"] == "NOT_CERTIFIED"
    assert odds["parameters_verified"] == 1


def test_100_requires_explicit_documentation_response_and_all_parameters():
    from bridge.api_football.contracts import ENDPOINTS
    proof = {}
    for path in ALLOWED_PATHS:
        fields = {f for c in ENDPOINTS.values() if c.path == path for f in c.fields}
        proof[path] = {"documentation": "VERIFIED_OFFICIAL_SECTION",
                       "real_response": "REAL_HTTP_200_OBSERVED",
                       "verified_parameters": sorted(fields)}
    r = audit_contract_coverage(certified=proof)
    assert r["coverage_percent"] == 100
    assert r["verdict"] == "PASS"
    assert r["catalog_completeness_against_future_versions"] == "NOT_VERIFIED"
