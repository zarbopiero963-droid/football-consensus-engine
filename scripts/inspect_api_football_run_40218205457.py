"""Offline, checksum-verified root cause report for manual LIVE provider scan."""
from __future__ import annotations
import base64
import collections
import hashlib
import io
import json
from pathlib import Path
import zipfile

ARCHIVE=Path("evidence/api_football/capability_40218205457.b64")
SHA256="5b79d1b3ef808297ce15c8d8820998f3b61163f40702afca4028e36373a0763e"

def inspect():
    data=base64.b64decode(ARCHIVE.read_text(encoding="ascii").strip(),validate=True)
    if hashlib.sha256(data).hexdigest()!=SHA256:
        raise ValueError("SHA_MISMATCH")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names=z.namelist()
        print("EVIDENCE_FILES", sorted(names))
        rows=json.loads(z.read("capabilities.json"))
        verdict=json.loads(z.read("five_point_verdict.json"))
    print("PROVIDER_ROWS",len(rows))
    for row in rows:
        key=row.get("path")
        if key=="_statistics_samples":
            print("STATS_SUMMARY", json.dumps({"distinct_fixtures":row.get("distinct_fixtures"),"distinct_teams":row.get("distinct_teams"),"sample_count":len(row.get("samples",[]))},sort_keys=True))
            continue
        print("ROW",json.dumps({"path":key,"status":row.get("status"),"parameters":row.get("parameters"),"page":row.get("page"),"count":row.get("count"),"http":row.get("http_status"),"paging":row.get("pagination"),"observed_odds":len(row.get("odds_observations",[])),"reason":row.get("reason")},sort_keys=True))
    print("FIVE_VERDICTS",json.dumps({key:verdict.get(key) for key in ("bookmakers","markets","statistics","live","coverage")},sort_keys=True))
if __name__=="__main__":
    inspect()
