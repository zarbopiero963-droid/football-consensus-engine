#!/usr/bin/env python3
"""Manual-only capped catalog probe. No raw response or secrets in logs."""
import asyncio
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path

import httpx
from bridge.api_football.config import Settings, OFFICIAL_BASE_URL
from bridge.api_football.capability_inventory import inventory_payload

PATHS = ("/odds/bookmakers", "/odds/bets", "/odds/live/bets", "/fixtures/statistics")
CAP = 4

async def run(env, *, transport=None):
    cfg = Settings.from_env(env)
    if not cfg.real_requests_authorized or cfg.base_url != OFFICIAL_BASE_URL or cfg.max_daily_requests < CAP:
        raise ValueError("LIVE_INVENTORY_NOT_AUTHORIZED")
    evidence = []
    async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, trust_env=False,
                                 follow_redirects=False, timeout=cfg.timeout_seconds, transport=transport) as client:
        for path in PATHS:
            # Statistics require a provider-observed fixture; do not invent one.
            if path == "/fixtures/statistics":
                evidence.append({"path": path, "status": "BLOCKED_REQUIRES_OBSERVED_FIXTURE"})
                continue
            try:
                async with client.stream("GET", path, headers={"x-apisports-key": cfg.api_key, "Accept": "application/json"}) as response:
                    if response.status_code != 200:
                        evidence.append({"path": path, "status": "HTTP_ERROR", "code": response.status_code})
                        break
                    payload = await response.aread()
                    if len(payload) > 524288:
                        evidence.append({"path": path, "status": "TOO_LARGE"})
                        break
                    parsed = json.loads(payload)
                    row = inventory_payload(path, parsed)
                    row["response_sha256"] = hashlib.sha256(payload).hexdigest()
                    row["http_status"] = response.status_code
                    row["response_bytes"] = len(payload)
                    paging = parsed.get("paging") if isinstance(parsed, dict) else None
                    row["pagination"] = {k: paging.get(k) for k in ("current", "total")} if isinstance(paging, dict) else None
                    evidence.append(row)
                    if row["status"] not in ("OBSERVED", "NO_DATA_OBSERVED"):
                        break
                    headers = response.headers
                    if headers.get("x-ratelimit-remaining") == "0" or headers.get("x-ratelimit-requests-remaining") == "0":
                        break
            except (httpx.RequestError, ValueError, UnicodeDecodeError):
                evidence.append({"path": path, "status": "NETWORK_OR_PAYLOAD_ERROR"})
                break
    return evidence

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("/tmp/apifootball_capability_evidence"))
    args = parser.parse_args()
    output = asyncio.run(run(os.environ))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "capabilities.json").write_text(json.dumps(output, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8")
    with (args.out_dir / "catalogs.csv").open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=["source_path", "phase", "id", "name"])
        writer.writeheader()
        for result in output:
            for item in result.get("catalog", []):
                writer.writerow({"source_path":result["path"],"phase":result.get("phase","UNKNOWN"),"id":item["id"],"name":item["name"]})
    with (args.out_dir / "field_registry.csv").open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=["source_path", "json_path", "types"])
        writer.writeheader()
        for result in output:
            for json_path, types in result.get("fields", {}).items():
                writer.writerow({"source_path":result["path"],"json_path":json_path,"types":",".join(types)})
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "/tmp/api-football-capability-summary.md"))
    lines = ["### API-FOOTBALL catalog inventory", "", "Catalog does not prove bookie/market availability or full pagination.", "",
             "| Endpoint | State | Records | Pages |", "|---|---|---:|---|"]
    for row in output:
        lines.append("| {} | {} | {} | {} |".format(row["path"], row["status"], row.get("count", "-"), row.get("pagination", "-")))
    summary.open("a").write(chr(10).join(lines) + chr(10))
    print("Catalog paths evaluated: {}".format(len(output)))
