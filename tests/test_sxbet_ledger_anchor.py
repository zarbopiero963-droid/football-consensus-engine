"""H1/H2: external anchoring of the ledger head + writer lease with fencing (in-memory AnchorStore).

The PostgreSQL implementation is exercised against a real database in
tests/integration/test_signer_ledger_anchor_postgres.py; this file pins the ledger-side semantics.
"""
import json

import pytest

pytest.importorskip("cryptography")

from signer import anchor as an  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.orders import LedgerError, OrderLedger  # noqa: E402
from sxbet_exchange_kit import MemoryAnchorStore  # noqa: E402

SIGNER_ID = "signer-test-1"
ADDR = "0x" + "aa" * 20
LID = SIGNER_ID + ":" + ADDR
MKT = "0x" + "ab" * 32
NOW = 1_800_000_000


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def store():
    return MemoryAnchorStore()


@pytest.fixture
def clock():
    return Clock()


def path(tmp_path):
    return str(tmp_path / "ledger.jsonl")


def create(tmp_path, store, clock):
    return OrderLedger.create(path(tmp_path), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock, anchor=store)


def reopen(tmp_path, store, clock, **kw):
    return OrderLedger.open(path(tmp_path), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock, anchor=store,
                            **kw)


def authorize(led, n=1):
    return led.authorize(client_order_id=f"order-key-{n:04d}", intent_hash="sha256:" + "1" * 64,
                         order_id="0x" + format(n, "064x"), market_hash=MKT, event_id="L1", stake=1_000_000,
                         expiry=NOW + 3600, approval_nonce=f"nonce-{n:016d}")


def lines(tmp_path):
    return (tmp_path / "ledger.jsonl").read_text().splitlines()


def refused(fn):
    with pytest.raises(LedgerError) as e:
        fn()
    return e.value.code


def test_ledger_id_binds_signer_and_address(tmp_path, store, clock):
    led = create(tmp_path, store, clock)
    assert led.ledger_id == LID


def test_every_append_advances_the_anchor_monotonically(tmp_path, store, clock):
    led = create(tmp_path, store, clock)
    assert store.head(LID).seq == 1
    authorize(led, 1)
    led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    h = store.head(LID)
    assert h.seq == 3 == len(lines(tmp_path)) and h.head_hash == led.head_hash
    assert [x.seq for x in store.heads[LID]] == [1, 2, 3]
    assert {x.fencing_token for x in store.heads[LID]} == {led.fencing_token}
    led.close()
    r = reopen(tmp_path, store, clock)
    assert r.get("order-key-0001").state == od.SIGNED and r.fencing_token > h.fencing_token
    authorize(r, 2)
    assert store.head(LID).seq == 4


def test_truncated_tail_detected(tmp_path, store, clock):
    led = create(tmp_path, store, clock)
    authorize(led, 1)
    led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    led.close()
    keep = lines(tmp_path)[:-1]  # still a valid hash chain: invisible without the anchor
    (tmp_path / "ledger.jsonl").write_text("\n".join(keep) + "\n")
    OrderLedger.open(path(tmp_path), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock).close()  # no anchor: ok
    assert refused(lambda: reopen(tmp_path, store, clock)) == "LEDGER_ANCHOR_AHEAD_OF_LOCAL"


def test_rollback_to_a_fresh_ledger_detected(tmp_path, store, clock):
    led = create(tmp_path, store, clock)
    authorize(led, 1)
    led.close()
    (tmp_path / "ledger.jsonl").unlink()
    assert refused(lambda: create(tmp_path, store, clock)) == "LEDGER_ANCHOR_EXISTS"
    assert not (tmp_path / "ledger.jsonl").exists()
    OrderLedger.create(path(tmp_path), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock).close()
    assert refused(lambda: reopen(tmp_path, store, clock)) == "LEDGER_ANCHOR_AHEAD_OF_LOCAL"


def test_rewritten_history_same_length_detected(tmp_path, store, clock):
    led = create(tmp_path, store, clock)
    authorize(led, 1)
    led.close()
    rows = lines(tmp_path)
    forged = json.loads(rows[-1])
    forged["stake"] = "1"
    rows[-1] = od._canon(forged)  # prev_sha256 unchanged: chain valid, head different
    (tmp_path / "ledger.jsonl").write_text("\n".join(rows) + "\n")
    assert refused(lambda: reopen(tmp_path, store, clock)) == "LEDGER_ANCHOR_HASH_MISMATCH"


def test_local_ahead_refused_unless_owner_acks_the_exact_head(tmp_path, store, clock):
    led = create(tmp_path, store, clock)
    authorize(led, 1)
    store.unreachable = True
    assert refused(lambda: led.transition("order-key-0001", od.SIGNED, reason="SIGNED")) == \
        "LEDGER_ANCHOR_WRITE_FAILED"
    assert led.poisoned and len(lines(tmp_path)) == 3  # fsync'd locally first (write-ahead order)
    assert refused(lambda: authorize(led, 2)) == "LEDGER_POISONED"
    led.close()
    store.unreachable = False
    assert refused(lambda: reopen(tmp_path, store, clock)) == "LEDGER_LOCAL_AHEAD_OF_ANCHOR"
    assert refused(lambda: reopen(tmp_path, store, clock, owner_ack_head="sha256:" + "0" * 64)) == \
        "LEDGER_LOCAL_AHEAD_OF_ANCHOR"
    local_head = od._line_hash(lines(tmp_path)[-1])
    r = reopen(tmp_path, store, clock, owner_ack_head=local_head)
    assert store.head(LID).seq == 3 and store.head(LID).head_hash == local_head
    assert r.get("order-key-0001").state == od.SIGNED


def test_local_ahead_on_a_fork_is_never_acknowledged(tmp_path, store, clock):
    led = create(tmp_path, store, clock)
    authorize(led, 1)
    led.close()
    rows = lines(tmp_path)
    forged = json.loads(rows[-1])
    forged["stake"] = "1"
    rows[-1] = od._canon(forged)
    extra = {"kind": "TRANSITION", "client_order_id": "order-key-0001", "from": "AUTHORIZED", "to": "REJECTED",
             "filled": 0, "reason": "X", "ts": NOW, "prev_sha256": od._line_hash(rows[-1])}
    rows.append(od._canon(extra))
    (tmp_path / "ledger.jsonl").write_text("\n".join(rows) + "\n")
    assert refused(lambda: reopen(tmp_path, store, clock, owner_ack_head=od._line_hash(rows[-1]))) == \
        "LEDGER_ANCHOR_HASH_MISMATCH"


def test_existing_ledger_without_anchor_row_needs_owner_bootstrap(tmp_path, store, clock):
    led = OrderLedger.create(path(tmp_path), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock)
    authorize(led, 1)
    led.close()
    assert refused(lambda: reopen(tmp_path, store, clock)) == "LEDGER_ANCHOR_MISSING"
    r = reopen(tmp_path, store, clock, owner_ack_head=od._line_hash(lines(tmp_path)[-1]))
    assert store.head(LID).seq == 2
    r.close()


def test_anchor_unreachable_at_start_fails_closed(tmp_path, store, clock):
    create(tmp_path, store, clock).close()
    store.unreachable = True
    assert refused(lambda: reopen(tmp_path, store, clock)) == "LEDGER_ANCHOR_UNREACHABLE"
    assert refused(lambda: OrderLedger.create(str(tmp_path / "other.jsonl"), signer_id=SIGNER_ID,
                                              signer_address=ADDR, clock=clock, anchor=store)) == \
        "LEDGER_ANCHOR_UNREACHABLE"
    assert not (tmp_path / "other.jsonl").exists()


def test_second_writer_refused_by_the_lease_not_by_flock(tmp_path, store, clock, monkeypatch):
    led = create(tmp_path, store, clock)

    def no_flock(*a):
        raise AssertionError("flock must not be used when an anchor store is configured")

    monkeypatch.setattr(od.fcntl, "flock", no_flock)
    assert refused(lambda: reopen(tmp_path, store, clock)) == "LEDGER_LOCKED"
    led.close()  # releases the lease
    reopen(tmp_path, store, clock).close()


def test_stale_writer_is_fenced(tmp_path, store, clock):
    led = create(tmp_path, store, clock)
    authorize(led, 1)
    newer = store.take_over(LID)  # old session died; another writer holds a higher token
    assert newer.token > led.fencing_token
    assert refused(lambda: authorize(led, 2)) == "LEDGER_FENCED"
    assert led.poisoned
    with pytest.raises(an.AnchorFenced):
        store.advance(LID, token=led.fencing_token, expected_seq=2, seq=3, head_hash="sha256:" + "1" * 64)


def test_signer_refuses_to_sign_when_the_anchor_write_fails(tmp_path, store, clock):
    pytest.importorskip("eth_account")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from eth_account import Account

    from signer.audit import AuditLog
    from signer.core import KEY_ENV, Signer, SignerConfig, SignerRefused
    from sxbet_signer_kit import ACCOUNT_ID, ESCROW, TOKEN, approve, context, load_limits, make_intent, pub

    acct = Account.create()
    owner = Ed25519PrivateKey.generate()
    led = OrderLedger.create(path(tmp_path), signer_id=SIGNER_ID, signer_address=acct.address, clock=clock,
                             anchor=store)
    cfg = SignerConfig(signer_id=SIGNER_ID, network="mainnet", chain_id=4162, verifying_contract=ESCROW,
                       base_token=TOKEN, odds_ladder_step_size=125, account_id=ACCOUNT_ID)
    s = Signer(cfg, owner_public_key=pub(owner), ledger=led, audit=AuditLog(str(tmp_path / "a.jsonl")),
               risk_config=load_limits(tmp_path, owner), env={KEY_ENV: acct.key.hex()}, clock=clock)
    i = make_intent(acct.address, idempotency_key="order-key-A001")
    out = s.sign_order(i, approve(owner, i), context(i))
    assert out["order_id"] and store.head(led.ledger_id).seq == 3
    i2 = make_intent(acct.address, idempotency_key="order-key-A002")
    store.unreachable = True
    with pytest.raises(SignerRefused) as e:
        s.sign_order(i2, approve(owner, i2), context(i2))
    assert e.value.code == "LEDGER_ANCHOR_WRITE_FAILED"


def test_local_only_mode_unchanged(tmp_path, clock):
    led = OrderLedger.create(path(tmp_path), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock)
    assert led.fencing_token is None and led.anchored is False
    with pytest.raises(LedgerError) as e:
        OrderLedger.open(path(tmp_path), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock)
    assert e.value.code == "LEDGER_LOCKED"  # flock still guards the local-only mode
