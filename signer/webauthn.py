"""Server-side WebAuthn (passkey) verification for owner approvals: ES256 / P-256 only, `cryptography` only.

Network-free (like every signer module). Implements the subset of W3C WebAuthn Level 2 the owner's
approval device needs:
- registration (navigator.credentials.create) with attestation "none": clientDataJSON
  (type "webauthn.create", challenge, origin allowlist), attestationObject CBOR {fmt: "none", attStmt: {},
  authData}, authenticatorData (rpIdHash, UP + UV flags, attested credential data), COSE_Key EC2 / ES256 / P-256;
- assertion (navigator.credentials.get): clientDataJSON (type "webauthn.get", challenge, origin, crossOrigin),
  authenticatorData (rpIdHash, UP + UV, signCount), ECDSA P-256 / SHA-256 signature (DER) over
  authenticatorData || SHA-256(clientDataJSON).

The challenge of an order approval is SHA-256 of the canonical approval envelope
(signer.approval.webauthn_challenge): the authenticator's signature is therefore bound to the exact order
content shown to the owner (binding v2 + human display), the single-use nonce, the TTL and the signer id.

signCount (WebAuthn §6.1.1): when the stored or the new value is non-zero the new value must be strictly
greater, otherwise WEBAUTHN_SIGN_COUNT_REGRESSION (possible cloned authenticator). Many synced passkeys
always report 0: then the counter carries no information and replay protection relies on the single-use
approval nonce (consumed in the order ledger) and on the request state machine of the service.

Attestation "none" means the server does NOT prove the authenticator model or that the key is
hardware-backed: enrolment is an owner-trusted act (one-time enrolment code read on the signer host).
Every refusal is an ApprovalError with a fixed code; no exception text, no key material.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import approval as appr
from .approval import ApprovalError

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40
FLAG_ED = 0x80
COSE_KTY_EC2, COSE_ALG_ES256, COSE_CRV_P256 = 2, -7, 1
MAX_CLIENT_DATA_BYTES = 4096
MAX_AUTH_DATA_BYTES = 4096
MAX_SIGNATURE_BYTES = 80          # DER ECDSA P-256 is at most 72 bytes
MAX_CREDENTIAL_ID_BYTES = 1023
_CBOR_MAX_DEPTH = 8
_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")


# ---------------------------------------------------------------- encodings
def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: Any) -> bytes:
    """Strict unpadded base64url; anything else raises ApprovalError(WEBAUTHN_MALFORMED)."""
    if not isinstance(text, str) or not _B64URL.fullmatch(text) or len(text) % 4 == 1 or len(text) > 16384:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise ApprovalError("WEBAUTHN_MALFORMED") from None
    if b64url_encode(raw) != text:  # non-canonical trailing bits
        raise ApprovalError("WEBAUTHN_MALFORMED")
    return raw


class _Cbor:
    """Minimal strict CBOR decoder (RFC 8949): definite lengths only, no tags, no floats."""

    def __init__(self, data: bytes):
        self.data = data

    def item(self, i: int, depth: int) -> tuple[Any, int]:
        d = self.data
        if depth > _CBOR_MAX_DEPTH or i >= len(d):
            raise ApprovalError("WEBAUTHN_CBOR_INVALID")
        ib = d[i]
        i += 1
        major, info = ib >> 5, ib & 0x1F
        if major == 7:
            simple = {20: False, 21: True, 22: None}
            if info not in simple:
                raise ApprovalError("WEBAUTHN_CBOR_INVALID")
            return simple[info], i
        if info < 24:
            arg = info
        elif info in (24, 25, 26, 27):
            n = 1 << (info - 24)
            if i + n > len(d):
                raise ApprovalError("WEBAUTHN_CBOR_INVALID")
            arg = int.from_bytes(d[i:i + n], "big")
            i += n
        else:
            raise ApprovalError("WEBAUTHN_CBOR_INVALID")  # indefinite length / reserved
        if major == 0:
            return arg, i
        if major == 1:
            return -1 - arg, i
        if major in (2, 3):
            if arg > len(d) - i:
                raise ApprovalError("WEBAUTHN_CBOR_INVALID")
            raw = d[i:i + arg]
            i += arg
            if major == 2:
                return bytes(raw), i
            try:
                return raw.decode("utf-8"), i
            except UnicodeDecodeError:
                raise ApprovalError("WEBAUTHN_CBOR_INVALID") from None
        if major == 4:
            if arg > len(d) - i:
                raise ApprovalError("WEBAUTHN_CBOR_INVALID")
            out = []
            for _ in range(arg):
                v, i = self.item(i, depth + 1)
                out.append(v)
            return out, i
        if major == 5:
            if arg > len(d) - i:
                raise ApprovalError("WEBAUTHN_CBOR_INVALID")
            m: dict[Any, Any] = {}
            for _ in range(arg):
                k, i = self.item(i, depth + 1)
                if type(k) not in (int, str) or k in m:
                    raise ApprovalError("WEBAUTHN_CBOR_INVALID")
                m[k], i = self.item(i, depth + 1)
            return m, i
        raise ApprovalError("WEBAUTHN_CBOR_INVALID")  # major 6: tags are not used by WebAuthn here


def cbor_decode_prefix(data: bytes, offset: int = 0) -> tuple[Any, int]:
    """Decode one CBOR item starting at `offset`; return (value, end offset)."""
    if not isinstance(data, (bytes, bytearray)):
        raise ApprovalError("WEBAUTHN_CBOR_INVALID")
    return _Cbor(bytes(data)).item(offset, 0)


def cbor_decode(data: bytes) -> Any:
    value, end = cbor_decode_prefix(data)
    if end != len(data):
        raise ApprovalError("WEBAUTHN_CBOR_INVALID")
    return value


# ---------------------------------------------------------------- policy and credentials
@dataclass(frozen=True)
class WebAuthnPolicy:
    """Relying party id (a registrable domain or "localhost") and the exact allowed origins."""
    rp_id: str
    allowed_origins: frozenset[str]

    def __post_init__(self):
        if not isinstance(self.rp_id, str) or not re.fullmatch(r"[a-z0-9.-]{1,253}", self.rp_id):
            raise ValueError("WEBAUTHN_POLICY_RP_ID_INVALID")
        if not self.allowed_origins or not all(_origin_ok(o, self.rp_id) for o in self.allowed_origins):
            raise ValueError("WEBAUTHN_POLICY_ORIGIN_INVALID")


def _origin_ok(origin: Any, rp_id: str) -> bool:
    """https://<rp_id or subdomain>[:port]; plain http only for localhost (a browser secure context)."""
    if not isinstance(origin, str):
        return False
    m = re.fullmatch(r"(https?)://([a-z0-9.-]+)(:[0-9]{1,5})?", origin)
    if not m:
        return False
    scheme, host = m.group(1), m.group(2)
    if scheme == "http" and host != "localhost":
        return False
    return host == rp_id or host.endswith("." + rp_id)


@dataclass(frozen=True)
class StoredCredential:
    credential_id: bytes
    public_key: bytes   # SEC1 uncompressed P-256 point (65 bytes)
    sign_count: int


class CredentialStore(Protocol):
    def get(self, credential_id: bytes) -> StoredCredential | None:
        ...

    def update_sign_count(self, credential_id: bytes, old: int, new: int) -> bool:
        """Compare-and-set; False when the stored value is not `old` any more."""
        ...


class InMemoryCredentialStore:
    """For tests and single-process prototypes. The service uses a persistent store."""

    def __init__(self):
        self._creds: dict[bytes, StoredCredential] = {}

    def add(self, cred: StoredCredential) -> None:
        if cred.credential_id in self._creds:
            raise ApprovalError("WEBAUTHN_CREDENTIAL_EXISTS")
        self._creds[cred.credential_id] = cred

    def get(self, credential_id: bytes) -> StoredCredential | None:
        return self._creds.get(bytes(credential_id))

    def update_sign_count(self, credential_id: bytes, old: int, new: int) -> bool:
        cur = self._creds.get(bytes(credential_id))
        if cur is None or cur.sign_count != old or not sign_count_acceptable(old, new):
            return False
        self._creds[cur.credential_id] = StoredCredential(cur.credential_id, cur.public_key, new)
        return True


def sign_count_acceptable(stored: int, new: int) -> bool:
    if stored == 0 and new == 0:
        return True
    return new > stored


# ---------------------------------------------------------------- parsing
@dataclass(frozen=True)
class AuthenticatorData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    credential_id: bytes | None = None
    cose_key: Mapping[Any, Any] | None = None


def parse_authenticator_data(raw: Any) -> AuthenticatorData:
    if not isinstance(raw, (bytes, bytearray)) or not 37 <= len(raw) <= MAX_AUTH_DATA_BYTES:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    raw = bytes(raw)
    rp_hash, flags, count = raw[:32], raw[32], int.from_bytes(raw[33:37], "big")
    if not flags & FLAG_AT:
        if flags & FLAG_ED:
            cbor_decode(raw[37:])  # extensions must be one well-formed CBOR map and nothing else
        elif len(raw) != 37:
            raise ApprovalError("WEBAUTHN_MALFORMED")
        return AuthenticatorData(rp_hash, flags, count)
    i = 37 + 16  # aaguid
    if len(raw) < i + 2:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    n = int.from_bytes(raw[i:i + 2], "big")
    i += 2
    if not 16 <= n <= MAX_CREDENTIAL_ID_BYTES or len(raw) < i + n:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    cred_id = raw[i:i + n]
    key, end = cbor_decode_prefix(raw, i + n)
    if flags & FLAG_ED:
        _, end = cbor_decode_prefix(raw, end)
    if end != len(raw) or not isinstance(key, dict):
        raise ApprovalError("WEBAUTHN_MALFORMED")
    return AuthenticatorData(rp_hash, flags, count, cred_id, key)


def cose_es256_public_key(cose: Any) -> bytes:
    """COSE_Key (RFC 9053) EC2 / ES256 / P-256 -> SEC1 uncompressed point; the point is validated."""
    if not isinstance(cose, Mapping) or not set(cose) <= {1, 2, 3, -1, -2, -3}:
        raise ApprovalError("WEBAUTHN_COSE_KEY_UNSUPPORTED")
    if cose.get(1) != COSE_KTY_EC2 or cose.get(3) != COSE_ALG_ES256 or cose.get(-1) != COSE_CRV_P256:
        raise ApprovalError("WEBAUTHN_COSE_KEY_UNSUPPORTED")
    x, y = cose.get(-2), cose.get(-3)
    if not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
        raise ApprovalError("WEBAUTHN_COSE_KEY_UNSUPPORTED")
    try:
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                            ec.SECP256R1()).public_key()
    except ValueError:
        raise ApprovalError("WEBAUTHN_COSE_KEY_INVALID") from None
    return pub.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _client_data(raw: Any, expected_type: str, challenge: bytes, policy: WebAuthnPolicy) -> None:
    if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= MAX_CLIENT_DATA_BYTES:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    try:
        cd = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApprovalError("WEBAUTHN_MALFORMED") from None
    if not isinstance(cd, dict):
        raise ApprovalError("WEBAUTHN_MALFORMED")
    if cd.get("type") != expected_type:
        raise ApprovalError("WEBAUTHN_TYPE_INVALID")
    try:
        got = b64url_decode(cd.get("challenge"))
    except ApprovalError:
        raise ApprovalError("WEBAUTHN_CHALLENGE_MISMATCH") from None
    if not hmac.compare_digest(got, challenge):
        raise ApprovalError("WEBAUTHN_CHALLENGE_MISMATCH")
    if cd.get("origin") not in policy.allowed_origins:
        raise ApprovalError("WEBAUTHN_ORIGIN_NOT_ALLOWED")
    if cd.get("crossOrigin", False) is not False:
        raise ApprovalError("WEBAUTHN_CROSS_ORIGIN")
    if "topOrigin" in cd:
        raise ApprovalError("WEBAUTHN_CROSS_ORIGIN")


def _flags_and_rp(ad: AuthenticatorData, policy: WebAuthnPolicy) -> None:
    if not hmac.compare_digest(ad.rp_id_hash, hashlib.sha256(policy.rp_id.encode("ascii")).digest()):
        raise ApprovalError("WEBAUTHN_RP_ID_MISMATCH")
    if not ad.flags & FLAG_UP:
        raise ApprovalError("WEBAUTHN_USER_NOT_PRESENT")
    if not ad.flags & FLAG_UV:
        raise ApprovalError("WEBAUTHN_USER_NOT_VERIFIED")


# ---------------------------------------------------------------- registration and assertion
def verify_registration(*, attestation_object: Any, client_data_json: Any, expected_challenge: bytes,
                        policy: WebAuthnPolicy) -> StoredCredential:
    """Attestation "none" only. Returns the credential to store (never trusted for the device model)."""
    _client_data(client_data_json, "webauthn.create", expected_challenge, policy)
    if not isinstance(attestation_object, (bytes, bytearray)) or len(attestation_object) > 8192:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    att = cbor_decode(bytes(attestation_object))
    if not isinstance(att, dict) or set(att) != {"fmt", "attStmt", "authData"}:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    if att["fmt"] != "none" or att["attStmt"] != {}:
        raise ApprovalError("WEBAUTHN_ATTESTATION_UNSUPPORTED")
    ad = parse_authenticator_data(att["authData"])
    _flags_and_rp(ad, policy)
    if ad.credential_id is None or ad.cose_key is None:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    return StoredCredential(ad.credential_id, cose_es256_public_key(ad.cose_key), ad.sign_count)


def verify_assertion(cred: StoredCredential, *, policy: WebAuthnPolicy, challenge: bytes, authenticator_data: Any,
                     client_data_json: Any, signature: Any) -> int:
    """Return the new signCount (to commit) or raise ApprovalError. Never mutates the store."""
    _client_data(client_data_json, "webauthn.get", challenge, policy)
    ad = parse_authenticator_data(authenticator_data)
    _flags_and_rp(ad, policy)
    if not sign_count_acceptable(cred.sign_count, ad.sign_count):
        raise ApprovalError("WEBAUTHN_SIGN_COUNT_REGRESSION")
    if not isinstance(signature, (bytes, bytearray)) or not 8 <= len(signature) <= MAX_SIGNATURE_BYTES:
        raise ApprovalError("WEBAUTHN_MALFORMED")
    try:
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), cred.public_key)
        pub.verify(bytes(signature), bytes(authenticator_data) + hashlib.sha256(bytes(client_data_json)).digest(),
                   ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError):
        raise ApprovalError("WEBAUTHN_SIGNATURE_INVALID") from None
    return ad.sign_count


class WebAuthnApprovalVerifier:
    """Verifies owner approvals made with a registered passkey. Used by signer.core and by the service."""

    def __init__(self, policy: WebAuthnPolicy, store: CredentialStore):
        if not isinstance(policy, WebAuthnPolicy):
            raise ValueError("WEBAUTHN_POLICY_INVALID")
        self.policy = policy
        self._store = store

    def verify_challenge(self, *, credential_id: Any, challenge: bytes, authenticator_data: Any,
                         client_data_json: Any, signature: Any) -> int:
        if not isinstance(credential_id, (bytes, bytearray)) or not 16 <= len(credential_id) <= MAX_CREDENTIAL_ID_BYTES:
            raise ApprovalError("WEBAUTHN_MALFORMED")
        cred = self._store.get(bytes(credential_id))
        if cred is None:
            raise ApprovalError("WEBAUTHN_CREDENTIAL_UNKNOWN")
        return verify_assertion(cred, policy=self.policy, challenge=challenge, authenticator_data=authenticator_data,
                                client_data_json=client_data_json, signature=signature)

    def commit(self, credential_id: bytes, new_count: int) -> None:
        cred = self._store.get(bytes(credential_id))
        if cred is None:
            raise ApprovalError("WEBAUTHN_CREDENTIAL_UNKNOWN")
        if not sign_count_acceptable(cred.sign_count, new_count) or \
                not self._store.update_sign_count(cred.credential_id, cred.sign_count, new_count):
            raise ApprovalError("WEBAUTHN_SIGN_COUNT_REGRESSION")

    def verify_order_approval(self, approval: Any, *, expected_intent_hash: str, signer_id: str, now: int,
                              decision: str = appr.DECISION_APPROVE) -> int:
        """Envelope + display + passkey checks for an order approval; returns the signCount to commit."""
        appr.check_webauthn_envelope(approval, expected_intent_hash=expected_intent_hash, signer_id=signer_id,
                                     now=now)
        challenge = appr.webauthn_challenge(appr.WEBAUTHN_ORDER_DOMAIN, decision=decision, binding=approval.binding,
                                            display=approval.display, approval_nonce=approval.approval_nonce,
                                            issued_at=approval.issued_at, expires_at=approval.expires_at,
                                            signer_id=approval.signer_id)
        return self.verify_challenge(credential_id=approval.credential_id, challenge=challenge,
                                     authenticator_data=approval.authenticator_data,
                                     client_data_json=approval.client_data_json, signature=approval.signature)
