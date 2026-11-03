"""FCB-WP-011: the archived SX Bet read-only audit evidence is intact, bound and sanitized."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "sxbet" / "readonly_audit_run41320538088.json"
SHA256 = "2582808205479f6023bcbd7f3a83359459b55f122f3d91b66be5458e7d07db9b"


def test_archived_report_is_byte_identical_to_the_artifact():
    assert hashlib.sha256(EVIDENCE.read_bytes()).hexdigest() == SHA256
    readme = (ROOT / "evidence" / "sxbet" / "README.md").read_text(encoding="utf-8")
    assert SHA256 in readme and "41320538088" in readme
    assert "d569e6e65f2777b36e0def2e8ea2bdcd46030b78" in readme


def test_report_is_read_only_and_matches_the_registered_outcome():
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert data["mode"] == "READ_ONLY" and data["network"] == "mainnet"
    assert data["write_operations_attempted"] == 0
    assert data["outcome_counts"] == {"PASS": 46, "EMPTY_VALID": 13, "NOT_TESTED": 3, "PARTIAL": 1}
    assert data["api_key_present"] is True  # a boolean, never the key


def test_report_contains_no_secret_or_wallet_material():
    text = EVIDENCE.read_text(encoding="utf-8")
    assert not re.search(r'"0x[0-9a-fA-F]{40}"', text)  # no bare EVM address
    for value in re.findall(r'"([^"]{24,})"', text):
        if value.startswith("0x") and len(value) == 66:
            continue  # public market hashes
        # opaque token shape: long, no spaces/underscores, letters AND digits (keys, JWT parts)
        token_like = re.fullmatch(r"[A-Za-z0-9\-.]{32,}", value) and re.search(r"\d", value) and re.search(r"[A-Za-z]", value)
        assert not token_like, value
    assert "<own address>" in text  # the account address is a placeholder, never the value
    lowered = text.lower()
    for word in ("private_key", "privatekey", "mnemonic", "seed phrase", "authorization", "bearer "):
        assert word not in lowered, word
