#!/usr/bin/env python3
"""Authorized MCP transport e2e probe (never invents unsafe tool arguments)."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path

URL = "https://football-consensus-browser-bridge.onrender.com/mcp"
SAFE_CALLS = {
    "apifootball_status": {},
    "apifootball_health": {},
    "apifootball_bookmakers": {},
    "apifootball_bets": {},
    "apifootball_live_bets": {},
}
BASELINE="ee4cabd1cde5a15256651f0a4db6af814cc1bf15"


def planned_calls(names: set[str], *, cap: int = 8):
    if cap < 1 or cap > 8:
        raise ValueError("INVALID_CALL_CAP")
    return [(name, args) for name,args in SAFE_CALLS.items() if name in names][:cap]


async def run(output: Path):
    bearer = os.getenv("FCB_MCP_BEARER_TOKEN", "")
    if not bearer:
        raise ValueError("MCP_OAUTH_BEARER_MISSING")
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from bridge.api_football.tool_evidence_gate import expected_tools
    records=[]
    async with httpx2.AsyncClient(headers={"Authorization":"Bearer "+bearer},
                                  timeout=30,trust_env=False,follow_redirects=False) as client:
        async with streamable_http_client(URL,http_client=client) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                discovery=await session.list_tools()
                names={tool.name for tool in discovery.tools}
                expected=set(expected_tools())
                unexpected=sorted(names-expected)
                missing=sorted(expected-names)
                for name, args in planned_calls(names):
                    result=await session.call_tool(name,arguments=args)
                    payload=result.model_dump(mode="json",exclude_none=True)
                    encoded=json.dumps(payload,sort_keys=True,separators=(",",":")).encode()
                    records.append({"tool":name,"arguments":args,
                       "is_error":bool(result.is_error),
                       "sha256":hashlib.sha256(encoded).hexdigest(),
                       "size":len(encoded),
                       "status":"MCP_CALL_SUCCEEDED" if not result.is_error else "MCP_TOOL_ERROR"})
                    await asyncio.sleep(1)
    out={"scope":"AUTHENTICATED_MCP_PROTOCOL_TOOL_CALLS",
         "baseline_commit":BASELINE,"discovered_count":len(names),
         "missing_expected_tools":missing,"unexpected_tools":unexpected,
         "observed_mcp_calls":len(records),
         "unverified_tool_count":len(expected)-sum(r["status"]=="MCP_CALL_SUCCEEDED" for r in records),
         "complete":False,"verdict":"NO_GO","records":records}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    print(json.dumps({key:out[key] for key in ("discovered_count","observed_mcp_calls",
        "unverified_tool_count","verdict")},sort_keys=True))
    return 1


if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path,default=Path("/tmp/fcb-real-mcp.json"))
    args=p.parse_args()
    try: raise SystemExit(asyncio.run(run(args.out)))
    except ValueError as exc: raise SystemExit("NO_GO: "+str(exc))
