#!/usr/bin/env python3
"""One-command fail-closed verification of global MCP certification evidence.

Run: python scripts/run_full_mcp_certification.py --evidence-dir evidence/certification
This is NOT a live 76-tool runner. It only verifies genuine independently
acquired evidence and refuses success if any required item is absent.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from bridge.api_football.global_mcp_certification import PINNED_BASELINE, verdict
from bridge.api_football.tool_evidence_gate import verify_all_tools


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence/certification"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/fcb-global-e2e-result.json"))
    parser.add_argument("--deployed-sha", default="", help="Observed Render deployment SHA, not an assumed value")
    args = parser.parse_args()
    root = args.evidence_dir
    domain_file = root / "domains.json"
    tool_file = root / "tools.json"
    try:
        domains = json.loads(domain_file.read_text()) if domain_file.is_file() else {}
        tools = json.loads(tool_file.read_text()) if tool_file.is_file() else {}
        tools_result = verify_all_tools(tools, root=root)
        final = verdict(domains, deployed_commit=args.deployed_sha,
                        evidence_root=root, tool_evidence=tools)
        # Require a complete, separately observed provider route/parameter contract.
        contract = domains.get("api_contract", {})
        final["official_parameter_reconciliation_complete"] = (
            isinstance(contract, dict) and contract.get("parameter_matrix_status") == "PASS"
        )
        if not final["official_parameter_reconciliation_complete"]:
            final["status"] = "NO_GO"
        final["observed_tool_proof_count"] = tools_result["verified"]
        final["required_tool_proof_count"] = tools_result["required"]
        final["result_note"] = "PASS means submitted evidence was hash-verified; independent examination of underlying real execution remains mandatory."
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        final = {"status": "NO_GO", "error": type(exc).__name__}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(final, sort_keys=True, indent=2) + "\n")
    print(json.dumps({key: final.get(key) for key in
        ("status", "deployed_commit_match", "observed_tool_proof_count",
         "required_tool_proof_count", "official_parameter_reconciliation_complete")},
        sort_keys=True))
    return 0 if final.get("status") == "CERTIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
