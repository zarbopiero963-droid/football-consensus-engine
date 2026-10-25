#!/usr/bin/env python3
"""Verify historical artifact-quota-independent API-FOOTBALL evidence, offline."""
from __future__ import annotations
import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

ARCHIVES = {
    "40205480029": (
        Path("evidence/api_football/route_inventory_40205480029.b64"),
        "9ac706a73c58dc106d810ac6c220774c5090e439d40eccaae0f1e6e955184333",
    ),
    "40211464376": (
        Path("evidence/api_football/route_inventory_40211464376.b64"),
        "08b34b6f438a62b0b8917ee151df991a921a8ff8fa253867066716fa752044d4",
    ),
}
ANOMALIES = ("PASS_EMPTY_NOT_AVAILABILITY", "BLOCKED_MISSING_REAL_PREREQUISITES")


def verify_archive(file: Path, expected_sha256: str) -> dict:
    raw = base64.b64decode(file.read_text(encoding="ascii").strip(), validate=True)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("EVIDENCE_HASH_MISMATCH")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if any(name.startswith("/") or ".." in Path(name).parts for name in archive.namelist()):
            raise ValueError("UNSAFE_EVIDENCE_PATH")
        if "route_inventory.json" not in archive.namelist():
            raise ValueError("MISSING_ROUTE_INVENTORY")
        if archive.getinfo("route_inventory.json").file_size > 500000:
            raise ValueError("EVIDENCE_TOO_LARGE")
        data = json.loads(archive.read("route_inventory.json"))
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError("INVALID_RECORDS")
    anomalies = [{
        "path": item.get("path"),
        "outcome": item.get("outcome"),
        "http_status": item.get("http_status"),
        "results": item.get("results"),
    } for item in records if item.get("outcome") in ANOMALIES]
    return {
        "archive_sha256": expected_sha256, "route_count": data.get("route_count"),
        "requests_attempted": data.get("requests_attempted"),
        "anomalies": anomalies,
    }


if __name__ == "__main__":
    for run_id, (file, sha) in ARCHIVES.items():
        result = verify_archive(file, sha)
        print(json.dumps({"run_id": run_id, **result}, ensure_ascii=False, sort_keys=True))
