"""WebAuthn (passkey, ES256) owner approvals: signer/webauthn.py + signer/approval.py + signer/core.py.

Offline. Synthetic assertions from a THROWAWAY P-256 key (tests/sxbet_webauthn_kit.py); the order-signing
key is a throwaway eth account. Nothing is sent anywhere.
"""
import copy

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import approval as appr  # noqa: E402
from signer import webauthn as wa  # noqa: E402
from signer.approval import ApprovalError, WebAuthnApproval  # noqa: E402
from signer.core import SignerRefused  # noqa: E402
from signer.intent import intent_hash  # noqa: E402
from sxbet_signer_kit import (ACCOUNT_ID, EVENT_ID, FEES, LABELS, NOW, SIGNER_ID, approve, context,  # noqa: E402
                              make_intent, make_signer)
from sxbet_webauthn_kit import FakePasskey, ORIGIN, RP_ID, cbor  # noqa: E402

POLICY = wa.WebAuthnPolicy(RP_ID, frozenset({ORIGIN}))


@pytest.fixture
def throwaway():
    acct = Account.create()
    return acct, acct.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()


def enrolled(passkey=None):
    passkey = passkey or FakePasskey()
    store = wa.InMemoryCredentialStore()
    chal = b"\x07" * 32
    store.add(wa.verify_registration(expected_challenge=chal, policy=POLICY, **passkey.register(chal)))
    return passkey, store, wa.WebAuthnApprovalVerifier(POLICY, store)


def binding_for(intent, **over):
    label = LABELS["outcomeOneName" if intent["is_maker_betting_outcome_one"] else "outcomeTwoName"]
    b = appr.build_binding(intent, account_id=ACCOUNT_ID, event_id=EVENT_ID, selection_label=label,
                           expected_fees=FEES, max_exposure_base_units=int(intent["total_bet_size"]),
                           quoted_percentage_odds=40 * 10 ** 18, intent_hash=intent_hash(intent))
    b.update(over)
    return b


def display_for(binding, **labels):
    args = dict(operation_id="req-0001", event_label="Home FC v Away FC", market_label="1X2 (no draw)",
                data_source="SIMULATED")
    args.update(labels)
    return appr.build_display(binding, **args)


def webauthn_approval(passkey, binding, display=None, *, nonce="wa-nonce-000000000001", issued=NOW, ttl=60,
                      signed_binding=None, signed_display=None, decision=appr.DECISION_APPROVE, **assert_kw):
    display = display or display_for(binding)
    chal = appr.webauthn_challenge(appr.WEBAUTHN_ORDER_DOMAIN, decision=decision,
                                   binding=signed_binding or binding, display=signed_display or display,
                                   approval_nonce=nonce, issued_at=issued, expires_at=issued + ttl,
                                   signer_id=SIGNER_ID)
    parts = passkey.assert_(chal, **assert_kw)
    return WebAuthnApproval(binding, display, nonce, issued, issued + ttl, SIGNER_ID, **parts)


def signer_with(tmp_path, throwaway, owner, verifier):
    s = make_signer(tmp_path, throwaway, owner)
    s._webauthn = verifier  # same as Signer(..., webauthn=verifier); make_signer has no such parameter
    return s


# ---------------------------------------------------------------- CBOR / registration
def test_cbor_decoder_is_strict():
    assert wa.cbor_decode(cbor({1: 2, "a": [b"x", -7, True, None]})) == {1: 2, "a": [b"x", -7, True, None]}
    for bad in (b"\x9f\xff", b"\xc0\x00", b"\xf9\x00\x00", b"\xa2\x01\x01\x01\x02", b"\x5a\xff\xff\xff\xff",
                cbor(1) + b"\x00", b""):
        with pytest.raises(ApprovalError):
            wa.cbor_decode(bad)


def test_registration_valid_and_negative_cases():
    pk = FakePasskey()
    chal = b"\x01" * 32
    cred = wa.verify_registration(expected_challenge=chal, policy=POLICY, **pk.register(chal))
    assert cred.credential_id == pk.credential_id and len(cred.public_key) == 65
    cases = [
        (dict(expected_challenge=b"\x02" * 32), {}, "WEBAUTHN_CHALLENGE_MISMATCH"),
        ({}, dict(origin="http://localhost:9999"), "WEBAUTHN_ORIGIN_NOT_ALLOWED"),
        ({}, dict(rp_id="evil.example"), "WEBAUTHN_RP_ID_MISMATCH"),
        ({}, dict(flags=wa.FLAG_UP | wa.FLAG_AT), "WEBAUTHN_USER_NOT_VERIFIED"),
        ({}, dict(fmt="packed"), "WEBAUTHN_ATTESTATION_UNSUPPORTED"),
        ({}, dict(att_stmt={"alg": -7}), "WEBAUTHN_ATTESTATION_UNSUPPORTED"),
    ]
    for kw, reg_kw, code in cases:
        args = dict(expected_challenge=chal, policy=POLICY, **pk.register(chal, **reg_kw))
        args.update(kw)
        with pytest.raises(ApprovalError) as e:
            wa.verify_registration(**args)
        assert str(e.value) == code, code


def test_registration_refuses_non_es256_cose_keys():
    good = FakePasskey()._cose()
    for mutate in ({3: -8}, {1: 1}, {-1: 2}, {-2: b"\x00" * 31}, {4: 1}):
        with pytest.raises(ApprovalError):
            wa.cose_es256_public_key({**good, **mutate})
    with pytest.raises(ApprovalError) as e:  # not a point on P-256
        wa.cose_es256_public_key({**good, -3: b"\x01" * 32})
    assert str(e.value) == "WEBAUTHN_COSE_KEY_INVALID"


def test_policy_refuses_plain_http_outside_localhost_and_foreign_origins():
    with pytest.raises(ValueError):
        wa.WebAuthnPolicy("signer.example", frozenset({"http://signer.example"}))
    with pytest.raises(ValueError):
        wa.WebAuthnPolicy("signer.example", frozenset({"https://other.example"}))
    wa.WebAuthnPolicy("signer.tailnet.ts.net", frozenset({"https://signer.tailnet.ts.net"}))


# ---------------------------------------------------------------- display / binding
def test_display_is_derived_and_shows_real_sx_bet_semantics(throwaway):
    acct, _ = throwaway
    intent = make_intent(acct.address)
    d = display_for(binding_for(intent))
    assert d["selection"] == {"outcome": 1, "label": "Home FC", "semantics": "BACK_OUTCOME_ONE",
                              "is_maker_betting_outcome_one": True}
    assert d["decimal_odds_limit"] == "2.5000" and d["implied_probability_limit_pct"] == "40.0000"
    assert d["stake_usdc"] == "5.000000" and d["max_potential_loss_base_units"] == "5000000"
    assert d["potential_payout_before_fees_base_units"] == "12500000" and d["slippage_limit_bps"] == 0
    assert d["order_expiry_utc"].endswith("Z") and d["data_source"] == "SIMULATED"
    two = display_for(binding_for(make_intent(acct.address, betting_outcome_one=False)))
    assert two["selection"]["semantics"] == "BACK_OUTCOME_TWO" and two["selection"]["label"] == "Away FC"


def test_display_mismatch_names_the_tampered_field(throwaway):
    acct, _ = throwaway
    b = binding_for(make_intent(acct.address))
    d = display_for(b)
    assert appr.display_mismatch(d, b) is None
    for k, v in (("stake_usdc", "50.000000"), ("decimal_odds_limit", "3.0000"), ("market_hash", "0x" + "cd" * 32),
                 ("max_potential_loss_base_units", "1")):
        assert appr.display_mismatch({**d, k: v}, b) == k
    assert appr.display_mismatch({**d, "extra": 1}, b) == "DISPLAY"
    assert appr.display_mismatch({**d, "event_label": "\x00"}, b) == "DISPLAY"


def test_challenge_changes_with_every_order_field(throwaway):
    acct, _ = throwaway
    intent = make_intent(acct.address)
    b = binding_for(intent)
    base = dict(decision="APPROVE", binding=b, display=display_for(b), approval_nonce="n" * 16, issued_at=NOW,
                expires_at=NOW + 60, signer_id=SIGNER_ID)
    ref = appr.webauthn_challenge(appr.WEBAUTHN_ORDER_DOMAIN, **base)
    variants = [make_intent(acct.address, stake_base_units=6_000_000),
                make_intent(acct.address, requested_percentage_odds=45 * 10 ** 18),
                make_intent(acct.address, market_hash="0x" + "cd" * 32),
                make_intent(acct.address, betting_outcome_one=False)]
    for v in variants:
        vb = binding_for(v)
        assert appr.webauthn_challenge(appr.WEBAUTHN_ORDER_DOMAIN, **{**base, "binding": vb,
                                                                      "display": display_for(vb)}) != ref
    assert appr.webauthn_challenge(appr.WEBAUTHN_ORDER_DOMAIN, **{**base, "decision": "REJECT"}) != ref
    assert appr.webauthn_challenge(appr.WEBAUTHN_CANCEL_DOMAIN, **base) != ref


# ---------------------------------------------------------------- signer core with a passkey approval
def test_valid_passkey_approval_signs(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, store, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    out = s.sign_order(intent, webauthn_approval(pk, binding_for(intent)), context(intent))
    assert out["live_trading"] == "UNAVAILABLE" and out["submitted"] is False
    assert store.get(pk.credential_id).sign_count == 1


def test_modified_stake_after_approval_is_refused(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    approved = make_intent(acct.address)
    tampered = make_intent(acct.address, stake_base_units=9_000_000)
    tb = binding_for(tampered)
    ab = binding_for(approved)
    a = webauthn_approval(pk, tb, signed_binding=ab, signed_display=display_for(ab))
    with pytest.raises(SignerRefused) as e:
        s.sign_order(tampered, a, context(tampered))
    assert e.value.code == "WEBAUTHN_CHALLENGE_MISMATCH"


@pytest.mark.parametrize("kw,code", [
    (dict(origin="https://evil.example"), "WEBAUTHN_ORIGIN_NOT_ALLOWED"),
    (dict(rp_id="evil.example"), "WEBAUTHN_RP_ID_MISMATCH"),
    (dict(flags=wa.FLAG_UP), "WEBAUTHN_USER_NOT_VERIFIED"),
    (dict(flags=wa.FLAG_UV), "WEBAUTHN_USER_NOT_PRESENT"),
    (dict(typ="webauthn.create"), "WEBAUTHN_TYPE_INVALID"),
    (dict(crossOrigin=True), "WEBAUTHN_CROSS_ORIGIN"),
])
def test_assertion_negative_cases(tmp_path, throwaway, owner, kw, code):
    acct, _ = throwaway
    pk, store, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(pk, binding_for(intent), **kw), context(intent))
    assert e.value.code == code
    assert store.get(pk.credential_id).sign_count == 0 and s.exposure().total == 0


def test_sign_count_regression_is_refused(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, store, ver = enrolled(FakePasskey(sign_count=10))
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(pk, binding_for(intent), sign_count=10), context(intent))
    assert e.value.code == "WEBAUTHN_SIGN_COUNT_REGRESSION"
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(pk, binding_for(intent), sign_count=3), context(intent))
    assert e.value.code == "WEBAUTHN_SIGN_COUNT_REGRESSION"


def test_replayed_assertion_never_yields_a_second_order(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, store, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    a = webauthn_approval(pk, binding_for(intent))
    first = s.sign_order(intent, a, context(intent))
    again = s.sign_order(intent, a, context(intent))  # same key + same intent: cached, no new signature
    assert again["cached"] is True and again["order_id"] == first["order_id"]
    # the same assertion re-verified at the passkey layer is a counter regression
    with pytest.raises(ApprovalError) as e:
        ver.verify_order_approval(a, expected_intent_hash=intent_hash(intent), signer_id=SIGNER_ID, now=NOW)
    assert str(e.value) == "WEBAUTHN_SIGN_COUNT_REGRESSION"


def test_replay_with_zero_counter_authenticator_hits_the_consumed_nonce(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, ver = enrolled(FakePasskey(counter_step=0))  # synced passkeys often always report 0
    s = signer_with(tmp_path, throwaway, owner, ver)
    i1 = make_intent(acct.address, idempotency_key="order-key-A001")
    s.sign_order(i1, webauthn_approval(pk, binding_for(i1), nonce="shared-nonce-00000001"), context(i1))
    i2 = make_intent(acct.address, idempotency_key="order-key-B002")
    with pytest.raises(SignerRefused) as e:
        s.sign_order(i2, webauthn_approval(pk, binding_for(i2), nonce="shared-nonce-00000001"), context(i2))
    assert e.value.code == "APPROVAL_NONCE_REPLAYED"


def test_expired_approval_is_refused(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(pk, binding_for(intent), issued=NOW - 61, ttl=60), context(intent))
    assert e.value.code == "APPROVAL_EXPIRED"
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(pk, binding_for(intent), ttl=121), context(intent))
    assert e.value.code == "APPROVAL_TTL_TOO_LONG"


def test_approval_for_request_a_cannot_sign_request_b(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    a_intent = make_intent(acct.address, idempotency_key="order-key-A001")
    b_intent = make_intent(acct.address, idempotency_key="order-key-B002")
    a_approval = webauthn_approval(pk, binding_for(a_intent))
    with pytest.raises(SignerRefused) as e:  # A's approval presented as-is for B
        s.sign_order(b_intent, a_approval, context(b_intent))
    assert e.value.code == "APPROVAL_INTENT_MISMATCH"
    bb = binding_for(b_intent)
    ab = binding_for(a_intent)
    forged = webauthn_approval(pk, bb, signed_binding=ab, signed_display=display_for(ab))  # A's signature on B
    with pytest.raises(SignerRefused) as e:
        s.sign_order(b_intent, forged, context(b_intent))
    assert e.value.code == "WEBAUTHN_CHALLENGE_MISMATCH"


def test_reject_decision_signature_cannot_approve(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(pk, binding_for(intent), decision="REJECT"), context(intent))
    assert e.value.code == "WEBAUTHN_CHALLENGE_MISMATCH"


def test_inconsistent_display_and_unknown_credential_are_refused(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    b = binding_for(intent)
    lying = {**display_for(b), "stake_usdc": "0.010000"}  # a page that shows a smaller stake than signed
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(pk, b, lying), context(intent))
    assert e.value.code == "APPROVAL_DISPLAY_MISMATCH_STAKE_USDC"
    stranger = FakePasskey()
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(stranger, b), context(intent))
    assert e.value.code == "WEBAUTHN_CREDENTIAL_UNKNOWN"


def test_signature_from_another_key_with_a_known_credential_id_is_refused(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    impostor = FakePasskey()
    impostor.credential_id = pk.credential_id
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(impostor, binding_for(intent)), context(intent))
    assert e.value.code == "WEBAUTHN_SIGNATURE_INVALID"


def test_webauthn_refused_when_not_configured_and_ed25519_path_still_works(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, _ = enrolled()
    s = make_signer(tmp_path, throwaway, owner)
    intent = make_intent(acct.address)
    with pytest.raises(SignerRefused) as e:
        s.sign_order(intent, webauthn_approval(pk, binding_for(intent)), context(intent))
    assert e.value.code == "WEBAUTHN_APPROVAL_NOT_CONFIGURED"
    assert s.sign_order(intent, approve(owner, intent), context(intent))["cached"] is False


def test_caller_mutation_after_submission_does_not_change_what_is_checked(tmp_path, throwaway, owner):
    acct, _ = throwaway
    pk, _, ver = enrolled()
    s = signer_with(tmp_path, throwaway, owner, ver)
    intent = make_intent(acct.address)
    b = binding_for(intent)
    a = webauthn_approval(pk, b)
    snapshot = copy.deepcopy(dict(a.display))
    out = s.sign_order(intent, a, context(intent))
    assert dict(a.display) == snapshot and out["order_id"].startswith("0x")
