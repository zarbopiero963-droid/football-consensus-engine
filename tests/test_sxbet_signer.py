"""Isolated signer: offline tests with freshly generated THROWAWAY keys. No funds, no network, no real key."""
import json
import threading

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

import signer  # noqa: E402
from bridge.sxbet import simulation as sim  # noqa: E402  (test-only cross-check; signer never imports bridge)
from signer import eip712  # noqa: E402
from signer.approval import Approval  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.core import KEY_ENV, Signer, SignerConfig, SignerConfigError, SignerRefused  # noqa: E402
from signer.intent import intent_hash  # noqa: E402
from signer.orders import OrderLedger  # noqa: E402
from sxbet_signer_kit import (ACCOUNT_ID, ESCROW, H, META_RAW, NOW, SIGNER_ID, TOKEN, Clock, approve, context,  # noqa: E402
                              make_intent, pub)
from sxbet_signer_kit import make_signer as _make_signer  # noqa: E402


@pytest.fixture
def throwaway():
    acct = Account.create()  # fresh, never funded, discarded after the test
    return acct, acct.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()  # throwaway owner approval key


def make_signer(tmp_path, throwaway, owner, *, max_stake=10_000_000, max_exposure=20_000_000, clock=None,
                ledger_path=None, audit_path=None):
    # Intentional change: limits now come from an owner-signed risk config (signer/risk.py).
    return _make_signer(tmp_path, throwaway, owner, clock=clock, ledger_path=ledger_path, audit_path=audit_path,
                        limits={"max_stake_per_order_base_units": max_stake,
                                "max_total_exposure_base_units": max_exposure})


def sign(s, i, a, ctx=None):
    return s.sign_order(i, a, context(i) if ctx is None else ctx)


def refused(fn):
    with pytest.raises(SignerRefused) as e:
        fn()
    return e.value.code


# ---------------------------------------------------------------- EIP-712
def test_bridge_and_signer_hash_the_same_intent(throwaway):
    i = make_intent(throwaway[0].address)
    assert intent_hash(i) == sim.intent_hash(i)


def test_eip712_digest_deterministic_and_recovers_throwaway_address(tmp_path, throwaway, owner):
    acct, _ = throwaway
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(acct.address)
    out = sign(s, i, approve(owner, i))
    # Independently rebuild the documented typed data (docs.sx.bet posting-orders python sample).
    domain = {"name": "OBv3 Escrow", "version": "1", "chainId": 4162,
              "verifyingContract": eip712.to_checksum_address(ESCROW)}
    msg = encode_typed_data(domain_data=domain, message_types=eip712.ORDER_TYPES, message_data=eip712.message(i))
    assert out["order_id"] == eip712.order_id(msg) == eip712.order_id(eip712.signable(i))
    assert out["order_id"] == out["order_id"].lower() and len(out["order_id"]) == 66
    sig = out["order"]["orderSignature"]
    assert Account.recover_message(msg, signature=sig) == acct.address == out["signer_address"]
    # timeInForce is sent but not signed: changing it does not change the digest.
    assert eip712.order_id(eip712.signable({**i, "time_in_force": "FOK"})) == out["order_id"]
    assert [f["name"] for f in eip712.ORDER_TYPES["Order"]] == [
        "marketHash", "baseToken", "totalBetSize", "percentageOdds", "salt", "expiry", "maker", "isMakerBettingOutcomeOne"]
    o = out["order"]
    assert isinstance(o["expiry"], int) and all(isinstance(o[k], str) for k in ("totalBetSize", "percentageOdds", "salt"))
    assert out["submitted"] is False and out["live_trading"] == "UNAVAILABLE" == signer.LIVE_TRADING


# ---------------------------------------------------------------- key custody
def test_key_never_exposed(tmp_path, throwaway, owner):
    acct, key = throwaway
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(acct.address)
    out = sign(s, i, approve(owner, i))
    bare = key[2:] if key.startswith("0x") else key
    for text in (repr(s), str(s), json.dumps(out), (tmp_path / "audit.jsonl").read_text(),
                 (tmp_path / "ledger.jsonl").read_text()):
        assert bare not in text.lower()
    assert "<hidden>" in repr(s)
    audit = (tmp_path / "audit.jsonl").read_text()
    assert out["order"]["orderSignature"][2:20] not in audit  # signature bytes never audited
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(s)
    assert not [a for a in vars(s) if "key" in a.lower() and a != "_owner_key"]


@pytest.mark.parametrize("env", [{}, {KEY_ENV: "not-a-key"}, {KEY_ENV: "0x" + "zz" * 32}])
def test_missing_or_malformed_key_fails_closed_without_echo(tmp_path, owner, env):
    cfg = SignerConfig(SIGNER_ID, "mainnet", 4162, ESCROW, TOKEN, 125)
    with pytest.raises(SignerConfigError) as e:
        Signer(cfg, owner_public_key=pub(owner), ledger=None, audit=AuditLog(str(tmp_path / "a")), env=env)
    assert "zz" not in str(e.value) and "not-a-key" not in str(e.value)


# ---------------------------------------------------------------- approval
def test_zero_exposure_default_refuses(tmp_path, throwaway, owner):
    acct, key = throwaway
    # No owner-signed risk config in the env => all-zero limits => refuse everything.
    cfg = SignerConfig(SIGNER_ID, "mainnet", 4162, ESCROW, TOKEN, 125, account_id=ACCOUNT_ID)
    ledger = OrderLedger.create(str(tmp_path / "l"), signer_id=SIGNER_ID, signer_address=acct.address)
    s = Signer(cfg, owner_public_key=pub(owner), ledger=ledger, audit=AuditLog(str(tmp_path / "a")),
               env={KEY_ENV: key}, clock=Clock())
    assert s.risk_config_source == "MISSING" and s.committed_exposure_base_units == 0
    i = make_intent(acct.address)
    assert refused(lambda: sign(s, i, approve(owner, i))) == "EXPOSURE_LIMIT_ZERO"


def test_invalid_approval_signature(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
    forged = Approval(a.binding, a.approval_nonce, a.issued_at, a.expires_at, a.signer_id, bytes(64))
    assert refused(lambda: sign(s, i, forged)) == "APPROVAL_SIGNATURE_INVALID"
    # Tampering any signed field (e.g. extending the expiry) breaks the signature.
    stretched = Approval(a.binding, a.approval_nonce, a.issued_at, a.expires_at + 30, a.signer_id, a.signature)
    assert refused(lambda: sign(s, i, stretched)) == "APPROVAL_SIGNATURE_INVALID"


def test_wrong_owner_key(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    assert refused(lambda: sign(s, i, approve(Ed25519PrivateKey.generate(), i))) == "APPROVAL_SIGNATURE_INVALID"


def test_chat_text_is_not_an_approval(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    assert refused(lambda: sign(s, i, "I, the owner, confirm this order")) == "APPROVAL_MISSING"
    assert refused(lambda: sign(s, i, None)) == "APPROVAL_MISSING"


def test_expired_and_overlong_and_future_approvals(tmp_path, throwaway, owner):
    clock = Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock)
    i = make_intent(throwaway[0].address)
    clock.t = NOW + 61
    assert refused(lambda: sign(s, i, approve(owner, i, ttl=60))) == "APPROVAL_EXPIRED"
    clock.t = NOW
    assert refused(lambda: sign(s, i, approve(owner, i, ttl=121))) == "APPROVAL_TTL_TOO_LONG"
    assert refused(lambda: sign(s, i, approve(owner, i, issued=NOW + 60))) == "APPROVAL_NOT_YET_VALID"
    assert refused(lambda: sign(s, i, approve(owner, i, signer_id="other-signer"))) == "APPROVAL_WRONG_SIGNER"


def test_replayed_nonce_refused_even_after_restart(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i1 = make_intent(throwaway[0].address, idempotency_key="order-key-A001")
    sign(s, i1, approve(owner, i1, nonce="nonce-replay-000001"))
    i2 = make_intent(throwaway[0].address, idempotency_key="order-key-A002")
    a2 = approve(owner, i2, nonce="nonce-replay-000001")
    assert refused(lambda: sign(s, i2, a2)) == "APPROVAL_NONCE_REPLAYED"
    restarted = make_signer(tmp_path, throwaway, owner)
    assert refused(lambda: sign(restarted, i2, a2)) == "APPROVAL_NONCE_REPLAYED"


@pytest.mark.parametrize("change", [
    {"requested_percentage_odds": 41 * 10 ** 18},  # price moved
    {"stake_base_units": 6_000_000},               # stake changed
    {"market_hash": "0x" + "cd" * 32},             # market changed
    {"betting_outcome_one": False},                # side changed
])
def test_any_parameter_change_breaks_the_approval(tmp_path, throwaway, owner, change):
    s = make_signer(tmp_path, throwaway, owner)
    approved = make_intent(throwaway[0].address)
    a = approve(owner, approved)
    changed = make_intent(throwaway[0].address, **change)
    assert refused(lambda: sign(s, changed, a)) == "APPROVAL_INTENT_MISMATCH"


def test_tampered_intent_dict_is_revalidated(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    bad = {**i, "percentage_odds": str(40 * 10 ** 18 + 1)}
    assert refused(lambda: sign(s, bad, approve(owner, bad))) == "INTENT_ODDS_NOT_ON_LADDER"
    lie = {**i, "percentage_odds": str(39 * 10 ** 18)}  # on ladder but not the round-down of requested
    assert refused(lambda: sign(s, lie, approve(owner, lie))) == "INTENT_ODDS_NOT_ROUND_DOWN_OF_REQUESTED"
    extra = {**i, "note": "x"}
    assert refused(lambda: sign(s, extra, approve(owner, i))) == "INTENT_KEYS_MISMATCH"
    no_maker = make_intent(None)
    assert refused(lambda: sign(s, no_maker, approve(owner, no_maker))) == "INTENT_ADDRESS_INVALID"


def test_maker_and_domain_must_be_the_signers(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    other = make_intent(Account.create().address)
    assert refused(lambda: sign(s, other, approve(owner, other))) == "INTENT_MAKER_IS_NOT_SIGNER"
    meta = json.loads(json.dumps(META_RAW))
    meta["domain"]["verifyingContract"] = meta["activeAsset"]["escrowAddress"] = "0x" + "ee" * 20
    i = sim.build_intent(network="mainnet", metadata=sim.parse_metadata(meta, "mainnet"), market_hash=H,
                         betting_outcome_one=True, stake_base_units=5_000_000, requested_percentage_odds=40 * 10 ** 18,
                         time_in_force="GTC", expiry=NOW + 3600, idempotency_key="order-key-0009", now=NOW,
                         maker=throwaway[0].address, salt=1)
    assert refused(lambda: sign(s, i, approve(owner, i))) == "INTENT_DOMAIN_NOT_CONFIGURED"


def test_stale_order_expiry_refused(tmp_path, throwaway, owner):
    clock = Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock)
    i = make_intent(throwaway[0].address)
    clock.t = NOW + 3600 - 5
    assert refused(lambda: sign(s, i, approve(owner, i, issued=clock.t))) == "INTENT_EXPIRED_OR_TOO_SOON"


# ---------------------------------------------------------------- limits
def test_stake_and_cumulative_exposure_limits(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, max_stake=5_000_000, max_exposure=8_000_000)
    big = make_intent(throwaway[0].address, stake_base_units=6_000_000, idempotency_key="order-key-B001")
    assert refused(lambda: sign(s, big, approve(owner, big))) == "STAKE_ABOVE_LIMIT"
    ok = make_intent(throwaway[0].address, idempotency_key="order-key-B002")
    sign(s, ok, approve(owner, ok))
    more = make_intent(throwaway[0].address, idempotency_key="order-key-B003")
    assert refused(lambda: sign(s, more, approve(owner, more))) == "EXPOSURE_ABOVE_LIMIT"
    # Exposure survives a restart (rebuilt from the append-only order ledger).
    assert make_signer(tmp_path, throwaway, owner).committed_exposure_base_units == 5_000_000


# ---------------------------------------------------------------- idempotency
def test_double_click_same_key_returns_cached_single_signature(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    first = sign(s, i, approve(owner, i))
    second = sign(s, i, approve(owner, i))
    assert first["cached"] is False and second["cached"] is True
    assert first["order"]["orderSignature"] == second["order"]["orderSignature"]
    assert s.committed_exposure_base_units == 5_000_000
    changed = make_intent(throwaway[0].address, stake_base_units=6_000_000)
    assert refused(lambda: sign(s, changed, approve(owner, changed))) == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"


def test_same_key_after_restart_requires_reconcile(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    sign(s, i, approve(owner, i))
    r = make_signer(tmp_path, throwaway, owner)
    assert refused(lambda: sign(r, i, approve(owner, i))) == "IDEMPOTENCY_KEY_SIGNED_BEFORE_RESTART_RECONCILE_FIRST"
    changed = make_intent(throwaway[0].address, stake_base_units=6_000_000)
    assert refused(lambda: sign(r, changed, approve(owner, changed))) == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"


def test_concurrent_retries_produce_exactly_one_signature(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    approvals = [approve(owner, i) for _ in range(16)]
    results, errors = [], []
    barrier = threading.Barrier(len(approvals))

    def worker(a):
        barrier.wait()
        try:
            results.append(sign(s, i, a))
        except SignerRefused as e:
            errors.append(e.code)

    threads = [threading.Thread(target=worker, args=(a,)) for a in approvals]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(results) == 16
    assert sum(1 for r in results if not r["cached"]) == 1
    assert len({r["order"]["orderSignature"] for r in results}) == 1
    signed = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert sum(1 for r in signed if r["decision"] == "SIGNED") == 1
    assert s.committed_exposure_base_units == 5_000_000


# ---------------------------------------------------------------- audit
def test_audit_is_append_only_hash_chained_and_allowlisted(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    sign(s, i, approve(owner, i))
    with pytest.raises(SignerRefused):
        sign(s, make_intent(throwaway[0].address, idempotency_key="order-key-C001"), "chat ok")
    path = tmp_path / "audit.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    # Intentional change: the atomic authorization is audited before the signature.
    assert [r["decision"] for r in rows] == ["AUTHORIZED", "SIGNED", "REFUSED"]
    assert rows[1]["order_id"].startswith("0x") and rows[1]["intent_hash"] == intent_hash(i)
    assert rows[2]["reason"] == "APPROVAL_MISSING"
    for r in rows:
        assert set(r) <= {"ts_utc", "prev_sha256", "event", "decision", "reason", "signer_id", "idempotency_key",
                          "intent_hash", "order_id", "total_bet_size", "approval_nonce_prefix", "network"}
    with pytest.raises(ValueError):
        AuditLog(str(path)).append(signature="0xdead")
    # Rewriting a past entry breaks the chain and the signer refuses to start on it.
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace('"SIGNED"', '"REFUSED"')
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        make_signer(tmp_path, throwaway, owner)


def test_signer_has_no_submit_or_network_surface():
    public = {n for n in dir(Signer) if not n.startswith("_")}
    # Intentional change: read-only exposure / risk-source views and the crash-recovery release.
    assert public == {"sign_order", "committed_exposure_base_units", "exposure", "risk_config_source",
                      "abandon_unsigned"}
