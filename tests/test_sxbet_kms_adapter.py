"""signer/kms.py: DER ECDSA secp256k1 (KMS-style, DIGEST mode) -> Ethereum 65-byte OBv3 order signature.

Owner mandate SXBET-SECURE-SIGNER-PREPARATION-01 §4. Offline only: no cloud SDK, no network, no real key.
The "KMS" is emulated by a THROWAWAY secp256k1 key from `cryptography` signing the 32-byte EIP-712
digest with ECDSA(Prehashed(SHA256)) — the local equivalent of AWS Sign(ECDSA_SHA_256, MessageType=DIGEST):
the digest is signed as is, without a second hash. Recovery is cross-checked with the independent
pure-Python ecrecover and manual EIP-712 encoder of tests/test_sxbet_eip712_official_vectors.py.
"""
import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import (  # noqa: E402
    Prehashed, decode_dss_signature, encode_dss_signature)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import eip712, kms  # noqa: E402
from test_sxbet_eip712_official_vectors import (  # noqa: E402  (independent implementation, reused)
    MAINNET, N, TORONTO, ecrecover, intent_for, manual_digest, order_with_maker, split_sig)


# ---------------------------------------------------------------- KMS emulator (throwaway key, DIGEST mode)
class FakeKms:
    """Emulates a KMS secp256k1 key: sign_digest() = ECDSA over the given 32 bytes, DER output.

    force_s: "high" / "low" / None rewrites s to n - s when needed, to exercise both halves (a real KMS may
    return either; AWS does not document low-s). raw_mode=True reproduces the MessageType=RAW mistake
    (the KMS hashes the digest again with SHA-256)."""

    def __init__(self, *, force_s=None, raw_mode=False):
        self._key = ec.generate_private_key(ec.SECP256K1())
        self.force_s = force_s
        self.raw_mode = raw_mode
        self.calls = 0

    def spki(self) -> bytes:
        return self._key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)

    @property
    def address(self) -> str:
        return kms.address_from_spki_der(self.spki())

    def sign_digest(self, digest: bytes) -> bytes:
        self.calls += 1
        algo = ec.ECDSA(hashes.SHA256()) if self.raw_mode else ec.ECDSA(Prehashed(hashes.SHA256()))
        der = self._key.sign(digest, algo)
        r, s = decode_dss_signature(der)
        if (self.force_s == "high" and s <= N // 2) or (self.force_s == "low" and s > N // 2):
            s = N - s  # (r, n - s) is an equally valid ECDSA signature
        return encode_dss_signature(r, s)


def _digest(maker: str, net: dict, **kw) -> tuple[dict, bytes]:
    order = order_with_maker(maker, net, **kw)
    d = bytes.fromhex(eip712.order_id(eip712.signable(intent_for(order, net)))[2:])
    assert d == manual_digest(order, chain_id=net["chain_id"], verifying_contract=net["escrow"])
    return order, d


NETS = pytest.mark.parametrize("net", [MAINNET, TORONTO], ids=["mainnet-4162", "toronto-79479957"])


# ================================================================ positive: recovers the maker
@NETS
@pytest.mark.parametrize("force_s", ["low", "high"])
def test_kms_der_signature_recovers_maker_for_obv3_order(net, force_s):
    fake = FakeKms(force_s=force_s)
    order, digest = _digest(fake.address, net)
    raw_der_s = decode_dss_signature(fake.sign_digest(digest))[1]
    assert (raw_der_s > N // 2) == (force_s == "high")  # the emulator really produced a high-s output
    sig = kms.KmsOrderSigner(fake, fake.address).sign_digest_hex(digest)
    r, s, v = split_sig(sig)
    assert v in (27, 28) and 1 <= s <= N // 2  # packed r||s||v, canonical low-s
    assert ecrecover(digest, sig) == fake.address == order["maker"]           # independent pure-Python
    assert Account._recover_hash(digest, signature=sig) == fake.address       # eth_account agrees


@NETS
def test_kms_signature_equals_what_the_in_process_key_path_would_verify(net):
    """Same order, KMS path vs eth_account in-process path: both recover the maker on the same digest."""
    fake = FakeKms()
    _, digest = _digest(fake.address, net)
    kms_sig = kms.KmsOrderSigner(fake, fake.address).sign_digest_hex(digest)
    payload = eip712.order_payload(intent_for(order_with_maker(fake.address, net), net), kms_sig)
    assert payload["orderSignature"] == kms_sig and payload["maker"] == fake.address
    assert ecrecover(digest, payload["orderSignature"]) == payload["maker"]


def test_high_s_is_normalized_and_v_flips():
    fake = FakeKms(force_s="high")
    _, digest = _digest(fake.address, MAINNET)
    der = fake.sign_digest(digest)
    r, s_high = decode_dss_signature(der)
    out = kms.der_to_ethereum_signature(digest, der, fake.address)
    assert int.from_bytes(out[32:64], "big") == N - s_high
    # the low-s twin of the same signature converts to exactly the same 65 bytes
    assert kms.der_to_ethereum_signature(digest, encode_dss_signature(r, N - s_high), fake.address) == out
    assert kms.normalize_low_s(N // 2) == N // 2 and kms.normalize_low_s(N // 2 + 1) == N - (N // 2 + 1)


def test_v_selection_is_the_only_recovering_parity():
    for _ in range(8):  # both parities occur over a few random signatures
        fake = FakeKms()
        _, digest = _digest(fake.address, MAINNET)
        out = kms.der_to_ethereum_signature(digest, fake.sign_digest(digest), fake.address)
        v = out[64]
        other = out[:64] + bytes([55 - v])  # 27 <-> 28
        assert ecrecover(digest, "0x" + out.hex()) == fake.address
        assert Account._recover_hash(digest, signature=other) != fake.address


def test_address_from_spki_matches_independent_derivation():
    fake = FakeKms()
    pt = fake._key.public_key().public_numbers()
    from test_sxbet_eip712_official_vectors import address_from_pubkey
    assert fake.address == address_from_pubkey((pt.x, pt.y))


# ================================================================ negatives
def test_wrong_key_is_refused():
    signer_kms, other = FakeKms(), FakeKms()
    _, digest = _digest(signer_kms.address, MAINNET)
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.KmsOrderSigner(other, signer_kms.address).sign_digest(digest)
    assert e.value.code == "KMS_SIGNATURE_DOES_NOT_RECOVER_SIGNER"


def test_raw_mode_rehash_mistake_fails_recovery():
    """MessageType=RAW / GCP `data`: the KMS signs sha256(digest). Valid ECDSA, wrong message: refused."""
    fake = FakeKms(raw_mode=True)
    _, digest = _digest(fake.address, MAINNET)
    der = fake.sign_digest(digest)
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.der_to_ethereum_signature(digest, der, fake.address)
    assert e.value.code == "KMS_SIGNATURE_DOES_NOT_RECOVER_SIGNER"
    # it IS a signature over sha256(digest), proving the cause is the double hash
    h = hashes.Hash(hashes.SHA256())
    h.update(digest)
    assert kms.der_to_ethereum_signature(h.finalize(), der, fake.address)


def test_wrong_digest_domain_fails_recovery():
    fake = FakeKms()
    _, d_main = _digest(fake.address, MAINNET)
    _, d_tor = _digest(fake.address, TORONTO)
    der = fake.sign_digest(d_main)
    with pytest.raises(kms.KmsSignatureError):
        kms.der_to_ethereum_signature(d_tor, der, fake.address)


def _good():
    fake = FakeKms(force_s="low")
    _, digest = _digest(fake.address, MAINNET)
    return fake, digest, fake.sign_digest(digest)


def _seq(*ints: bytes) -> bytes:
    body = b"".join(ints)
    return bytes([0x30, len(body)]) + body


def _int(b: bytes) -> bytes:
    return bytes([0x02, len(b)]) + b


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d + b"\x00", "KMS_DER_MALFORMED"),                       # outer length no longer matches
    (lambda d: d[:-1], "KMS_DER_MALFORMED"),                           # truncated
    (lambda d: b"\x31" + d[1:], "KMS_DER_MALFORMED"),                   # SET instead of SEQUENCE
    (lambda d: b"", "KMS_DER_LENGTH"),
    (lambda d: b"\x30" + b"\x00" * 80, "KMS_DER_LENGTH"),               # oversized
    (lambda d: _seq(_int(b"\x01"), _int(b"\x01")) + b"\x00", "KMS_DER_MALFORMED"),
])
def test_malformed_der_refused(mutate, code):
    fake, digest, der = _good()
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.der_to_ethereum_signature(digest, mutate(der), fake.address)
    assert e.value.code == code


@pytest.mark.parametrize("der,code", [
    (_seq(_int(b"\x00\x01"), _int(b"\x01")), "KMS_DER_NON_MINIMAL"),
    (_seq(_int(b"\x81"), _int(b"\x01")), "KMS_DER_NEGATIVE_INTEGER"),
    (_seq(_int(b"\x01" * 34), _int(b"\x01")), "KMS_DER_INTEGER_SIZE"),  # 34-byte INTEGER
    (_seq(_int(b"\x00"), _int(b"\x01")), "KMS_SIGNATURE_OUT_OF_RANGE"),  # r = 0
    (_seq(_int(b"\x01"), _int(b"\x00" + N.to_bytes(32, "big"))), "KMS_SIGNATURE_OUT_OF_RANGE"),  # s = n
    (_seq(_int(b"\x01"), b"\x03\x01\x01"), "KMS_DER_MALFORMED"),         # BIT STRING, not INTEGER
    (b"\x30\x81\x06" + _int(b"\x01") + _int(b"\x01"), "KMS_DER_MALFORMED"),  # long-form length
    (_seq(_int(b"\x01\x01"), _int(b"")), "KMS_DER_INTEGER_SIZE"),      # zero-length INTEGER
])
def test_strict_der_rules(der, code):
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.parse_der_signature(der)
    assert e.value.code == code


def test_oversized_integer_refused_even_with_valid_outer_length():
    der = _seq(_int(b"\x01" * 34), _int(b"\x01" * 32))  # exactly MAX_DER_LENGTH bytes, r has 34 bytes
    assert len(der) == kms.MAX_DER_LENGTH
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.parse_der_signature(der)
    assert e.value.code == "KMS_DER_INTEGER_SIZE"


@pytest.mark.parametrize("digest", [b"\x00" * 31, b"\x00" * 33, "00" * 32, bytearray(32), None])
def test_digest_must_be_exactly_32_bytes(digest):
    fake = FakeKms()
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.KmsOrderSigner(fake, fake.address).sign_digest(digest)
    assert e.value.code == "KMS_DIGEST_INVALID"
    assert fake.calls == 0  # never forwarded to the backend


def test_bad_expected_address_and_backend_refused():
    fake, digest, der = _good()
    for bad in ("", "0x1234", fake.address[2:], None):
        with pytest.raises(kms.KmsSignatureError):
            kms.der_to_ethereum_signature(digest, der, bad)
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.KmsOrderSigner(object(), fake.address)
    assert e.value.code == "KMS_BACKEND_INVALID"


def test_backend_exception_is_replaced_by_fixed_code():
    class Boom:
        def sign_digest(self, digest):
            raise RuntimeError("arn:aws:kms:eu-west-1:111122223333:key/secret-id AccessDenied")
    fake = FakeKms()
    _, digest = _digest(fake.address, MAINNET)
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.KmsOrderSigner(Boom(), fake.address).sign_digest(digest)
    assert e.value.code == "KMS_BACKEND_FAILED" and "arn:" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_spki_of_other_curve_refused():
    p256 = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(kms.KmsSignatureError) as e:
        kms.address_from_spki_der(p256)
    assert e.value.code == "KMS_PUBLIC_KEY_NOT_SECP256K1"
    for bad in (b"", b"\x30\x00", "not-bytes"):
        with pytest.raises(kms.KmsSignatureError):
            kms.address_from_spki_der(bad)


def test_default_signer_path_is_unchanged_and_not_wired_to_kms():
    """signer.core keeps the in-process key as its only signing path; kms.py is an unwired adapter."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "signer" / "core.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            mods.update(a.name for a in node.names)
            mods.add(node.module or "")
    assert "kms" not in mods
    kms_src = (pathlib.Path(__file__).resolve().parents[1] / "signer" / "kms.py").read_text(encoding="utf-8")
    for lib in ("boto3", "botocore", "google.cloud", "azure"):
        assert f"import {lib}" not in kms_src and f"from {lib}" not in kms_src
