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
from signer.command import OwnerCommandAuthorization  # noqa: E402
from signer.audit import AuditLog  # noqa: E402
from signer.core import KEY_ENV, Signer, SignerConfig, SignerConfigError, SignerRefused  # noqa: E402
from signer.intent import intent_hash  # noqa: E402
from signer.orders import OrderLedger  # noqa: E402
from sxbet_signer_kit import (ACCOUNT_ID, ESCROW, H, META_RAW, NOW, SIGNER_ID, TOKEN, Clock, context, owner_command,  # noqa: E402
                              make_intent, pub)
from sxbet_signer_kit import make_signer as _make_signer  # noqa: E402


@pytest.fixture
def throwaway():
    acct = Account.create()  # fresh, never funded, discarded after the test
    return acct, acct.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()  # throwaway owner key (risk config, flags)


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
    out = sign(s, i, owner_command(i))
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
    out = sign(s, i, owner_command(i))
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


# ---------------------------------------------------------------- owner command (intentional change: the per-order
# Ed25519 Approval is replaced by the owner's explicit command verified by the signer, §2.6 as rewritten)
def test_zero_exposure_default_refuses(tmp_path, throwaway, owner):
    acct, key = throwaway
    # No owner-signed risk config in the env => all-zero limits => refuse everything.
    cfg = SignerConfig(SIGNER_ID, "mainnet", 4162, ESCROW, TOKEN, 125, account_id=ACCOUNT_ID)
    ledger = OrderLedger.create(str(tmp_path / "l"), signer_id=SIGNER_ID, signer_address=acct.address)
    s = Signer(cfg, owner_public_key=pub(owner), ledger=ledger, audit=AuditLog(str(tmp_path / "a")),
               env={KEY_ENV: key}, clock=Clock())
    assert s.risk_config_source == "MISSING" and s.committed_exposure_base_units == 0
    i = make_intent(acct.address)
    assert refused(lambda: sign(s, i, owner_command(i))) == "EXPOSURE_LIMIT_ZERO"


def test_command_text_hash_must_match_the_text(tmp_path, throwaway, owner):
    """Was test_invalid_approval_signature: the signer recomputes the text hash of the literal command."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    forged = OwnerCommandAuthorization(a.command_id, a.command_text, "sha256:" + "0" * 64, a.received_at,
                                       a.client_ref_sha256)
    assert refused(lambda: sign(s, i, forged)) == "COMMAND_TEXT_HASH_MISMATCH"
    # Editing the text after the hash was taken (e.g. a bigger stake) is detected too.
    edited = OwnerCommandAuthorization(a.command_id, a.command_text.replace("per 5 USDC", "per 9 USDC"),
                                       a.command_text_sha256, a.received_at, a.client_ref_sha256)
    assert refused(lambda: sign(s, i, edited)) == "COMMAND_TEXT_HASH_MISMATCH"
    assert not s._ledger.command_consumed(a.command_id)


def test_malformed_command_record(tmp_path, throwaway, owner):
    """Was test_wrong_owner_key: there is no per-order owner key any more; a malformed record is refused."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    for bad in (OwnerCommandAuthorization("short", a.command_text, a.command_text_sha256, NOW, a.client_ref_sha256),
                OwnerCommandAuthorization(a.command_id, a.command_text, a.command_text_sha256, NOW, "ref"),
                OwnerCommandAuthorization(a.command_id, a.command_text, a.command_text_sha256, str(NOW),
                                          a.client_ref_sha256)):
        assert refused(lambda: sign(s, i, bad)) == "COMMAND_MALFORMED"


def test_chat_text_alone_is_not_an_authorization(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    assert refused(lambda: sign(s, i, "I, the owner, confirm this order")) == "COMMAND_MISSING"
    assert refused(lambda: sign(s, i, None)) == "COMMAND_MISSING"
    assert refused(lambda: sign(s, i, {"command_text": "Metti un Limit ..."})) == "COMMAND_MISSING"


def test_expired_and_future_commands(tmp_path, throwaway, owner):
    clock = Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock)  # owner-signed window: 120 s
    i = make_intent(throwaway[0].address)
    clock.t = NOW + 121
    assert refused(lambda: sign(s, i, owner_command(i, received_at=NOW), context(i, now=clock.t))) \
        == "COMMAND_EXPIRED"
    clock.t = NOW + 120
    assert sign(s, i, owner_command(i, received_at=NOW), context(i, now=clock.t))["cached"] is False
    clock.t = NOW
    j = make_intent(throwaway[0].address, idempotency_key="order-key-F001")
    assert refused(lambda: sign(s, j, owner_command(j, received_at=NOW + 60))) == "COMMAND_NOT_YET_VALID"


def test_command_window_comes_from_the_owner_signed_config(tmp_path, throwaway, owner):
    clock = Clock(NOW + 31)
    s = _make_signer(tmp_path, throwaway, owner, clock=clock, limits={"command_max_age_seconds": 30})
    i = make_intent(throwaway[0].address)
    assert refused(lambda: sign(s, i, owner_command(i, received_at=NOW), context(i, now=clock.t))) \
        == "COMMAND_EXPIRED"


def test_replayed_command_id_refused_even_after_restart(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i1 = make_intent(throwaway[0].address, idempotency_key="order-key-A001")
    sign(s, i1, owner_command(i1, command_id="cmd-replay-0000000001"))
    i2 = make_intent(throwaway[0].address, idempotency_key="order-key-A002")
    a2 = owner_command(i2, command_id="cmd-replay-0000000001")  # same id, different (valid) command text
    assert refused(lambda: sign(s, i2, a2)) == "COMMAND_ID_ALREADY_CONSUMED"
    restarted = make_signer(tmp_path, throwaway, owner)
    assert refused(lambda: sign(restarted, i2, a2)) == "COMMAND_ID_ALREADY_CONSUMED"


@pytest.mark.parametrize("change,code", [
    ({"requested_percentage_odds": 41 * 10 ** 18}, "COMMAND_MISMATCH_ODDS"),   # price moved
    ({"stake_base_units": 6_000_000}, "COMMAND_MISMATCH_STAKE"),               # stake changed
    ({"betting_outcome_one": False}, "COMMAND_MISMATCH_SELECTION"),            # side changed
    ({"time_in_force": "FOK"}, "COMMAND_MISMATCH_ORDER_TYPE"),                 # limit -> taker
])
def test_any_parameter_change_breaks_the_command(tmp_path, throwaway, owner, change, code):
    s = make_signer(tmp_path, throwaway, owner)
    commanded = make_intent(throwaway[0].address)
    a = owner_command(commanded)
    changed = make_intent(throwaway[0].address, **change)
    assert refused(lambda: sign(s, changed, a)) == code
    assert not s._ledger.command_consumed(a.command_id)


def test_market_change_is_caught_by_the_signers_market_row(tmp_path, throwaway, owner):
    """Was the market_hash case above: the command names the market by event + selection + line, so a
    different market of the same event with the same labels makes the command ambiguous (§2.3bis)."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    ctx = context(i)
    twin = {**ctx["market"], "marketHash": "0x" + "cd" * 32, "type": 226}
    ctx["event_markets"] = [ctx["market"], twin]
    assert refused(lambda: sign(s, i, owner_command(i), ctx)) == "COMMAND_AMBIGUOUS_MARKET"
    other = make_intent(throwaway[0].address, market_hash="0x" + "cd" * 32)
    assert refused(lambda: sign(s, other, owner_command(i), context(i))) == "MARKET_MISMATCH"




def test_tampered_intent_dict_is_revalidated(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    bad = {**i, "percentage_odds": str(40 * 10 ** 18 + 1)}
    assert refused(lambda: sign(s, bad, owner_command(bad))) == "INTENT_ODDS_NOT_ON_LADDER"
    lie = {**i, "percentage_odds": str(39 * 10 ** 18)}  # on ladder but not the round-down of requested
    assert refused(lambda: sign(s, lie, owner_command(lie))) == "INTENT_ODDS_NOT_ROUND_DOWN_OF_REQUESTED"
    extra = {**i, "note": "x"}
    assert refused(lambda: sign(s, extra, owner_command(i))) == "INTENT_KEYS_MISMATCH"
    no_maker = make_intent(None)
    assert refused(lambda: sign(s, no_maker, owner_command(no_maker))) == "INTENT_ADDRESS_INVALID"


def test_maker_and_domain_must_be_the_signers(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    other = make_intent(Account.create().address)
    assert refused(lambda: sign(s, other, owner_command(other))) == "INTENT_MAKER_IS_NOT_SIGNER"
    meta = json.loads(json.dumps(META_RAW))
    meta["domain"]["verifyingContract"] = meta["activeAsset"]["escrowAddress"] = "0x" + "ee" * 20
    i = sim.build_intent(network="mainnet", metadata=sim.parse_metadata(meta, "mainnet"), market_hash=H,
                         betting_outcome_one=True, stake_base_units=5_000_000, requested_percentage_odds=40 * 10 ** 18,
                         time_in_force="GTC", expiry=NOW + 3600, idempotency_key="order-key-0009", now=NOW,
                         maker=throwaway[0].address, salt=1)
    assert refused(lambda: sign(s, i, owner_command(i))) == "INTENT_DOMAIN_NOT_CONFIGURED"


def test_stale_order_expiry_refused(tmp_path, throwaway, owner):
    clock = Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock)
    i = make_intent(throwaway[0].address)
    clock.t = NOW + 3600 - 5
    assert refused(lambda: sign(s, i, owner_command(i, received_at=clock.t))) == "INTENT_EXPIRED_OR_TOO_SOON"


# ---------------------------------------------------------------- limits
def test_stake_and_cumulative_exposure_limits(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, max_stake=5_000_000, max_exposure=8_000_000)
    big = make_intent(throwaway[0].address, stake_base_units=6_000_000, idempotency_key="order-key-B001")
    assert refused(lambda: sign(s, big, owner_command(big))) == "STAKE_ABOVE_LIMIT"
    ok = make_intent(throwaway[0].address, idempotency_key="order-key-B002")
    sign(s, ok, owner_command(ok))
    more = make_intent(throwaway[0].address, idempotency_key="order-key-B003")
    assert refused(lambda: sign(s, more, owner_command(more))) == "EXPOSURE_ABOVE_LIMIT"
    # Exposure survives a restart (rebuilt from the append-only order ledger).
    assert make_signer(tmp_path, throwaway, owner).committed_exposure_base_units == 5_000_000


# ---------------------------------------------------------------- idempotency
def test_double_click_same_key_returns_cached_single_signature(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    first = sign(s, i, owner_command(i))
    second = sign(s, i, owner_command(i))
    assert first["cached"] is False and second["cached"] is True
    assert first["order"]["orderSignature"] == second["order"]["orderSignature"]
    assert s.committed_exposure_base_units == 5_000_000
    changed = make_intent(throwaway[0].address, stake_base_units=6_000_000)
    assert refused(lambda: sign(s, changed, owner_command(changed))) == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"


def test_same_key_after_restart_requires_reconcile(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    sign(s, i, owner_command(i))
    r = make_signer(tmp_path, throwaway, owner)
    assert refused(lambda: sign(r, i, owner_command(i))) == "IDEMPOTENCY_KEY_SIGNED_BEFORE_RESTART_RECONCILE_FIRST"
    changed = make_intent(throwaway[0].address, stake_base_units=6_000_000)
    assert refused(lambda: sign(r, changed, owner_command(changed))) == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"


def test_concurrent_retries_produce_exactly_one_signature(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    approvals = [owner_command(i) for _ in range(16)]
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
    sign(s, i, owner_command(i))
    with pytest.raises(SignerRefused):
        sign(s, make_intent(throwaway[0].address, idempotency_key="order-key-C001"), "chat ok")
    path = tmp_path / "audit.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    # Intentional change: the atomic authorization is audited before the signature.
    assert [r["decision"] for r in rows] == ["AUTHORIZED", "SIGNED", "REFUSED"]
    assert rows[1]["order_id"].startswith("0x") and rows[1]["intent_hash"] == intent_hash(i)
    assert rows[2]["reason"] == "COMMAND_MISSING"  # intentional: owner command, not an Ed25519 approval
    for r in rows:
        assert set(r) <= {"ts_utc", "prev_sha256", "event", "decision", "reason", "signer_id", "idempotency_key",
                          "intent_hash", "order_id", "total_bet_size", "approval_nonce_prefix", "network",
                          "owner_command_id", "owner_command_sha256"}
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
