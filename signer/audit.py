"""Append-only JSONL audit of signer decisions (governance §2.6).

Only an allowlist of fields is ever written: no private key, no signature bytes,
no approval signature. Each line carries the sha256 of the previous line, so a
rewritten or deleted entry breaks the chain. A correction is a new entry.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterator

ALLOWED_FIELDS = frozenset({"event", "decision", "reason", "signer_id", "idempotency_key", "intent_hash",
                            "order_id", "total_bet_size", "approval_nonce_prefix", "network"})
GENESIS = "sha256:" + "0" * 64


class AuditLog:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._prev = GENESIS
        for rec in self.records():
            self._prev = rec["_line_sha256"]

    def append(self, **fields: Any) -> None:
        extra = set(fields) - ALLOWED_FIELDS
        if extra:
            raise ValueError("AUDIT_FIELD_NOT_ALLOWED")
        with self._lock:
            rec = {"ts_utc": datetime.now(timezone.utc).isoformat(), "prev_sha256": self._prev, **fields}
            line = json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            with open(self._path, "a", encoding="ascii") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._prev = "sha256:" + hashlib.sha256(line.encode("ascii")).hexdigest()

    def records(self) -> Iterator[dict[str, Any]]:
        """Yield records in order; raise if the hash chain is broken."""
        if not os.path.exists(self._path):
            return
        prev = GENESIS
        with open(self._path, encoding="ascii") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("prev_sha256") != prev:
                    raise ValueError("AUDIT_CHAIN_BROKEN")
                prev = "sha256:" + hashlib.sha256(line.encode("ascii")).hexdigest()
                rec["_line_sha256"] = prev
                yield rec
