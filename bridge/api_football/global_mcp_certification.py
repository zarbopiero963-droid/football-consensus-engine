"""Fail-closed global MCP certification manifest for a pinned deployed release.

Offline manifest generation NEVER implies that a real browser, provider, quota
coordinator, OAuth flow or ChatGPT connector was exercised.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from bridge.api_football.catalog import ALLOWED_PATHS
from bridge.api_football.contracts import ENDPOINTS
from bridge.api_football.full_contract_audit import audit_contract_coverage

MANDATORY_DOMAINS = (
    "api_contract", "api_live", "mcp_api_tools", "browser_tools",
    "oauth", "render_deploy", "neon_quota", "chatgpt_e2e",
    "security_recovery", "independent_review",
)
VALID = frozenset(("PASS", "FAIL", "BLOCKED", "NOT_VERIFIED"))
PINNED_BASELINE = "ee4cabd1cde5a15256651f0a4db6af814cc1bf15"


def inventory() -> dict[str, Any]:
    """Deterministic contract inventory; browser discovery requires a live read."""
    items = sorted(ENDPOINTS)
    return {
        "baseline_commit": PINNED_BASELINE,
        "api_paths": sorted(ALLOWED_PATHS),
        "api_path_count": len(ALLOWED_PATHS),
        "api_tools": items,
        "api_tool_count": len(items),
        "browser_tools": None,
        "browser_tool_count": None,
        "browser_discovery": "NOT_VERIFIED",
        "route_contract": audit_contract_coverage(),
    }


def verdict(evidence: dict[str, Any], *, deployed_commit: str | None = None,
            evidence_root: str | Path = 'evidence/certification') -> dict[str, Any]:
    """Only authenticated per-domain evidence on the exact commit can PASS."""
    if not isinstance(evidence, dict):
        raise ValueError("INVALID_EVIDENCE")
    rows = []
    root = Path(evidence_root).resolve()
    for domain in MANDATORY_DOMAINS:
        item = evidence.get(domain)
        valid = isinstance(item, dict) and item.get("status") in VALID
        documented = valid and bool(item.get("evidence_uri")) and bool(item.get("sha256"))
        digest = item.get("sha256") if valid else None
        digest_ok = isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        matching = documented and digest_ok and item.get("commit") == PINNED_BASELINE
        # Verify actual local evidence bytes, not merely a claimed hash or URL.
        if matching:
            uri = item["evidence_uri"]
            if not isinstance(uri, str) or "://" in uri:
                matching = False
            else:
                candidate = (root / uri).resolve()
                try:
                    candidate.relative_to(root)
                    matching = candidate.is_file() and hashlib.sha256(
                        candidate.read_bytes()).hexdigest() == digest
                except (ValueError, OSError):
                    matching = False
        state = item["status"] if matching else "NOT_VERIFIED"
        rows.append({"domain": domain, "status": state,
                     "evidence_attached": bool(matching)})
    deploy_match = deployed_commit == PINNED_BASELINE
    complete = all(x["status"] == "PASS" for x in rows) and deploy_match
    return {"baseline_commit": PINNED_BASELINE,
            "deployed_commit_match": deploy_match,
            "status": "CERTIFIED" if complete else "NO_GO",
            "domain_results": rows,
            "scope": "VERSION_PINNED_FUNCTIONAL_AND_SECURITY_CONTRACT",
            "global_future_data_completeness_claim": False}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
