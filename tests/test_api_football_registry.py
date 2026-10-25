from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest
from bridge.api_football.catalog import ALLOWED_PATHS
from bridge.api_football.contracts import ENDPOINTS
from bridge.api_football.registry import build_registry, canonical_json, sha256, validate_registry

SPEC=importlib.util.spec_from_file_location("contract_check",Path("scripts/check_api_football_contracts.py"))
MODULE=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)

def test_registry_matches_runtime_allowlist_and_tools():
    registry=build_registry()
    assert not validate_registry(registry)
    assert {entry["path"] for entry in registry["endpoints"]} == ALLOWED_PATHS
    assert {entry["id"] for entry in registry["tools"]} == set(ENDPOINTS)
    assert all(entry["official_status"] == "PENDING_OFFICIAL_VERIFICATION" for entry in registry["endpoints"])

def test_hash_is_deterministic_for_normalized_registry():
    registry=build_registry()
    assert canonical_json(registry) == canonical_json(deepcopy(registry))
    assert sha256(registry) == sha256(deepcopy(registry))
    assert sha256(registry) == hashlib.sha256(canonical_json(registry).encode("utf-8")).hexdigest()

@pytest.mark.parametrize("mutation,classification",[
    ("add_endpoint","ADDITIVE"),("remove_endpoint","BREAKING"),
    ("add_required_parameter","BREAKING"),("remove_parameter","BREAKING"),
    ("type","BREAKING"),("enum","BREAKING"),("minimum","BREAKING"),
    ("required","BREAKING"),("response_schema","BREAKING"),("notes","DOCUMENTATION_ONLY"),
])
def test_semantic_detector_classifies_adversarial_changes(mutation,classification):
    old=build_registry(); new=deepcopy(old)
    if mutation=="add_endpoint":
        new["endpoints"].append({"id":"route:/synthetic","path":"/synthetic","method":"GET","tool_ids":[],"official_status":"UNKNOWN","source":None,"source_section":None,"response_schema":{"status":"UNKNOWN"},"error_schema":{"status":"UNKNOWN"}})
    elif mutation=="remove_endpoint": new["endpoints"].pop()
    else:
        tool=next(item for item in new["tools"] if item["parameters"])
        if mutation=="add_required_parameter":
            tool["parameters"].append({"id":tool["id"]+".synthetic","name":"synthetic","required":True,"type":"UNKNOWN","format":None,"enum":[],"minimum":None,"maximum":None,"status":"UNKNOWN","implementation_status":"IMPLEMENTED","source":None})
        elif mutation=="remove_parameter": tool["parameters"].pop()
        elif mutation=="type": tool["parameters"][0]["type"]="integer"
        elif mutation=="enum": tool["parameters"][0]["enum"]=["one"]
        elif mutation=="minimum": tool["parameters"][0]["minimum"]=1
        elif mutation=="required": tool["parameters"][0]["required"]=True
        elif mutation=="response_schema": tool["response_schema"]={"status":"DOCUMENTED"}
        elif mutation=="notes": tool["notes"]="documentation text"
    changes=MODULE.semantic_diff(old,new)
    assert any(item["classification"]==classification for item in changes)

def test_snapshot_hash_mismatch_is_rejected(tmp_path):
    path=tmp_path/"bad.json"
    path.write_text(json.dumps({"metadata":{},"registry":build_registry(),"sha256":"0"*64}))
    with pytest.raises(ValueError,match="SNAPSHOT_HASH_MISMATCH"):
        MODULE.read_snapshot(path)

def test_real_verified_cannot_be_smuggled_into_offline_registry():
    registry=build_registry()
    registry["tools"][0]["parameters"][0]["status"]="REAL_VERIFIED"
    assert "REAL_VERIFIED_FORBIDDEN" in validate_registry(registry)
