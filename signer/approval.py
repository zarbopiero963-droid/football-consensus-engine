"""Out-of-band owner approval: Ed25519 over a canonical message, single use, short TTL.

The owner's approval device holds the Ed25519 PRIVATE key; the signer holds only
the PUBLIC key. A chat message, a tool output or a web page cannot produce a
valid approval because none of them has the owner's private approval key: the
signer, not the model, verifies the signature (governance §2.6).

Canonical message (sorted-key compact JSON, ASCII):
    {"approval_nonce", "domain": "sxbet.order_approval.v1", "expires_at",
     "intent_hash", "issued_at", "signer_id"}
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

APPROVAL_DOMAIN = "sxbet.order_approval.v1"
MAX_TTL_SECONDS = 120
CLOCK_SKEW_SECONDS = 5
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class ApprovalError(ValueError):
    pass


@dataclass(frozen=True)
class Approval:
    intent_hash: str
    approval_nonce: str
    issued_at: int
    expires_at: int
    signer_id: str
    signature: bytes  # 64-byte Ed25519 signature over approval_message(...)

    def __repr__(self) -> str:
        return (f"Approval(intent_hash={self.intent_hash!r}, nonce={self.approval_nonce[:6]}..., "
                f"expires_at={self.expires_at}, signer_id={self.signer_id!r})")


def approval_message(*, intent_hash: str, approval_nonce: str, issued_at: int, expires_at: int,
                     signer_id: str) -> bytes:
    return json.dumps({"domain": APPROVAL_DOMAIN, "intent_hash": intent_hash, "approval_nonce": approval_nonce,
                       "issued_at": issued_at, "expires_at": expires_at, "signer_id": signer_id},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 32:
        raise ApprovalError("OWNER_PUBLIC_KEY_INVALID")
    return Ed25519PublicKey.from_public_bytes(bytes(raw))


def verify(approval: Any, public_key: Ed25519PublicKey, *, expected_intent_hash: str, signer_id: str,
           now: int) -> None:
    """Raise ApprovalError(code) unless the approval is authentic, bound and fresh. Nonce not consumed here."""
    if not isinstance(approval, Approval):
        raise ApprovalError("APPROVAL_MISSING")
    if not isinstance(approval.intent_hash, str) or not _HASH.fullmatch(approval.intent_hash):
        raise ApprovalError("APPROVAL_MALFORMED")
    if not isinstance(approval.approval_nonce, str) or not _NONCE.fullmatch(approval.approval_nonce):
        raise ApprovalError("APPROVAL_MALFORMED")
    if type(approval.issued_at) is not int or type(approval.expires_at) is not int:
        raise ApprovalError("APPROVAL_MALFORMED")
    if not isinstance(approval.signature, (bytes, bytearray)) or len(approval.signature) != 64:
        raise ApprovalError("APPROVAL_MALFORMED")
    if approval.signer_id != signer_id:
        raise ApprovalError("APPROVAL_WRONG_SIGNER")
    if approval.intent_hash != expected_intent_hash:
        raise ApprovalError("APPROVAL_INTENT_MISMATCH")
    if approval.expires_at - approval.issued_at > MAX_TTL_SECONDS or approval.expires_at <= approval.issued_at:
        raise ApprovalError("APPROVAL_TTL_TOO_LONG")
    if approval.issued_at > now + CLOCK_SKEW_SECONDS:
        raise ApprovalError("APPROVAL_NOT_YET_VALID")
    if now >= approval.expires_at:
        raise ApprovalError("APPROVAL_EXPIRED")
    msg = approval_message(intent_hash=approval.intent_hash, approval_nonce=approval.approval_nonce,
                           issued_at=approval.issued_at, expires_at=approval.expires_at, signer_id=approval.signer_id)
    try:
        public_key.verify(bytes(approval.signature), msg)
    except InvalidSignature:
        raise ApprovalError("APPROVAL_SIGNATURE_INVALID") from None


class NonceStore:
    """Single-use nonces. With a path, used nonces are appended (fsync) and survive restarts."""

    def __init__(self, path: str | None = None):
        self._path = path
        self._used: set[str] = set()
        self._lock = threading.Lock()
        if path and os.path.exists(path):
            with open(path, encoding="ascii") as fh:
                self._used.update(line.strip() for line in fh if line.strip())

    def consume(self, nonce: str) -> bool:
        """True if newly consumed; False if already used (replay)."""
        with self._lock:
            if nonce in self._used:
                return False
            if self._path:
                with open(self._path, "a", encoding="ascii") as fh:
                    fh.write(nonce + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            self._used.add(nonce)
            return True
