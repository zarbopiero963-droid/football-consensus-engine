"""Regression (review of test/sxbet-e2e-simulated-engine, finding 1): a late fill of a FILLED order is recorded.

A FILLED order may hold filled < stake (INACTIVE FILLED with a dust remainingSize). If its row is then purged
(404) and the complete fills show more than the recorded fill, `_not_found_after_expiry` yields Evidence(FILLED,
total). Before the fix FILLED -> FILLED was not in TRANSITIONS: ILLEGAL_TRANSITION, the late fill was never
recorded and the order could never settle (the fills sum no longer equalled `filled`). The same held for a
FOUND row reporting a larger fill. Fix (intentional, declared): FILLED -> FILLED is allowed, like CANCELLED /
EXPIRED, only for a strictly increasing fill <= stake. It never releases anything: FILLED reserves 0 and holds
the matched amount, which only goes UP.
"""
import pytest

pytest.importorskip("cryptography")

from signer import orders as od  # noqa: E402
from signer.orders import OrderLedger, OrderStateError, evidence_from_exchange  # noqa: E402

ADDR, OID, MKT, NOW, STAKE, KEY = "0x" + "aa" * 20, "0x" + "77" * 32, "0x" + "ab" * 32, 1_800_000_000, 5_000_000, "order-key-0001"
NF = {"kind": "NOT_FOUND"}


class Clock:
    t = NOW

    def __call__(self):
        return self.t


def row(remaining, reason="FILLED", status="INACTIVE"):
    return {"kind": "FOUND", "order": {"id": OID, "totalBetSize": str(STAKE), "remainingSize": str(remaining),
                                       "status": status, "inactiveReason": reason}}


def fills(*amounts, status="MATCHED", ret=None):
    return {"kind": "OK", "complete": True, "fills": [
        {"orderId": OID, "fillAmount": str(a), "status": status,
         "settlement": None if ret is None else {"settleReturnAmount": str(r)}} for a, r in
        zip(amounts, ret or [None] * len(amounts))]}


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    led = OrderLedger.create(str(tmp_path / "l.jsonl"), signer_id="signer-test-1", signer_address=ADDR, clock=clock)
    led.authorize(client_order_id=KEY, intent_hash="sha256:" + "1" * 64, order_id=OID, market_hash=MKT,
                  event_id="L1", stake=STAKE, expiry=NOW + 3600, approval_nonce="nonce-0000000000000001")
    for to, why in ((od.SIGNED, "SIGNED"), (od.UNKNOWN, "SEND_ATTEMPT"), (od.SUBMITTED, "EXCHANGE_SUBMITTED")):
        led.transition(KEY, to, reason=why)
    o = apply(led, clock, row(1), fills(STAKE - 1))                 # INACTIVE FILLED with a 1-unit remainder
    assert (o.state, o.filled) == (od.FILLED, STAKE - 1)
    return led, clock


def apply(led, clock, lookup, f):
    return led.apply_evidence(KEY, evidence_from_exchange(led.get(KEY), lookup=lookup, client_lookup=NF, fills=f,
                                                          now=int(clock())))


def test_purged_filled_order_records_late_fill_without_release(env):
    led, clock = env
    before = led.exposure()
    assert (before.total, before.matched, before.reserved_unmatched) == (STAKE - 1, STAKE - 1, 0)
    clock.t += 3600
    o = apply(led, clock, NF, fills(STAKE - 1, 1))
    assert (o.state, o.filled) == (od.FILLED, STAKE)
    ex = led.exposure()
    assert (ex.total, ex.matched, ex.reserved_unmatched) == (STAKE, STAKE, 0)      # only ever goes up
    o = apply(led, clock, NF, fills(STAKE - 1, 1, status="SETTLED", ret=[12_499_997, 3]))
    assert o.state == od.SETTLED and o.realized_pnl == 12_500_000 - STAKE and led.exposure().total == 0


def test_found_row_with_larger_fill_is_recorded(env):
    led, clock = env
    o = apply(led, clock, row(0), fills(STAKE - 1, 1))
    assert (o.state, o.filled) == (od.FILLED, STAKE)


@pytest.mark.parametrize("f", [fills(STAKE - 1), fills(STAKE - 2), fills(STAKE, 1)])
def test_purged_filled_order_equal_lower_or_excess_fills_learn_nothing(env, f):
    led, clock = env
    clock.t += 3600
    o = apply(led, clock, NF, f)
    assert (o.state, o.filled) == (od.FILLED, STAKE - 1)


def test_filled_self_transition_only_strictly_increasing_within_stake(env):
    led, _ = env
    for bad, code in ((STAKE - 1, "NO_OP_TRANSITION"), (STAKE - 2, "FILLED_AMOUNT_INVALID"),
                      (STAKE + 1, "FILLED_AMOUNT_INVALID")):
        with pytest.raises(OrderStateError) as e:
            led.transition(KEY, od.FILLED, reason="LATE_FILL", filled=bad)
        assert e.value.code == code
    assert od.TRANSITIONS[od.FILLED] == frozenset({od.FILLED, od.SETTLED})
