"""FIX 2 (transactional authorization) + owner-command binding to every order field. Offline, throwaway keys only.

Intentional change (§2.6 rewrite, #187): the per-order Ed25519 `Approval` (and its v2 binding) is replaced by
the owner's explicit command (`OwnerCommandAuthorization`), parsed and compared field by field by the signer.
The transactional properties are unchanged: a refusal never consumes the command; consumption is atomic with
AUTHORIZE in the ledger; a consumed command id never authorises a second order, also after a restart.
"""
import json
import threading
from dataclasses import dataclass

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import command as cm  # noqa: E402
from signer.core import Signer, SignerRefused  # noqa: E402
from signer.orders import AUTHORIZED, REJECTED, SIGNED, LedgerError, OrderLedger, OrderStateError  # noqa: E402
from sxbet_signer_kit import (NOW, SIGNER_ID, context, make_intent, make_signer,  # noqa: E402
                              owner_command)


@pytest.fixture
def throwaway():
    acct = Account.create()
    return acct, acct.key.hex()


@pytest.fixture
def owner():
    return Ed25519PrivateKey.generate()


def refused(fn):
    with pytest.raises(SignerRefused) as e:
        fn()
    return e.value.code


def ledger_rows(tmp_path):
    return [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]


# ---------------------------------------------------------------- FIX 2: refusal never burns the command
def test_refused_by_risk_then_retry_with_same_approval_succeeds(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    # Balance too low: refused AFTER the command verified, BEFORE consumption.
    poor = context(i, balance={"available_base_units": "1000", "observed_at": NOW})
    assert refused(lambda: s.sign_order(i, a, poor)) == "INSUFFICIENT_BALANCE"
    assert not s._ledger.command_consumed(a.command_id)
    assert [r["kind"] for r in ledger_rows(tmp_path)] == ["GENESIS"]
    out = s.sign_order(i, a, context(i))  # same command, limits now allow
    assert out["cached"] is False and s._ledger.command_consumed(a.command_id)


def test_refused_by_exposure_then_retry_after_release(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_total_exposure_base_units": 6_000_000})
    first = make_intent(throwaway[0].address, idempotency_key="order-key-T001")
    s.sign_order(first, owner_command(first), context(first))
    second = make_intent(throwaway[0].address, idempotency_key="order-key-T002")
    a2 = owner_command(second)
    assert refused(lambda: s.sign_order(second, a2, context(second))) == "EXPOSURE_ABOVE_LIMIT"
    # Exchange confirms the first order cancelled with nothing matched: reservation released.
    s._ledger.transition("order-key-T001", "SUBMITTED", reason="TEST_SETUP")
    s._ledger.transition("order-key-T001", "CANCELLED", reason="EXCHANGE_ORDER_INACTIVE", filled=0)
    assert s.sign_order(second, a2, context(second))["cached"] is False


def test_one_approval_two_concurrent_requests_exactly_one_signature(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(s.sign_order(i, a, context(i)))
        except SignerRefused as e:
            errors.append(e.code)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == [] and sum(1 for r in results if not r["cached"]) == 1
    assert len({r["order"]["orderSignature"] for r in results}) == 1
    assert sum(1 for r in ledger_rows(tmp_path) if r["kind"] == "AUTHORIZE") == 1


def test_ledger_nonce_race_at_storage_level(tmp_path, throwaway):
    """Different orders racing on one nonce directly at the ledger: exactly one AUTHORIZE."""
    led = OrderLedger.create(str(tmp_path / "l"), signer_id=SIGNER_ID, signer_address=throwaway[0].address)
    ok, bad = [], []
    barrier = threading.Barrier(10)

    def worker(n):
        barrier.wait()
        try:
            led.authorize(client_order_id=f"order-key-R{n:03d}", intent_hash="sha256:" + "0" * 64,
                          order_id="0x" + f"{n:064x}", market_hash="0x" + "ab" * 32, event_id="L1", stake=1,
                          expiry=NOW + 3600, approval_nonce="nonce-shared-0000001")
            ok.append(n)
        except OrderStateError as e:
            bad.append(e.code)

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 1 and bad == ["APPROVAL_NONCE_REPLAYED"] * 9


class SimulatedCrash(BaseException):
    """Not an Exception: models the process dying (nothing below catches it)."""


def test_crash_between_consume_and_sign_keeps_nonce_consumed(tmp_path, throwaway, owner, monkeypatch):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)

    def crash(self, msg):
        raise SimulatedCrash()

    monkeypatch.setattr(Signer, "_produce_signature", crash)
    with pytest.raises(SimulatedCrash):
        s.sign_order(i, a, context(i))
    monkeypatch.undo()
    rows = ledger_rows(tmp_path)
    assert [r["kind"] for r in rows] == ["GENESIS", "AUTHORIZE"]  # consumed + reserved, never SIGNED
    assert "orderSignature" not in json.dumps(rows)
    assert a.command_text not in json.dumps(rows)  # only hashes of the command are recorded
    # Restart: state reconcilable, command still consumed, the same command id cannot be replayed.
    r = make_signer(tmp_path, throwaway, owner)
    assert r._ledger.get(i["idempotency_key"]).state == AUTHORIZED
    assert r.committed_exposure_base_units == 5_000_000
    assert refused(lambda: r.sign_order(i, a, context(i))) == "IDEMPOTENCY_KEY_SIGNED_BEFORE_RESTART_RECONCILE_FIRST"
    other = make_intent(throwaway[0].address, idempotency_key="order-key-X002")
    replay = owner_command(other, command_id=a.command_id)
    assert refused(lambda: r.sign_order(other, replay, context(other))) == "COMMAND_ID_ALREADY_CONSUMED"
    # Explicit release (no signature ever left): exposure back to zero, command still consumed.
    assert r.abandon_unsigned(i["idempotency_key"]) == REJECTED
    assert r.committed_exposure_base_units == 0
    assert make_signer(tmp_path, throwaway, owner)._ledger.command_consumed(a.command_id)
    assert refused(lambda: r.abandon_unsigned(i["idempotency_key"])) == "NOT_AN_ABANDONED_AUTHORIZATION"


def test_in_process_signing_failure_releases_and_never_returns_signature(tmp_path, throwaway, owner, monkeypatch):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    monkeypatch.setattr(Signer, "_produce_signature", lambda self, msg: (_ for _ in ()).throw(RuntimeError("hsm")))
    assert refused(lambda: s.sign_order(i, a, context(i))) == "SIGNING_FAILED"
    assert s._ledger.get(i["idempotency_key"]).state == REJECTED and s.committed_exposure_base_units == 0
    assert s._ledger.command_consumed(a.command_id)


def test_signed_is_persisted_before_signature_release(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    s.sign_order(i, owner_command(i), context(i))
    assert [r.get("to") for r in ledger_rows(tmp_path)][-1] == SIGNED
    assert refused(lambda: s.abandon_unsigned(i["idempotency_key"])) == "NOT_AN_ABANDONED_AUTHORIZATION"


def test_replay_after_restart_refused(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    s.sign_order(i, a, context(i))
    r = make_signer(tmp_path, throwaway, owner)
    other = make_intent(throwaway[0].address, idempotency_key="order-key-X003")
    assert refused(lambda: r.sign_order(other, owner_command(other, command_id=a.command_id),
                                        context(other))) == "COMMAND_ID_ALREADY_CONSUMED"
    # The same TEXT under a new id is refused too (one command = one operation).
    assert refused(lambda: r.sign_order(other, owner_command(other, text=a.command_text),
                                        context(other))) == "COMMAND_TEXT_REPLAYED"


def test_same_digest_under_new_key_refused(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address, salt=7)
    s.sign_order(i, owner_command(i), context(i))
    twin = make_intent(throwaway[0].address, salt=7, idempotency_key="order-key-TWIN")  # same signed struct
    assert refused(lambda: s.sign_order(twin, owner_command(twin), context(twin))) == "ORDER_ID_ALREADY_AUTHORIZED"


# ---------------------------------------------------------------- ledger fail-closed (signer refuses)
def test_missing_or_foreign_ledger_fails_closed(tmp_path, throwaway, owner):
    with pytest.raises(LedgerError) as e:
        OrderLedger.open(str(tmp_path / "nope.jsonl"), signer_id=SIGNER_ID, signer_address=throwaway[0].address)
    assert e.value.code == "LEDGER_MISSING"
    OrderLedger.create(str(tmp_path / "other.jsonl"), signer_id=SIGNER_ID, signer_address=Account.create().address)
    with pytest.raises(LedgerError) as e:
        OrderLedger.open(str(tmp_path / "other.jsonl"), signer_id=SIGNER_ID, signer_address=throwaway[0].address)
    assert e.value.code == "LEDGER_FOREIGN"
    with pytest.raises(LedgerError):
        OrderLedger.create(str(tmp_path / "other.jsonl"), signer_id=SIGNER_ID, signer_address=throwaway[0].address)
    (tmp_path / "empty.jsonl").write_text("")
    with pytest.raises(LedgerError) as e:
        OrderLedger.open(str(tmp_path / "empty.jsonl"), signer_id=SIGNER_ID, signer_address=throwaway[0].address)
    assert e.value.code == "LEDGER_EMPTY"



# ---------------------------------------------------------------- owner command must match every order field
# (intentional change: replaces the v2 approval-binding tests; signer-config fields such as maker, chain,
# verifying contract, network are still checked against the intent by test_maker_and_domain_must_be_the_signers)
BASE = "Metti un Limit a quota 2,5 per 5 USDC su Home FC in Home FC–Away FC"


@pytest.mark.parametrize("text,code", [
    (BASE.replace("Home FC–Away FC", "Home FC–Other FC"), "COMMAND_MISMATCH_EVENT"),
    (BASE.replace("Home FC–Away FC", "Away FC–Home FC"), "COMMAND_MISMATCH_EVENT"),
    (BASE.replace(" in Home FC–Away FC", " in evento L999"), "COMMAND_MISMATCH_EVENT"),
    (BASE.replace("su Home FC in", "su Away FC in"), "COMMAND_MISMATCH_SELECTION"),
    (BASE.replace("su Home FC in", "su Over 2,5 in"), "COMMAND_MISMATCH_MARKET"),
    (BASE.replace("su Home FC in", "su Home FC +1,5 in"), "COMMAND_MISMATCH_MARKET"),
    (BASE + " (tipo mercato 226)", "COMMAND_MISMATCH_MARKET"),
    (BASE.replace("per 5 USDC", "per 4 USDC"), "COMMAND_MISMATCH_STAKE"),
    (BASE.replace("per 5 USDC", "per 5,0000001 USDC"), "COMMAND_INVALID_STAKE_PRECISION"),
    (BASE.replace("per 5 USDC", "per 5 USDT"), "COMMAND_MISMATCH_CURRENCY"),
    (BASE.replace("quota 2,5", "quota 2,45"), "COMMAND_MISMATCH_ODDS"),
    ("Piazza 5 USDC su Home FC in Home FC–Away FC a quota minima 2,5", "COMMAND_MISMATCH_ORDER_TYPE"),
    (BASE + " post-only", "COMMAND_MISMATCH_POST_ONLY"),
])
def test_owner_signed_binding_must_match_every_field(tmp_path, throwaway, owner, text, code):
    """The owner's command disagrees with the order: the signer re-derives every field and refuses."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i, text=text)
    assert refused(lambda: s.sign_order(i, a, context(i))) == code
    assert not s._ledger.command_consumed(a.command_id)  # refusal never burns the command
    assert s.sign_order(i, owner_command(i), context(i))["cached"] is False  # the exact command still works


@pytest.mark.parametrize("field,value,code", [
    # The fields of the old approval binding that are not part of an owner's command are still bound: the
    # signer re-derives them from its own configuration (account, maker, network / chain, domain, token).
    ("maker", "0x" + "11" * 20, "INTENT_MAKER_IS_NOT_SIGNER"),
    ("network", "testnet", "INTENT_CHAIN_MISMATCH"),
    ("verifying_contract", "0x" + "ee" * 20, "INTENT_DOMAIN_NOT_CONFIGURED"),
    ("base_token", "0x" + "ee" * 20, "INTENT_DOMAIN_NOT_CONFIGURED"),
])
def test_signer_config_fields_still_bound_to_the_intent(tmp_path, throwaway, owner, field, value, code):
    s = make_signer(tmp_path, throwaway, owner)
    i = {**make_intent(throwaway[0].address), field: value}
    a = owner_command(make_intent(throwaway[0].address))
    assert refused(lambda: s.sign_order(i, a, context(i))) == code
    assert not s._ledger.command_consumed(a.command_id)


def test_binding_side_inconsistent_with_selection_is_malformed(tmp_path, throwaway, owner):
    """The command names the label of the OTHER outcome than the side being signed."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address, betting_outcome_one=False)
    a = owner_command(i, label="Home FC")  # outcome two is "Away FC"
    assert refused(lambda: s.sign_order(i, a, context(i))) == "COMMAND_MISMATCH_SELECTION"


def test_any_field_edited_after_owner_signature_breaks_it(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    edited = cm.OwnerCommandAuthorization(a.command_id, a.command_text.replace("quota 2,5", "quota 2,4"),
                                          a.command_text_sha256, a.received_at, a.client_ref_sha256)
    assert refused(lambda: s.sign_order(i, edited, context(i))) == "COMMAND_TEXT_HASH_MISMATCH"


@dataclass(frozen=True)
class LegacyApproval:  # the shape of the removed per-order Ed25519 Approval
    binding: dict
    approval_nonce: str
    issued_at: int
    expires_at: int
    signer_id: str
    signature: bytes


def test_v1_style_approval_is_refused(tmp_path, throwaway, owner):
    """Intentional non-backward-compatible change: a per-order Ed25519 approval object is no longer accepted."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    legacy = LegacyApproval({"intent_hash": "sha256:" + "0" * 64}, "nonce-0000000000000001", NOW, NOW + 60,
                            SIGNER_ID, bytes(64))
    assert refused(lambda: s.sign_order(i, legacy, context(i))) == "COMMAND_MISSING"


def test_ttl_cap_and_clock(tmp_path, throwaway, owner):
    """The command window is owner-signed and capped at 300 s: a longer window makes the config malformed
    (all-zero limits => every order refused)."""
    assert cm.COMMAND_MAX_AGE_HARD_CAP_SECONDS <= 300
    s = make_signer(tmp_path, throwaway, owner, limits={"command_max_age_seconds": 301})
    assert s.risk_config_source == "MALFORMED"
    i = make_intent(throwaway[0].address)
    assert refused(lambda: s.sign_order(i, owner_command(i), context(i))) == "EXPOSURE_LIMIT_ZERO"


def test_signer_without_account_id_refuses(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, account_id="")
    i = make_intent(throwaway[0].address)
    assert refused(lambda: s.sign_order(i, owner_command(i), context(i))) == "ACCOUNT_ID_NOT_CONFIGURED"


# ---------------------------------------------------------------- L1: command / context snapshot
def test_l1_binding_mutated_during_risk_evaluation_has_no_effect(tmp_path, throwaway, owner):
    """A caller-owned authorization whose fields change between reads must not change what is checked or
    recorded: the signer reads every field once (snapshot) and uses only the copy."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    good = owner_command(i)
    other_text = good.command_text.replace("per 5 USDC", "per 9 USDC")

    class Shifty(cm.OwnerCommandAuthorization):
        reads = [0]

        def __getattribute__(self, name):
            if name == "command_text":
                Shifty.reads[0] += 1
                return good.command_text if Shifty.reads[0] == 1 else other_text
            return super().__getattribute__(name)

    sh = Shifty(good.command_id, good.command_text, good.command_text_sha256, good.received_at,
                good.client_ref_sha256)
    assert s.sign_order(i, sh, context(i))["cached"] is False
    (auth,) = [r for r in ledger_rows(tmp_path) if r["kind"] == "AUTHORIZE"]
    assert auth["command_text_sha256"] == cm.text_sha256(good.command_text)
    assert auth["command_canonical_sha256"] == cm.canonical_sha256(good.command_text)


def test_l1_snapshot_refuses_non_plain_binding(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    weird = cm.OwnerCommandAuthorization(a.command_id, a.command_text.encode(), a.command_text_sha256,
                                         a.received_at, a.client_ref_sha256)
    assert refused(lambda: s.sign_order(i, weird, context(i))) == "COMMAND_MALFORMED"
    ctx = {**context(i), "quote": {**context(i)["quote"], "observed_at": object()}}
    assert refused(lambda: s.sign_order(i, a, ctx)) == "RISK_CONTEXT_MALFORMED"
    assert not s._ledger.command_consumed(a.command_id)


def test_l3_signed_line_not_persisted_means_no_signature_returned(tmp_path, throwaway, owner, monkeypatch):
    from signer import orders as od
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = owner_command(i)
    real = od.OrderLedger._append

    def fail_on_signed(self, rec):
        if rec.get("to") == SIGNED:
            self._poisoned = True
            raise od.LedgerError("LEDGER_WRITE_FAILED")
        return real(self, rec)

    monkeypatch.setattr(od.OrderLedger, "_append", fail_on_signed)
    assert refused(lambda: s.sign_order(i, a, context(i))) == "LEDGER_WRITE_FAILED"
    monkeypatch.undo()
    assert s._ledger.get(i["idempotency_key"]).state == AUTHORIZED  # reserved, never handed out
    other = make_intent(throwaway[0].address, idempotency_key="order-key-P002")
    assert refused(lambda: s.sign_order(other, owner_command(other), context(other))) == "LEDGER_POISONED"
