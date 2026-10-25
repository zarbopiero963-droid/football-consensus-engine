#!/usr/bin/env python3
"""Offline certificate inventory; prints NO_GO until real per-domain evidence exists."""
import json
import os
from pathlib import Path
from bridge.api_football.global_mcp_certification import inventory, verdict

output = Path(os.environ.get("FCB_CERT_OUT", "/tmp/fcb-global-e2e-certificate.json"))
payload = {"inventory": inventory(), "verdict": verdict({})}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({"api_paths":payload["inventory"]["api_path_count"],
                  "api_contract_tools":payload["inventory"]["api_tool_count"],
                  "browser_discovery":payload["inventory"]["browser_discovery"],
                  "verdict":payload["verdict"]["status"]},sort_keys=True))
