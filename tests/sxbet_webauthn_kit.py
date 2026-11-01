"""Synthetic WebAuthn authenticator for offline tests: a THROWAWAY P-256 key generated per test, no browser.

Builds registration (attestation "none") and assertion responses exactly as a platform authenticator
(e.g. Android Chrome + screen lock) would: authenticatorData, clientDataJSON, DER ECDSA signature.
"""
from __future__ import annotations

import hashlib
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from signer.webauthn import FLAG_AT, FLAG_UP, FLAG_UV, b64url_encode

RP_ID = "localhost"
ORIGIN = "http://localhost:8787"


def cbor(v) -> bytes:
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
        raise ValueError
    if v is False:
        return b"\xf4"
    if v is True:
        return b"\xf5"
    if v is None:
        return b"\xf6"
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        b = v.encode()
        return head(3, len(b)) + b
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(type(v))


class FakePasskey:
    def __init__(self, rp_id=RP_ID, origin=ORIGIN, sign_count=0, counter_step=1):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.rp_id, self.origin = rp_id, origin
        self.sign_count, self.counter_step = sign_count, counter_step

    def _cose(self):
        n = self.key.public_key().public_numbers()
        return {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}

    def _client_data(self, typ, challenge, origin=None, **extra):
        return json.dumps({"type": typ, "challenge": b64url_encode(challenge), "origin": origin or self.origin,
                           "crossOrigin": False, **extra}).encode()

    def register(self, challenge, *, flags=FLAG_UP | FLAG_UV | FLAG_AT, rp_id=None, fmt="none", att_stmt=None,
                 origin=None):
        ad = (hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([flags])
              + self.sign_count.to_bytes(4, "big") + b"\x00" * 16 + len(self.credential_id).to_bytes(2, "big")
              + self.credential_id + cbor(self._cose()))
        att = cbor({"fmt": fmt, "attStmt": att_stmt or {}, "authData": ad})
        return {"attestation_object": att, "client_data_json": self._client_data("webauthn.create", challenge, origin)}

    def assert_(self, challenge, *, flags=FLAG_UP | FLAG_UV, rp_id=None, origin=None, sign_count=None,
                typ="webauthn.get", **cd_extra):
        if sign_count is None:
            self.sign_count += self.counter_step
            sign_count = self.sign_count
        ad = hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([flags]) + sign_count.to_bytes(4, "big")
        cdj = self._client_data(typ, challenge, origin, **cd_extra)
        sig = self.key.sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"credential_id": self.credential_id, "authenticator_data": ad, "client_data_json": cdj,
                "signature": sig}


def to_wire(parts: dict) -> dict:
    """The JSON the approval page POSTs (base64url fields)."""
    return {k: b64url_encode(v) for k, v in parts.items()}
