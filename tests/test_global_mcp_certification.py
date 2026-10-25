from bridge.api_football.global_mcp_certification import (
    PINNED_BASELINE, MANDATORY_DOMAINS, inventory, verdict,
)

def test_all_known_api_routes_and_tools_inventory():
    data=inventory()
    assert data["api_path_count"]==39
    assert data["api_tool_count"]==40  # 40 typed provider contracts mapped onto 39 paths
    assert data["browser_discovery"]=="NOT_VERIFIED"
    assert data["route_contract"]["verdict"]=="INCOMPLETE"

def test_missing_real_evidence_blocks_certificate():
    assert verdict({},deployed_commit=PINNED_BASELINE)["status"]=="NO_GO"

def test_wrong_release_cannot_certify(tmp_path):
    import hashlib
    payload = b"reproducible-example-evidence-only"
    (tmp_path / "signed.txt").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    records={domain:{"status":"PASS","sha256":digest,
                     "evidence_uri":"signed.txt","commit":PINNED_BASELINE}
             for domain in MANDATORY_DOMAINS}
    assert verdict(records,deployed_commit="wrong",evidence_root=tmp_path)["status"]=="NO_GO"
    assert verdict(records,deployed_commit=PINNED_BASELINE,evidence_root=tmp_path)["status"]=="NO_GO"  # Missing 76 execution proofs
    # Any file edit invalidates the certificate.
    (tmp_path / "signed.txt").write_bytes(b"tampered")
    assert verdict(records,deployed_commit=PINNED_BASELINE,evidence_root=tmp_path)["status"]=="NO_GO"

def test_malformed_and_wrong_commit_evidence_fail_closed():
    records={"oauth":{"status":"PASS","sha256":"not-a-digest",
                     "evidence_uri":"test","commit":PINNED_BASELINE},
             "api_live":{"status":"PASS","sha256":"a"*64,
                     "evidence_uri":"test","commit":"different"}}
    outcome=verdict(records,deployed_commit=PINNED_BASELINE)
    assert outcome["status"]=="NO_GO"
    assert all(x["status"]=="NOT_VERIFIED" for x in outcome["domain_results"])
