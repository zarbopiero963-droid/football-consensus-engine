#!/usr/bin/env python3
"""Offline validator, snapshot writer and semantic change detector."""
from __future__ import annotations
import argparse, json
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.api_football.registry import build_registry, canonical_json, sha256, validate_registry

def _items(registry):
    result={}
    for endpoint in registry["endpoints"]: result["endpoint:"+endpoint["id"]]=endpoint
    for tool in registry["tools"]:
        result["tool:"+tool["id"]]=tool
        for parameter in tool["parameters"]: result["parameter:"+parameter["id"]]=parameter
    return result

def semantic_diff(previous, current):
    old,new=_items(previous),_items(current); changes=[]
    for key in sorted(set(old)|set(new)):
        if key not in old:
            classification = "BREAKING" if key.startswith("parameter:") and new[key].get("required") is True else "ADDITIVE"
            changes.append({"key":key,"kind":"added","classification":classification})
        elif key not in new: changes.append({"key":key,"kind":"removed","classification":"BREAKING"})
        else:
            before,after=old[key],new[key]
            for field in ("required","type","format","enum","minimum","maximum","pagination","response_schema","error_schema","official_status","notes"):
                if before.get(field)!=after.get(field):
                    classification = "DOCUMENTATION_ONLY" if field == "notes" else "UNKNOWN"
                    if field in {"type","format","enum","minimum","maximum","pagination","response_schema","error_schema"}: classification="BREAKING"
                    elif field=="required" and after.get(field) is True: classification="BREAKING"
                    elif field=="enum" and set(after.get(field, [])) < set(before.get(field, [])): classification="BREAKING"
                    elif field in {"minimum","maximum"}: classification="BREAKING"
                    changes.append({"key":key,"kind":field+"_changed","classification":classification})
            if before.get("source_section") != after.get("source_section"):
                changes.append({"key":key,"kind":"source_changed","classification":"DOCUMENTATION_ONLY"})
    return changes

def candidate_gap_report(registry, evidence_path):
    evidence=json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("authoritative") is True: raise ValueError("CANDIDATE_EVIDENCE_MUST_NOT_BE_AUTHORITATIVE")
    if evidence.get("status") != "PARTIAL_EVIDENCE_ONLY": raise ValueError("CANDIDATE_EVIDENCE_STATUS_INVALID")
    if type(evidence.get("authoritative")) is not bool or evidence["authoritative"]:
        raise ValueError("CANDIDATE_EVIDENCE_MUST_NOT_BE_AUTHORITATIVE")
    candidates=evidence.get("endpoints")
    if not isinstance(candidates,dict):
        raise ValueError("CANDIDATE_ENDPOINTS_INVALID")
    allowed={item["path"] for item in registry["endpoints"]}
    if any(path not in allowed for path in candidates):
        raise ValueError("CANDIDATE_UNKNOWN_ENDPOINT")
    if any(not isinstance(names,list) or any(
        not isinstance(name,str) or not name.isidentifier() for name in names
    ) for names in candidates.values()):
        raise ValueError("CANDIDATE_PARAMETERS_INVALID")
    fields_by_path={}
    for tool in registry["tools"]:
        fields_by_path.setdefault(tool["path"],set()).update(item["name"] for item in tool["parameters"])
    gaps=[]
    for path,names in sorted(evidence.get("endpoints",{}).items()):
        gaps.append({"path":path,"candidate_only":sorted(names),"runtime_missing":sorted(set(names)-fields_by_path.get(path,set())),"promotion_allowed":False})
    return {"status":"PENDING_OFFICIAL_VERIFICATION","gaps":gaps}

def read_snapshot(path):
    payload=json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"metadata","registry","sha256"}: raise ValueError("SNAPSHOT_SCHEMA_INVALID")
    if payload["sha256"] != sha256(payload["registry"]): raise ValueError("SNAPSHOT_HASH_MISMATCH")
    return payload

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--write",type=Path)
    parser.add_argument("--compare",type=Path)
    parser.add_argument("--candidate-evidence",type=Path)
    args=parser.parse_args()
    registry=build_registry(); errors=validate_registry(registry)
    if errors: raise SystemExit("\n".join(errors))
    payload={"metadata":{"format":"api-football-contract-snapshot/v1","created_at_utc":datetime.now(timezone.utc).isoformat()},"registry":registry,"sha256":sha256(registry)}
    if args.write:
        args.write.parent.mkdir(parents=True,exist_ok=True)
        args.write.write_text(json.dumps(payload,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    if args.candidate_evidence:
        print(json.dumps(candidate_gap_report(registry,args.candidate_evidence),sort_keys=True))
    if args.compare:
        previous=read_snapshot(args.compare)["registry"]
        changes=semantic_diff(previous,registry)
        print(json.dumps({"changes":changes},sort_keys=True))
        if any(item["classification"] in {"BREAKING","UNKNOWN"} for item in changes): return 2
    else:
        print(json.dumps({"endpoint_count":len(registry["endpoints"]),"tool_count":len(registry["tools"]),"sha256":payload["sha256"]},sort_keys=True))
    return 0
if __name__=="__main__": raise SystemExit(main())
