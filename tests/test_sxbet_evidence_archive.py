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


MARKET_HASH_KEYS = {"marketHash", "probe_market_hash"}


def _walk(node, key=None):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)
    else:
        yield key, node


def test_report_contains_no_secret_or_wallet_material():
    text = EVIDENCE.read_text(encoding="utf-8")
    # No EVM address anywhere, also embedded inside a longer string.
    assert not re.search(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])", text)
    for key, value in _walk(json.loads(text)):
        if not isinstance(value, str):
            continue
        # 32-byte hex is allowed ONLY as a public market hash; elsewhere it could be a private key.
        if re.search(r"0x[0-9a-fA-F]{64}", value):
            assert key in MARKET_HASH_KEYS and re.fullmatch(r"0x[0-9a-fA-F]{64}", value), key
        assert not re.search(r"\beyJ[A-Za-z0-9_-]{8,}\.", value), key  # JWT
        for word in re.findall(r"[A-Za-z0-9_\-.+/=]{32,}", value):
            if re.fullmatch(r"0x[0-9a-fA-F]{64}", word):
                continue
            # opaque token shape: upper AND lower case AND digits (snake_case names are lowercase)
            assert not (re.search(r"[a-z]", word) and re.search(r"[A-Z]", word) and re.search(r"\d", word)), key
    assert "<own address>" in text  # the account address is a placeholder, never the value
    lowered = text.lower()
    for word in ("private_key", "privatekey", "mnemonic", "seed phrase", "authorization", "bearer "):
        assert word not in lowered, word


def test_the_guard_catches_planted_secrets(tmp_path, monkeypatch):
    """The checks above must fail on realistic leaks (review of #170)."""
    import tests.test_sxbet_evidence_archive as mod
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    for planted in ({"wallet": "account:orders_v3_#0x" + "ab" * 20},
                    {"note": "0x" + "1f" * 32},
                    {"token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc_DEF-123"},
                    {"key": "sk_live_" + "Ab1" * 12}):
        leaked = tmp_path / "leak.json"
        leaked.write_text(json.dumps({**data, "planted": planted}), encoding="utf-8")
        monkeypatch.setattr(mod, "EVIDENCE", leaked)
        try:
            mod.test_report_contains_no_secret_or_wallet_material()
        except AssertionError:
            continue
        raise AssertionError(f"leak not detected: {planted}")
