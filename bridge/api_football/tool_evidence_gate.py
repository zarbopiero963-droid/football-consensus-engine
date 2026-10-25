"""Per-tool evidence gate: every discovered MCP method requires real proof."""
import hashlib
import json
from pathlib import Path

SNAPSHOT = Path("evidence/mcp/connected_tools_2026-10-25.json")
BASELINE = "ee4cabd1cde5a15256651f0a4db6af814cc1bf15"


def expected_tools():
    observed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    names = observed["api_tools"] + observed["browser_tools"]
    if len(names) != 76 or len(set(names)) != 76:
        raise ValueError("MCP_INVENTORY_DRIFT")
    return sorted(names)


def verify_all_tools(proofs, *, root="evidence/certification"):
    """Never infer successful execution from tools/list discovery."""
    expected = expected_tools()
    home = Path(root).resolve()
    actual = proofs if isinstance(proofs, dict) else {}
    rows = []
    for name in expected:
        item = actual.get(name)
        ok = isinstance(item, dict) and item.get("status") == "PASS" and item.get("commit") == BASELINE
        if ok:
            digest, uri = item.get("sha256"), item.get("file")
            ok = isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
            ok = ok and isinstance(uri, str) and "://" not in uri
        if ok:
            target = (home / uri).resolve()
            try:
                target.relative_to(home)
                ok = target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest
            except (OSError, ValueError):
                ok = False
        rows.append({"tool": name, "status": "PASS" if ok else "NOT_VERIFIED"})
    return {"required": len(expected), "verified": sum(row["status"] == "PASS" for row in rows),
            "status": "PASS" if len(actual) == len(expected) and all(r["status"] == "PASS" for r in rows) else "NO_GO",
            "results": rows}
