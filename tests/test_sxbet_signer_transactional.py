"""FIX 2 (transactional approval) + v2 approval binding. Offline, throwaway keys only."""
import json
import threading

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from eth_account import Account  # noqa: E402

from signer import approval as appr  # noqa: E402
from signer.core import Signer, SignerRefused  # noqa: E402
from signer.orders import AUTHORIZED, REJECTED, SIGNED, LedgerError, OrderLedger, OrderStateError  # noqa: E402
from sxbet_signer_kit import (FEES, NOW, SIGNER_ID, Clock, approve, context, make_intent,  # noqa: E402
                              make_signer)


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


# ---------------------------------------------------------------- FIX 2: refusal never burns the approval
def test_refused_by_risk_then_retry_with_same_approval_succeeds(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
    # Balance too low: refused AFTER the approval verified, BEFORE consumption.
    poor = context(i, balance={"available_base_units": "1000", "observed_at": NOW})
    assert refused(lambda: s.sign_order(i, a, poor)) == "INSUFFICIENT_BALANCE"
    assert not s._ledger.nonce_consumed(a.approval_nonce)
    assert [r["kind"] for r in ledger_rows(tmp_path)] == ["GENESIS"]
    out = s.sign_order(i, a, context(i))  # same approval, limits now allow
    assert out["cached"] is False and s._ledger.nonce_consumed(a.approval_nonce)


def test_refused_by_exposure_then_retry_after_release(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, limits={"max_total_exposure_base_units": 6_000_000})
    first = make_intent(throwaway[0].address, idempotency_key="order-key-T001")
    s.sign_order(first, approve(owner, first), context(first))
    second = make_intent(throwaway[0].address, idempotency_key="order-key-T002")
    a2 = approve(owner, second, ttl=120)
    assert refused(lambda: s.sign_order(second, a2, context(second))) == "EXPOSURE_ABOVE_LIMIT"
    # Exchange confirms the first order cancelled with nothing matched: reservation released.
    s._ledger.transition("order-key-T001", "SUBMITTED", reason="TEST_SETUP")
    s._ledger.transition("order-key-T001", "CANCELLED", reason="EXCHANGE_ORDER_INACTIVE", filled=0)
    assert s.sign_order(second, a2, context(second))["cached"] is False


def test_one_approval_two_concurrent_requests_exactly_one_signature(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
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
    a = approve(owner, i)

    def crash(self, msg):
        raise SimulatedCrash()

    monkeypatch.setattr(Signer, "_produce_signature", crash)
    with pytest.raises(SimulatedCrash):
        s.sign_order(i, a, context(i))
    monkeypatch.undo()
    rows = ledger_rows(tmp_path)
    assert [r["kind"] for r in rows] == ["GENESIS", "AUTHORIZE"]  # consumed + reserved, never SIGNED
    assert "orderSignature" not in json.dumps(rows)
    # Restart: state reconcilable, nonce still consumed, same approval cannot be replayed.
    r = make_signer(tmp_path, throwaway, owner)
    assert r._ledger.get(i["idempotency_key"]).state == AUTHORIZED
    assert r.committed_exposure_base_units == 5_000_000
    assert refused(lambda: r.sign_order(i, a, context(i))) == "IDEMPOTENCY_KEY_SIGNED_BEFORE_RESTART_RECONCILE_FIRST"
    other = make_intent(throwaway[0].address, idempotency_key="order-key-X002")
    replay = approve(owner, other, nonce=a.approval_nonce)
    assert refused(lambda: r.sign_order(other, replay, context(other))) == "APPROVAL_NONCE_REPLAYED"
    # Explicit release (no signature ever left): exposure back to zero, nonce still consumed.
    assert r.abandon_unsigned(i["idempotency_key"]) == REJECTED
    assert r.committed_exposure_base_units == 0
    assert make_signer(tmp_path, throwaway, owner)._ledger.nonce_consumed(a.approval_nonce)
    assert refused(lambda: r.abandon_unsigned(i["idempotency_key"])) == "NOT_AN_ABANDONED_AUTHORIZATION"


def test_in_process_signing_failure_releases_and_never_returns_signature(tmp_path, throwaway, owner, monkeypatch):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
    monkeypatch.setattr(Signer, "_produce_signature", lambda self, msg: (_ for _ in ()).throw(RuntimeError("hsm")))
    assert refused(lambda: s.sign_order(i, a, context(i))) == "SIGNING_FAILED"
    assert s._ledger.get(i["idempotency_key"]).state == REJECTED and s.committed_exposure_base_units == 0
    assert s._ledger.nonce_consumed(a.approval_nonce)


def test_signed_is_persisted_before_signature_release(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    s.sign_order(i, approve(owner, i), context(i))
    assert [r.get("to") for r in ledger_rows(tmp_path)][-1] == SIGNED
    assert refused(lambda: s.abandon_unsigned(i["idempotency_key"])) == "NOT_AN_ABANDONED_AUTHORIZATION"


def test_replay_after_restart_refused(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
    s.sign_order(i, a, context(i))
    r = make_signer(tmp_path, throwaway, owner)
    other = make_intent(throwaway[0].address, idempotency_key="order-key-X003")
    assert refused(lambda: r.sign_order(other, approve(owner, other, nonce=a.approval_nonce),
                                        context(other))) == "APPROVAL_NONCE_REPLAYED"


def test_same_digest_under_new_key_refused(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address, salt=7)
    s.sign_order(i, approve(owner, i), context(i))
    twin = make_intent(throwaway[0].address, salt=7, idempotency_key="order-key-TWIN")  # same signed struct
    assert refused(lambda: s.sign_order(twin, approve(owner, twin), context(twin))) == "ORDER_ID_ALREADY_AUTHORIZED"


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


# ---------------------------------------------------------------- v2 approval binding
@pytest.mark.parametrize("field,value,code", [
    ("account_id", "someone-else", "APPROVAL_BINDING_MISMATCH_ACCOUNT_ID"),
    ("maker", "0x" + "11" * 20, "APPROVAL_BINDING_MISMATCH_MAKER"),
    ("chain_id", 79479957, "APPROVAL_BINDING_MISMATCH_CHAIN_ID"),
    ("verifying_contract", "0x" + "ee" * 20, "APPROVAL_BINDING_MISMATCH_VERIFYING_CONTRACT"),
    ("network", "testnet", "APPROVAL_BINDING_MISMATCH_NETWORK"),
    ("market_hash", "0x" + "cd" * 32, "APPROVAL_BINDING_MISMATCH_MARKET_HASH"),
    ("stake_base_units", "4000000", "APPROVAL_BINDING_MISMATCH_STAKE_BASE_UNITS"),
    ("percentage_odds_limit", str(41 * 10 ** 18), "APPROVAL_BINDING_MISMATCH_PERCENTAGE_ODDS_LIMIT"),
    ("time_in_force", "FOK", "APPROVAL_BINDING_MISMATCH_TIME_IN_FORCE"),
    ("order_expiry", NOW + 7200, "APPROVAL_BINDING_MISMATCH_ORDER_EXPIRY"),
    ("client_order_id", "order-key-OTHER", "APPROVAL_BINDING_MISMATCH_CLIENT_ORDER_ID"),
    ("event_id", "L999", "APPROVAL_BINDING_MISMATCH_EVENT_ID"),
    ("selection", {"outcome": 1, "label": "Away FC"}, "APPROVAL_BINDING_MISMATCH_SELECTION_LABEL"),
    ("expected_fees", {**FEES, "takerPayoutFee": "0.04"}, "FEES_CHANGED_SINCE_APPROVAL"),
    ("max_exposure_base_units", "4999999", "ORDER_EXPOSURE_ABOVE_APPROVED_MAX"),
])
def test_owner_signed_binding_must_match_every_field(tmp_path, throwaway, owner, field, value, code):
    """The owner signs a binding that disagrees with the order: the signer re-derives and refuses."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i, **{field: value})
    assert refused(lambda: s.sign_order(i, a, context(i))) == code
    assert not s._ledger.nonce_consumed(a.approval_nonce)  # refusal never burns the approval


def test_binding_side_inconsistent_with_selection_is_malformed(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i, selection={"outcome": 2, "label": "Away FC"})  # outcome 2 but side = outcome one
    assert refused(lambda: s.sign_order(i, a, context(i))) == "APPROVAL_MALFORMED"


def test_any_field_edited_after_owner_signature_breaks_it(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
    edited = appr.Approval({**a.binding, "max_exposure_base_units": "99000000"}, a.approval_nonce, a.issued_at,
                           a.expires_at, a.signer_id, a.signature)
    assert refused(lambda: s.sign_order(i, edited, context(i))) == "APPROVAL_SIGNATURE_INVALID"


def test_v1_style_approval_is_refused(tmp_path, throwaway, owner):
    """Intentional non-backward-compatible change: intent-hash-only approvals are rejected."""
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
    v1 = appr.Approval({"intent_hash": a.intent_hash}, a.approval_nonce, a.issued_at, a.expires_at, a.signer_id,
                       a.signature)
    assert refused(lambda: s.sign_order(i, v1, context(i))) == "APPROVAL_MALFORMED"


def test_ttl_cap_and_clock(tmp_path, throwaway, owner):
    clock = Clock()
    s = make_signer(tmp_path, throwaway, owner, clock=clock)
    i = make_intent(throwaway[0].address)
    assert appr.MAX_TTL_SECONDS <= 120
    assert refused(lambda: s.sign_order(i, approve(owner, i, ttl=121), context(i))) == "APPROVAL_TTL_TOO_LONG"


def test_signer_without_account_id_refuses(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner, account_id="")
    i = make_intent(throwaway[0].address)
    assert refused(lambda: s.sign_order(i, approve(owner, i), context(i))) == "APPROVAL_BINDING_MISMATCH_ACCOUNT_ID"


# ---------------------------------------------------------------- L1: approval binding / context snapshot
def test_l1_binding_mutated_during_risk_evaluation_has_no_effect(tmp_path, throwaway, owner):
    """A caller-owned binding dict mutated after verification (here: while the risk engine reads the
    context) must not change what is checked or recorded: only the verified snapshot is used."""
    from collections.abc import Mapping

    from sxbet_signer_kit import EVENT_ID
    s = make_signer(tmp_path, throwaway, owner, limits={"max_exposure_per_event_base_units": 6_000_000})
    first = make_intent(throwaway[0].address, idempotency_key="order-key-L001")
    s.sign_order(first, approve(owner, first), context(first))
    i = make_intent(throwaway[0].address, idempotency_key="order-key-L002")
    a = approve(owner, i)
    base = context(i)

    class Sneaky(Mapping):
        def __getitem__(self, k):
            if k == "balance":  # read after the event-id check, before the per-event limit check
                a.binding["event_id"] = "L-OTHER-EVENT"
            return base[k]

        def __iter__(self):
            return iter(base)

        def __len__(self):
            return len(base)

    assert refused(lambda: s.sign_order(i, a, Sneaky())) == "EVENT_EXPOSURE_ABOVE_LIMIT"
    assert s.exposure().by_event == {EVENT_ID: 5_000_000} and s._ledger.get("order-key-L002") is None


def test_l1_snapshot_refuses_non_plain_binding(tmp_path, throwaway, owner):
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
    weird = appr.Approval({**a.binding, "expected_fees": {1: "0.01"}}, a.approval_nonce, a.issued_at,
                          a.expires_at, a.signer_id, a.signature)
    assert refused(lambda: s.sign_order(i, weird, context(i))) == "APPROVAL_MALFORMED"
    ctx = {**context(i), "quote": {**context(i)["quote"], "observed_at": object()}}
    assert refused(lambda: s.sign_order(i, a, ctx)) == "RISK_CONTEXT_MALFORMED"
    assert not s._ledger.nonce_consumed(a.approval_nonce)


def test_l3_signed_line_not_persisted_means_no_signature_returned(tmp_path, throwaway, owner, monkeypatch):
    from signer import orders as od
    s = make_signer(tmp_path, throwaway, owner)
    i = make_intent(throwaway[0].address)
    a = approve(owner, i)
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
    assert refused(lambda: s.sign_order(other, approve(owner, other), context(other))) == "LEDGER_POISONED"
