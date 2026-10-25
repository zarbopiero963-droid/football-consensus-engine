#!/usr/bin/env python3
"""Manual-only capped catalog probe. No raw response or secrets in logs."""
import asyncio
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
    output = asyncio.run(run(os.environ))
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "/tmp/api-football-capability-summary.md"))
    lines = ["### API-FOOTBALL catalog inventory", "", "Catalog does not prove bookie/market availability or full pagination.", "",
             "| Endpoint | State | Records | Pages |", "|---|---|---:|---|"]
    for row in output:
        lines.append("| {} | {} | {} | {} |".format(row["path"], row["status"], row.get("count", "-"), row.get("pagination", "-")))
    summary.open("a").write(chr(10).join(lines) + chr(10))
    print("Catalog paths evaluated: {}".format(len(output)))
