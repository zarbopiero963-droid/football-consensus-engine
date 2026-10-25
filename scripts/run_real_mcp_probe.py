#!/usr/bin/env python3
"""Bounded real provider probe, plus honest MCP/Browser E2E evidence gaps.

This runner performs LIVE HTTP probes only for valid documented endpoint
samples. It does not impersonate the OAuth-authenticated ChatGPT MCP client.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import httpx
from bridge.api_football.catalog import ALLOWED_PATHS
from bridge.api_football.contracts import ENDPOINTS, validate, ParameterError
from bridge.api_football.config import OFFICIAL_BASE_URL

CAP = 60
EXAMPLES = {
    "/status": {}, "/timezone": {}, "/countries": {}, "/leagues/seasons": {},
    "/leagues": {"id":39}, "/teams": {"league":39,"season":2026},
    "/fixtures": {"league":39,"season":2026,"next":1},
    "/odds/bookmakers": {}, "/odds/bets": {}, "/odds/live/bets": {},
    "/teams/countries": {}, "/players/seasons": {}, "/teams/seasons":{"team":33},
}
def plan():
    result=[]
    for tool, spec in sorted(ENDPOINTS.items()):
        params=EXAMPLES.get(spec.path)
        if params is None: continue
        if not set(spec.required)<=set(params):continue
        try:
            _, query=validate(tool,params)
        except ParameterError:continue
        result.append((tool,spec.path,query))
    return result

async def run(out: Path):
    key=os.environ.get("APIFOOTBALL_API_KEY")
    authorized=os.environ.get("APIFOOTBALL_LIVE_AUTHORIZED")=="true"
    rows=[]
    out.mkdir(parents=True,exist_ok=True)
    if not key or not authorized:
        raise SystemExit("NO_GO: LIVE authorization/secret missing")
    samples=plan()
    if len(samples)>CAP:
        raise SystemExit("NO_GO: bounded cap exceeded")
    async with httpx.AsyncClient(base_url=OFFICIAL_BASE_URL,timeout=25,
                                 trust_env=False,follow_redirects=False) as client:
        for index,(tool,path,params) in enumerate(samples):
            if index: await asyncio.sleep(7.5)
            try:
                response=await client.get(path,params=params,
                    headers={"x-apisports-key":key,"Accept":"application/json"})
                raw=response.content
                good=response.status_code==200 and len(raw)<=2_097_152
                payload=response.json() if good else {}
                good=good and isinstance(payload,dict) and payload.get("errors") in ({},[],None)
                rows.append({"tool":tool,"path":path,"parameters":params,
                   "http_status":response.status_code,"status":"OBSERVED" if good else "BLOCKED",
                   "response_sha256":hashlib.sha256(raw).hexdigest(),
                   "provider_results":payload.get("results") if good else None})
                if not good:break
            except (httpx.HTTPError,ValueError):
                rows.append({"tool":tool,"path":path,"status":"BLOCKED_REQUEST_ERROR"})
                break
    missing=sorted(set(ALLOWED_PATHS)-{x["path"] for x in rows if x["status"]=="OBSERVED"})
    report={"scope":"AUTHORIZED_PROVIDER_HTTP_NOT_MCP_E2E",
        "sampled":len(rows),"required_known_routes":len(ALLOWED_PATHS),
        "not_tested_routes":missing,
        "unproven_mcp_tool_calls":76,
        "official_parameter_reconciliation":"NOT_CERTIFIED",
        "verdict":"NO_GO",
        "records":rows}
    (out/"provider_samples.json").write_text(json.dumps(report,sort_keys=True,indent=2)+"\n")
    print(json.dumps({k:report[k] for k in ("sampled","required_known_routes",
        "unproven_mcp_tool_calls","verdict")},sort_keys=True))
    return 1

if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path,default=Path("/tmp/fcb-real-provider"))
    args=p.parse_args()
    raise SystemExit(asyncio.run(run(args.out)))
