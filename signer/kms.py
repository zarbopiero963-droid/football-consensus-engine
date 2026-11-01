"""Digest-signer adapter: turn an external ECDSA secp256k1 DER signature into an Ethereum 65-byte signature.

Scope (owner mandate SXBET-SECURE-SIGNER-PREPARATION-01 §4). Network-free: this module contains no
client for AWS KMS, Google Cloud KMS, Azure Key Vault or any HSM, and imports no cloud SDK. A concrete
backend is a separate, owner-approved component that implements `DigestSigner`; nothing here calls it on
its own. See docs/sxbet/kms_compatibility.md for the verified provider contracts.

Why an adapter is needed (all verified on the official docs, see the doc):
- the EIP-712 order digest (keccak256(0x1901 || domainSeparator || structHash), signer/eip712.py) is ALREADY
  the 32-byte value ECDSA must sign. A KMS must therefore sign it as a pre-computed digest (AWS
  MessageType=DIGEST, GCP `digest` field, Azure Sign = "sign hash"). Sending it as a message (AWS
  MessageType=RAW, GCP `data`) makes the KMS SHA-256 it again: the result is a valid ECDSA signature over
  sha256(digest), which recovers to a DIFFERENT address and is useless for SX Bet;
- AWS / GCP return the signature DER-encoded (SEQUENCE { INTEGER r, INTEGER s }, RFC 3279 §2.2.3); Ethereum
  needs packed r(32) || s(32) || v(1);
- the DER carries no recovery id: v (27/28) is found by trial ecrecover against the known signer address;
- AWS does not document low-s output, so s may be high: OpenZeppelin ECDSA (and EIP-2) reject s > n/2, so
  s is normalised to n - s (which flips the recovery parity; trial recovery handles that).

Fail-closed: every malformed or non-recovering input raises KmsSignatureError with a fixed code; no
exception message carries the DER, the digest or key material.
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key
from eth_keys import keys
from eth_utils import keccak, to_checksum_address

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
HALF_N = SECP256K1_N // 2
DIGEST_LENGTH = 32
# DER of two 33-byte INTEGERs: 2 + 2 * (2 + 33) = 72 bytes. Anything longer cannot be a secp256k1 signature.
MAX_DER_LENGTH = 72
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class KmsSignatureError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@runtime_checkable
class DigestSigner(Protocol):
    """A backend that ECDSA-signs a 32-byte digest WITHOUT hashing it again and returns DER (r, s).

    AWS KMS: Sign(KeySpec ECC_SECG_P256K1, SigningAlgorithm ECDSA_SHA_256, MessageType DIGEST).
    Google Cloud KMS: asymmetricSign(EC_SIGN_SECP256K1_SHA256, digest={"sha256": digest}).
    The implementation must never return or log key material; this protocol has no key accessor.
    """

    def sign_digest(self, digest: bytes) -> bytes: ...


def _check_digest(digest: object) -> bytes:
    if type(digest) is not bytes or len(digest) != DIGEST_LENGTH:
        raise KmsSignatureError("KMS_DIGEST_INVALID")
    return digest


def _der_int(buf: bytes, pos: int) -> tuple[int, int]:
    if pos + 2 > len(buf) or buf[pos] != 0x02:
        raise KmsSignatureError("KMS_DER_MALFORMED")
    length = buf[pos + 1]
    if length & 0x80:  # long-form length: never needed for <= 33-byte integers
        raise KmsSignatureError("KMS_DER_MALFORMED")
    if length == 0 or length > 33:
        raise KmsSignatureError("KMS_DER_INTEGER_SIZE")
    start, end = pos + 2, pos + 2 + length
    if end > len(buf):
        raise KmsSignatureError("KMS_DER_MALFORMED")
    body = buf[start:end]
    if body[0] & 0x80:  # negative INTEGER
        raise KmsSignatureError("KMS_DER_NEGATIVE_INTEGER")
    if length > 1 and body[0] == 0x00 and not body[1] & 0x80:  # non-minimal leading zero
        raise KmsSignatureError("KMS_DER_NON_MINIMAL")
    return int.from_bytes(body, "big"), end


def parse_der_signature(der: object) -> tuple[int, int]:
    """Strict DER Ecdsa-Sig-Value: SEQUENCE { INTEGER r, INTEGER s }, minimal encoding, no trailing bytes,
    1 <= r, s < n. BER variants (long-form length, padding, extra data) are refused."""
    if type(der) is not bytes or not 8 <= len(der) <= MAX_DER_LENGTH:
        raise KmsSignatureError("KMS_DER_LENGTH")
    if der[0] != 0x30 or der[1] & 0x80 or der[1] != len(der) - 2:
        raise KmsSignatureError("KMS_DER_MALFORMED")
    r, pos = _der_int(der, 2)
    s, pos = _der_int(der, pos)
    if pos != len(der):
        raise KmsSignatureError("KMS_DER_TRAILING_DATA")
    if not (1 <= r < SECP256K1_N and 1 <= s < SECP256K1_N):
        raise KmsSignatureError("KMS_SIGNATURE_OUT_OF_RANGE")
    return r, s


def normalize_low_s(s: int) -> int:
    """EIP-2 / OpenZeppelin canonical form: s in [1, n/2]."""
    if not 1 <= s < SECP256K1_N:
        raise KmsSignatureError("KMS_SIGNATURE_OUT_OF_RANGE")
    return SECP256K1_N - s if s > HALF_N else s


def _recover(digest: bytes, r: int, s: int, parity: int) -> str | None:
    try:
        pub = keys.Signature(vrs=(parity, r, s)).recover_public_key_from_msg_hash(digest)
    except Exception:  # r not an x-coordinate for this parity, point at infinity, ...
        return None
    return pub.to_checksum_address()


def der_to_ethereum_signature(digest: bytes, der: bytes, expected_address: str) -> bytes:
    """65-byte r || s || v (v in {27, 28}, low-s) that ecrecovers `digest` to `expected_address`.

    Refuses when neither recovery id yields the expected address (wrong key, re-hashed digest, wrong
    digest) and when both would (impossible for a valid signature; refused rather than guessed)."""
    digest = _check_digest(digest)
    if not isinstance(expected_address, str) or not _ADDRESS.fullmatch(expected_address):
        raise KmsSignatureError("KMS_EXPECTED_ADDRESS_INVALID")
    want = to_checksum_address(expected_address)
    r, s = parse_der_signature(der)
    s = normalize_low_s(s)
    hits = [p for p in (0, 1) if _recover(digest, r, s, p) == want]
    if len(hits) != 1:
        raise KmsSignatureError("KMS_SIGNATURE_DOES_NOT_RECOVER_SIGNER")
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + hits[0]])


def address_from_spki_der(spki_der: object) -> str:
    """EIP-55 address of a secp256k1 public key in X.509 SubjectPublicKeyInfo DER (the format AWS
    GetPublicKey returns). Any other curve or key type is refused."""
    if type(spki_der) is not bytes:
        raise KmsSignatureError("KMS_PUBLIC_KEY_INVALID")
    try:
        pub = load_der_public_key(spki_der)
    except Exception:
        raise KmsSignatureError("KMS_PUBLIC_KEY_INVALID") from None
    if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve, ec.SECP256K1):
        raise KmsSignatureError("KMS_PUBLIC_KEY_NOT_SECP256K1")
    point = pub.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return to_checksum_address(keccak(point[1:])[-20:])


class KmsOrderSigner:
    """Signs EIP-712 digests through a DigestSigner, returning Ethereum-format signatures for one address.

    NOT wired into signer.core.Signer: the default signing path (in-process key from
    SXBET_SIGNER_PRIVATE_KEY) is unchanged. Wiring is a separate, owner-merged change
    (docs/sxbet/kms_compatibility.md §7).
    """

    def __init__(self, backend: DigestSigner, address: str):
        if not isinstance(backend, DigestSigner):
            raise KmsSignatureError("KMS_BACKEND_INVALID")
        if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
            raise KmsSignatureError("KMS_EXPECTED_ADDRESS_INVALID")
        self._backend = backend
        self.address = to_checksum_address(address)

    def __repr__(self) -> str:
        return f"KmsOrderSigner(address={self.address})"

    def sign_digest(self, digest: bytes) -> bytes:
        digest = _check_digest(digest)
        try:
            der = self._backend.sign_digest(digest)
        except KmsSignatureError:
            raise
        except Exception:  # a backend message could carry ARNs, endpoints or credentials: never forward it
            raise KmsSignatureError("KMS_BACKEND_FAILED") from None
        return der_to_ethereum_signature(digest, der, self.address)

    def sign_digest_hex(self, digest: bytes) -> str:
        return "0x" + self.sign_digest(digest).hex()
