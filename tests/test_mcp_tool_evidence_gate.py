import hashlib
from bridge.api_football.tool_evidence_gate import expected_tools, verify_all_tools, BASELINE

def test_exact_live_discovery_inventory():
    tools=expected_tools()
    assert len(tools)==76
    assert len([t for t in tools if t.startswith("apifootball_")])==42
    assert len([t for t in tools if t.startswith("browser_")])==34

def test_empty_or_partial_proof_is_no_go(tmp_path):
    assert verify_all_tools({},root=tmp_path)["status"]=="NO_GO"
    name=expected_tools()[0]
    data=b"test-only-proof"
    (tmp_path/"proof.txt").write_bytes(data)
    one={name:{"status":"PASS","commit":BASELINE,"file":"proof.txt",
               "sha256":hashlib.sha256(data).hexdigest()}}
    result=verify_all_tools(one,root=tmp_path)
    assert result["verified"]==1
    assert result["status"]=="NO_GO"

def test_all_tools_need_real_matching_bytes_and_correct_commit(tmp_path):
    data=b"test-only-not-a-real-production-certification"
    (tmp_path/"proof.txt").write_bytes(data)
    record={"status":"PASS","commit":BASELINE,"file":"proof.txt",
            "sha256":hashlib.sha256(data).hexdigest()}
    all_proofs={name:dict(record) for name in expected_tools()}
    assert verify_all_tools(all_proofs,root=tmp_path)["status"]=="PASS"
    all_proofs[expected_tools()[0]]["commit"]="wrong"
    assert verify_all_tools(all_proofs,root=tmp_path)["status"]=="NO_GO"
    all_proofs[expected_tools()[0]]["commit"]=BASELINE
    (tmp_path/"proof.txt").write_bytes(b"tampered")
    assert verify_all_tools(all_proofs,root=tmp_path)["status"]=="NO_GO"

def test_traversal_and_external_uri_forbidden(tmp_path):
    fake={name:{"status":"PASS","commit":BASELINE,"file":"../secret.txt",
                "sha256":"a"*64} for name in expected_tools()}
    assert verify_all_tools(fake,root=tmp_path)["status"]=="NO_GO"
    fake[expected_tools()[0]]["file"]="https://example.test/proof"
    assert verify_all_tools(fake,root=tmp_path)["status"]=="NO_GO"
