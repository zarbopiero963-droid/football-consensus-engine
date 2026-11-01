"""SX Bet OBv3 order signing: official example, independent EIP-712 implementation, ecrecover, EOA key format.

Owner audit FCB-SXBET-MAGIC-WALLET-FULL-AUDIT-01. Offline only: no network, no funds, no real key.
Signing keys are generated in-test (Account.create) and discarded; the two fixed keys below are
PUBLISHED test keys (EIP-155 spec example, Hardhat default account #0) used only for address derivation.

Evidence labels used in this file:
- OFFICIAL_EXAMPLE: values copied verbatim from https://docs.sx.bet/openapi.json (POST /orders-v3 example,
  read 01-11-2026). The docs publish NO reproducible digest/signature vector: the posting-orders sample uses
  a random salt and expiry, and the OpenAPI example signature does not recover its own `maker` under the
  mainnet or Toronto domain (pinned below as OFFICIAL_EXAMPLE_NOT_A_VECTOR).
- INDEPENDENT_IMPLEMENTATION: the EIP-712 digest is rebuilt by hand from the EIP-712 spec
  (https://eips.ethereum.org/EIPS/eip-712: typeHash, encodeData, domainSeparator, 0x1901 prefix) using only
  the keccak256 primitive, not eth_account's encode_typed_data; signature recovery and public-key / address
  derivation use a pure-Python secp256k1 implementation, not eth_keys. Equality with signer/eip712.py is
  a cross-implementation check, NOT an official vector.
"""
import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import keccak  # noqa: E402  (hash primitive only)

from signer import eip712  # noqa: E402
from sxbet_signer_kit import approve, context, make_intent, make_signer  # noqa: E402

# ---------------------------------------------------------------- domains (docs/sxbet/magic_and_proxy_wallet.md §4)
MAINNET = {"chain_id": 4162, "escrow": "0x890482680C3a0116aBB003B17e7D694D13a6c1eB",
           "usdc": "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"}
TORONTO = {"chain_id": 79479957, "escrow": "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38",
           "usdc": "0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956"}

# ---------------------------------------------------------------- OFFICIAL_EXAMPLE (docs.sx.bet/openapi.json)
OFFICIAL_ORDER = {
    "marketHash": "0x81cfc23d0a02403f32d29b5a7c5686acd5eaedcdaa461cf253a1488e4cac0fcf",
    "maker": "0xbcc6D643e4159A75ED1dB4e13330230B82F2AEe5",
    "totalBetSize": "2000000",
    "percentageOdds": "40000000000000000000",
    "salt": "0x8fa0cd4dbb399048caf74b8455f0fcf502fc989fc3e2720003aa10756faa70ef",
    "expiry": 1785527222,
    "baseToken": "0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956",
    "isMakerBettingOutcomeOne": True,
    "timeInForce": "GTC",
    "orderSignature": "0xa343a8052087c6739375c157f716beb6ddad7f1549e4c746aafda2869eb1f3235e7ba71aa79fa5b3d9"
                      "9b51b51c9cfc1b41cbda4b02badc0ef31ec3e978e069d81b",
    "clientOrderId": "quote-2026-07-31-002",
}
OFFICIAL_EXAMPLE_ORDER_ID = "0x7f611d85216fa11810d5357ffaaeafbbefb88e69b9fd26bfae7b13a67cceb9dc"  # 200 example

# ---------------------------------------------------------------- pure-Python secp256k1 (INDEPENDENT_IMPLEMENTATION)
P = 2 ** 256 - 2 ** 32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(k, pt):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def eip55(addr20: bytes) -> str:
    low = addr20.hex()
    h = keccak(low.encode("ascii")).hex()
    return "0x" + "".join(c.upper() if c.isalpha() and int(h[i], 16) >= 8 else c for i, c in enumerate(low))


def address_from_pubkey(pt) -> str:
    return eip55(keccak(pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big"))[-20:])


def address_from_private_key(key_hex: str) -> str:
    d = int(key_hex[2:] if key_hex.startswith("0x") else key_hex, 16)
    assert 1 <= d < N
    return address_from_pubkey(_mul(d, G))


def split_sig(sig_hex: str):
    raw = bytes.fromhex(sig_hex[2:] if sig_hex.startswith("0x") else sig_hex)
    assert len(raw) == 65, "orderSignature must be 65 bytes, packed r-s-v"
    return int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big"), raw[64]


def ecrecover(digest: bytes, sig_hex: str) -> str:
    """Recover the signer address of a 32-byte digest (no prefix: EIP-712 digests are signed raw)."""
    r, s, v = split_sig(sig_hex)
    assert v in (27, 28) and 1 <= r < N and 1 <= s < N
    y2 = (pow(r, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    assert y * y % P == y2, "r is not an x-coordinate on secp256k1"
    if y & 1 != v - 27:
        y = P - y
    z = int.from_bytes(digest, "big")
    rinv = pow(r, -1, N)
    q = _add(_mul(s * rinv % N, (r, y)), _mul((-z * rinv) % N, G))
    return address_from_pubkey(q)


# ---------------------------------------------------------------- manual EIP-712 (INDEPENDENT_IMPLEMENTATION)
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
ORDER_FIELDS = [("marketHash", "bytes32"), ("baseToken", "address"), ("totalBetSize", "uint256"),
                ("percentageOdds", "uint256"), ("salt", "uint256"), ("expiry", "uint256"), ("maker", "address"),
                ("isMakerBettingOutcomeOne", "bool")]


def _type_string(fields) -> str:
    return "Order(" + ",".join(f"{t} {n}" for n, t in fields) + ")"


def _enc(typ: str, value) -> bytes:
    if typ == "bytes32":
        b = bytes.fromhex(value[2:])
        assert len(b) == 32
        return b
    if typ == "address":
        return bytes(12) + bytes.fromhex(value[2:].lower())
    if typ == "uint256":
        v = int(value, 0) if isinstance(value, str) else int(value)
        assert 0 <= v < 2 ** 256
        return v.to_bytes(32, "big")
    if typ == "bool":
        assert isinstance(value, bool)
        return int(value).to_bytes(32, "big")
    if typ == "string":
        return keccak(value.encode("utf-8"))
    raise AssertionError(typ)


def manual_domain_separator(name: str, version: str, chain_id: int, verifying_contract: str) -> bytes:
    return keccak(keccak(DOMAIN_TYPE.encode()) + _enc("string", name) + _enc("string", version)
                  + _enc("uint256", chain_id) + _enc("address", verifying_contract))


def manual_struct_hash(order: dict, fields=ORDER_FIELDS) -> bytes:
    return keccak(keccak(_type_string(fields).encode()) + b"".join(_enc(t, order[n]) for n, t in fields))


def manual_digest(order: dict, *, chain_id: int, verifying_contract: str, name="OBv3 Escrow", version="1",
                  fields=ORDER_FIELDS) -> bytes:
    return keccak(b"\x19\x01" + manual_domain_separator(name, version, chain_id, verifying_contract)
                  + manual_struct_hash(order, fields))


def intent_for(order: dict, net: dict, *, time_in_force="GTC") -> dict:
    """signer/eip712.py intent fields for an order in the documented wire shape."""
    salt = order["salt"]
    return {"domain_name": "OBv3 Escrow", "domain_version": "1", "chain_id": net["chain_id"],
            "verifying_contract": net["escrow"].lower(), "market_hash": order["marketHash"],
            "base_token": order["baseToken"].lower(), "total_bet_size": order["totalBetSize"],
            "percentage_odds": order["percentageOdds"],
            "salt": str(int(salt, 16)) if salt.startswith("0x") else salt, "expiry": order["expiry"],
            "maker": order["maker"].lower(), "is_maker_betting_outcome_one": order["isMakerBettingOutcomeOne"],
            "time_in_force": time_in_force, "idempotency_key": "vector-0001"}


def signer_digest(intent: dict) -> bytes:
    return bytes.fromhex(eip712.order_id(eip712.signable(intent))[2:])


def order_with_maker(maker: str, net: dict, **kw) -> dict:
    o = {k: OFFICIAL_ORDER[k] for k in ("marketHash", "totalBetSize", "percentageOdds", "salt", "expiry",
                                       "isMakerBettingOutcomeOne")}
    o.update(baseToken=net["usdc"], maker=maker)
    o.update(kw)
    return o


@pytest.fixture
def throwaway():
    acct = Account.create()  # fresh, never funded, discarded after the test
    return acct, acct.key.hex()


# ================================================================ sanity of the independent primitives
def test_independent_primitives_match_published_constants():
    # keccak256("") (Ethereum Yellow Paper) and the EIP-55 spec's own examples.
    assert keccak(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    for a in ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
              "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB", "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb"):
        assert eip55(bytes.fromhex(a[2:])) == a
    # EIP-712 spec "Mail" example: published domain separator and digest.
    mail_domain = keccak(keccak(DOMAIN_TYPE.encode()) + _enc("string", "Ether Mail") + _enc("string", "1")
                         + _enc("uint256", 1) + _enc("address", "0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC"))
    assert mail_domain.hex() == "f2cee375fa42b42143804025fc449deafd50cc031ca257e0b194a650a912090f"
    person = "Person(string name,address wallet)"
    mail = "Mail(Person from,Person to,string contents)" + person

    def p(name, wallet):
        return keccak(keccak(person.encode()) + _enc("string", name) + _enc("address", wallet))
    sh = keccak(keccak(mail.encode()) + p("Cow", "0xCD2a3d9F938E13CD947Ec05AbC7FE734Df8DD826")
                + p("Bob", "0xbBbBBBBbbBBBbbbBbbBbbbbBBbBbbbbBbBbbBBbB") + _enc("string", "Hello, Bob!"))
    assert keccak(b"\x19\x01" + mail_domain + sh).hex() == \
        "be609aee343fb3c4b28e1df9e632fca64fcfaede20f02e86244efddf30957bd2"


def test_published_domain_addresses_are_valid_eip55():
    for net in (MAINNET, TORONTO):
        for a in (net["escrow"], net["usdc"]):
            assert eip55(bytes.fromhex(a[2:])) == a


# ================================================================ (a) digest: official example + independent impl
@pytest.mark.parametrize("net", [MAINNET, TORONTO], ids=["mainnet-4162", "toronto-79479957"])
def test_INDEPENDENT_IMPLEMENTATION_digest_equals_signer_on_official_example_fields(net):
    """OFFICIAL_EXAMPLE fields, two independent EIP-712 encoders, identical digest (no official digest exists)."""
    order = dict(OFFICIAL_ORDER)
    intent = intent_for(order, net)
    manual = manual_digest(order, chain_id=net["chain_id"], verifying_contract=net["escrow"])
    assert signer_digest(intent) == manual
    assert eip712.order_id(eip712.signable(intent)) == "0x" + manual.hex()  # orderId = digest, lowercase hex
    # Hex salt (OpenAPI example / JS sample) and decimal salt (Python sample) are the same uint256.
    assert manual_digest({**order, "salt": str(int(order["salt"], 16))}, chain_id=net["chain_id"],
                         verifying_contract=net["escrow"]) == manual


def test_OFFICIAL_EXAMPLE_NOT_A_VECTOR_signature_does_not_recover_published_maker():
    """Pinned finding: the OpenAPI POST /orders-v3 example is illustrative. Under both published domains its
    orderSignature recovers an address other than its `maker`, and the 200-example orderId matches neither
    digest. It therefore cannot serve as an official test vector (docs defect, not a signer defect)."""
    for net in (MAINNET, TORONTO):
        d = manual_digest(OFFICIAL_ORDER, chain_id=net["chain_id"], verifying_contract=net["escrow"])
        assert "0x" + d.hex() != OFFICIAL_EXAMPLE_ORDER_ID
        rec = ecrecover(d, OFFICIAL_ORDER["orderSignature"])
        assert rec != OFFICIAL_ORDER["maker"]
        assert Account._recover_hash(d, signature=OFFICIAL_ORDER["orderSignature"]) == rec  # both recoverers agree
    r, s, v = split_sig(OFFICIAL_ORDER["orderSignature"])  # still well-formed: 65 bytes, v=27, low-s
    assert v == 27 and s <= N // 2


# ================================================================ (b) sign with throwaway key, recover == maker
@pytest.mark.parametrize("net", [MAINNET, TORONTO], ids=["mainnet-4162", "toronto-79479957"])
def test_throwaway_signature_recovers_maker_with_independent_ecrecover(net, throwaway):
    acct, _ = throwaway
    order = order_with_maker(acct.address, net)
    intent = intent_for(order, net)
    sig = acct.sign_message(eip712.signable(intent)).signature.to_0x_hex()
    digest = manual_digest(order, chain_id=net["chain_id"], verifying_contract=net["escrow"])
    assert ecrecover(digest, sig) == acct.address == order["maker"]
    r, s, v = split_sig(sig)
    assert v in (27, 28) and s <= N // 2  # canonical low-s, as OpenZeppelin ECDSA requires


def test_real_signer_output_verifies_under_independent_implementation(tmp_path, throwaway):
    """End to end through signer.core.Signer (risk + approval + ledger), checked only with manual code."""
    acct, _ = throwaway
    owner = Ed25519PrivateKey.generate()
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(acct.address)
    out = s.sign_order(i, approve(owner, i), context(i))
    o = out["order"]
    assert "timeInForce" in o and o["orderSignature"]
    digest = manual_digest(o, chain_id=i["chain_id"], verifying_contract=eip712.to_checksum_address(
        i["verifying_contract"]))
    assert out["order_id"] == "0x" + digest.hex()
    assert ecrecover(digest, o["orderSignature"]) == o["maker"] == acct.address == out["signer_address"]


# ================================================================ (c) negative tests
def _signed(acct, net):
    order = order_with_maker(acct.address, net)
    intent = intent_for(order, net)
    return order, intent, acct.sign_message(eip712.signable(intent)).signature.to_0x_hex()


def test_wrong_chain_id_breaks_digest_and_recovery(throwaway):
    acct, _ = throwaway
    order, intent, sig = _signed(acct, MAINNET)
    for cid in (1, 4161, 4163, TORONTO["chain_id"]):
        d = manual_digest(order, chain_id=cid, verifying_contract=MAINNET["escrow"])
        assert d != signer_digest(intent)
        assert d == signer_digest({**intent, "chain_id": cid})
        assert ecrecover(d, sig) != acct.address


def test_wrong_verifying_contract_breaks_digest_and_recovery(throwaway):
    acct, _ = throwaway
    order, intent, sig = _signed(acct, MAINNET)
    for vc in (TORONTO["escrow"], MAINNET["usdc"], "0x" + "00" * 20):
        d = manual_digest(order, chain_id=MAINNET["chain_id"], verifying_contract=vc)
        assert d != signer_digest(intent)
        assert d == signer_digest({**intent, "verifying_contract": vc.lower()})
        assert ecrecover(d, sig) != acct.address


def test_wrong_domain_name_or_version_breaks_recovery(throwaway):
    acct, _ = throwaway
    order, intent, sig = _signed(acct, MAINNET)
    for name, version in (("OBv3Escrow", "1"), ("OBv3 Escrow", "2"), ("SportX", "1")):
        d = manual_digest(order, chain_id=MAINNET["chain_id"], verifying_contract=MAINNET["escrow"], name=name,
                          version=version)
        assert ecrecover(d, sig) != acct.address


def test_field_order_is_the_documented_one_and_a_swap_breaks_recovery(throwaway):
    # Documented order (eip712-order-signing / posting-orders): "Order matters: salt precedes expiry".
    assert [(f["name"], f["type"]) for f in eip712.ORDER_TYPES["Order"]] == ORDER_FIELDS
    assert _type_string(ORDER_FIELDS) == ("Order(bytes32 marketHash,address baseToken,uint256 totalBetSize,"
                                          "uint256 percentageOdds,uint256 salt,uint256 expiry,address maker,"
                                          "bool isMakerBettingOutcomeOne)")
    acct, _ = throwaway
    order, intent, sig = _signed(acct, MAINNET)
    swapped = list(ORDER_FIELDS)
    swapped[4], swapped[5] = swapped[5], swapped[4]  # expiry before salt (legacy SportX v1 LibOrder order)
    for fields in (swapped, ORDER_FIELDS[::-1]):
        d = manual_digest(order, chain_id=MAINNET["chain_id"], verifying_contract=MAINNET["escrow"], fields=fields)
        assert d != signer_digest(intent)
        assert ecrecover(d, sig) != acct.address


def test_time_in_force_is_not_signed(throwaway):
    assert "timeInForce" not in {f["name"] for f in eip712.ORDER_TYPES["Order"]}
    acct, _ = throwaway
    order, intent, sig = _signed(acct, MAINNET)
    base = signer_digest(intent)
    for tif in ("GTC", "IOC", "FOK"):
        assert signer_digest({**intent, "time_in_force": tif}) == base
        payload = eip712.order_payload({**intent, "time_in_force": tif}, sig)
        assert payload["timeInForce"] == tif  # sent on the wire, outside the signature
    # If timeInForce WERE signed, the digest would differ and the same signature would not verify.
    with_tif = ORDER_FIELDS + [("timeInForce", "string")]
    d = manual_digest({**order, "timeInForce": "GTC"}, chain_id=MAINNET["chain_id"],
                      verifying_contract=MAINNET["escrow"], fields=with_tif)
    assert d != base and ecrecover(d, sig) != acct.address


def test_maker_not_signer_recovery_mismatch(throwaway):
    acct, _ = throwaway
    other = Account.create()
    order = order_with_maker(other.address, MAINNET)  # maker = other, signed by acct
    intent = intent_for(order, MAINNET)
    sig = acct.sign_message(eip712.signable(intent)).signature.to_0x_hex()
    rec = ecrecover(manual_digest(order, chain_id=MAINNET["chain_id"], verifying_contract=MAINNET["escrow"]), sig)
    assert rec == acct.address != order["maker"]


def test_real_signer_refuses_intent_whose_maker_is_not_its_key(tmp_path, throwaway):
    from signer.core import SignerRefused
    owner = Ed25519PrivateKey.generate()
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(Account.create().address)
    with pytest.raises(SignerRefused) as e:
        s.sign_order(i, approve(owner, i), context(i))
    assert e.value.code == "INTENT_MAKER_IS_NOT_SIGNER"


def test_mainnet_and_toronto_domains_are_not_interchangeable(throwaway):
    m = manual_domain_separator("OBv3 Escrow", "1", MAINNET["chain_id"], MAINNET["escrow"])
    t = manual_domain_separator("OBv3 Escrow", "1", TORONTO["chain_id"], TORONTO["escrow"])
    assert m != t
    acct, _ = throwaway
    order = order_with_maker(acct.address, MAINNET)
    for signed_on, checked_on in ((MAINNET, TORONTO), (TORONTO, MAINNET)):
        sig = acct.sign_message(eip712.signable(intent_for(order, signed_on))).signature.to_0x_hex()
        good = manual_digest(order, chain_id=signed_on["chain_id"], verifying_contract=signed_on["escrow"])
        bad = manual_digest(order, chain_id=checked_on["chain_id"], verifying_contract=checked_on["escrow"])
        assert ecrecover(good, sig) == acct.address
        assert ecrecover(bad, sig) != acct.address
        # Half-swapped domains (right chain, wrong escrow and vice versa) also fail.
        for cid, vc in ((signed_on["chain_id"], checked_on["escrow"]), (checked_on["chain_id"], signed_on["escrow"])):
            assert ecrecover(manual_digest(order, chain_id=cid, verifying_contract=vc), sig) != acct.address


def test_any_signed_field_change_breaks_recovery(throwaway):
    acct, _ = throwaway
    order, intent, sig = _signed(acct, MAINNET)
    changes = {"marketHash": "0x" + "00" * 31 + "01", "baseToken": TORONTO["usdc"], "totalBetSize": "2000001",
               "percentageOdds": "40125000000000000000", "salt": "1", "expiry": order["expiry"] + 1,
               "maker": Account.create().address, "isMakerBettingOutcomeOne": False}
    for k, v in changes.items():
        d = manual_digest({**order, k: v}, chain_id=MAINNET["chain_id"], verifying_contract=MAINNET["escrow"])
        assert ecrecover(d, sig) != acct.address, k


# ================================================================ (d) EOA key format (MAGIC-02, fake keys only)
# Published test keys (public knowledge, never funded by us, used ONLY to derive addresses):
PUBLISHED_KEYS = [
    # EIP-155 spec example: private key 0x4646...46 -> 0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F
    ("0x" + "46" * 32, "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F"),
    # Hardhat / Anvil default account #0 (publicly documented development key)
    ("0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
     "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"),
]


@pytest.mark.parametrize("key,address", PUBLISHED_KEYS, ids=["eip155-example", "hardhat-0"])
def test_published_32_byte_hex_key_derives_published_address(key, address):
    assert address_from_private_key(key) == address               # pure-Python secp256k1 + keccak + EIP-55
    assert Account.from_key(key).address == address               # eth_account import, 0x-prefixed
    assert Account.from_key(key[2:]).address == address           # same key without 0x prefix


def test_exported_style_hex_key_round_trips_and_signs_as_its_address(tmp_path, throwaway):
    """A standard secp256k1 EOA key exported as 32-byte hex (the format a wallet export such as Magic's
    revealEVMPrivateKey shows for "taking the key to another wallet") imports into eth_account and into the
    isolated signer, derives the same EIP-55 address independently, and produces OBv3 signatures that
    recover to that address. Throwaway key only: no real or Magic key is ever used."""
    acct, _ = throwaway
    key_hex = "0x" + bytes(acct.key).hex()
    assert len(bytes.fromhex(key_hex[2:])) == 32
    derived = address_from_private_key(key_hex)
    assert derived == acct.address == Account.from_key(key_hex).address == Account.from_key(key_hex[2:]).address
    for n, fmt in enumerate((key_hex, key_hex[2:], "0x" + key_hex[2:].upper())):  # 0x / bare / upper-case hex
        owner = Ed25519PrivateKey.generate()
        d = tmp_path / f"fmt{n}"
        d.mkdir()
        s = make_signer(d, (acct, fmt), owner)
        assert s.address == derived
        i = make_intent(derived)
        o = s.sign_order(i, approve(owner, i), context(i))["order"]
        dig = manual_digest(o, chain_id=i["chain_id"],
                            verifying_contract=eip712.to_checksum_address(i["verifying_contract"]))
        assert ecrecover(dig, o["orderSignature"]) == derived


@pytest.mark.parametrize("bad", ["0x" + "11" * 31, "0x" + "11" * 33, "0x" + "zz" * 32, "0x" + "00" * 32,
                                 "0x" + format(N, "064x"), ""])
def test_malformed_keys_rejected_by_signer_without_echoing_key(tmp_path, throwaway, bad):
    from signer.core import SignerConfigError
    acct, _ = throwaway
    with pytest.raises(SignerConfigError) as e:
        make_signer(tmp_path, (acct, bad), Ed25519PrivateKey.generate())
    assert e.value.args[0] in ("SIGNER_KEY_MISSING_OR_MALFORMED", "SIGNER_KEY_INVALID")
    assert bad[2:] == "" or bad[2:] not in str(e.value)
