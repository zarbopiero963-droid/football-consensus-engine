"""Regression (bug found by the SIMULATED e2e engine / monitor): identical "absent" exchange bytes observed at
different moments must not be swallowed by the evidence dedupe.

`apply_evidence` ignores evidence whose sha256 the order already recorded. That sha256 was computed over the
exchange bytes only, and the "absent" observation (404 by id, 404 by clientOrderId, complete empty fills) is
byte-identical every time. Before the fix:
- after RECONCILED_ABSENT (UNKNOWN -> SIGNED), the same 404s seen after expiry were dropped, so a signed order
  that never reached the exchange stayed SIGNED and fully reserved for ever (never EXPIRED_UNCONFIRMED);
- after a re-send that timed out again (SIGNED -> UNKNOWN), the second RECONCILED_ABSENT was dropped, so the
  order stayed UNKNOWN for ever and could never be re-sent.
Both failed safe (over-reserving) but locked capital permanently. The fix gives these two verdicts an
evidence id bound to their context (the send attempt, resp. "after expiry"), exactly like the existing
CONFIRM_AFTER_WINDOW id; one observation per context is still recorded once.
"""
import pytest

pytest.importorskip("cryptography")

from signer import orders as od  # noqa: E402
from signer.orders import OrderLedger, evidence_from_exchange  # noqa: E402

ADDR, OID, MKT, NOW, STAKE, KEY = "0x" + "aa" * 20, "0x" + "77" * 32, "0x" + "ab" * 32, 1_800_000_000, 5_000_000, "order-key-0001"
NF = {"kind": "NOT_FOUND"}
EMPTY = {"kind": "OK", "fills": [], "complete": True}


class Clock:
    t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    led = OrderLedger.create(str(tmp_path / "l.jsonl"), signer_id="signer-test-1", signer_address=ADDR, clock=clock)
    led.authorize(client_order_id=KEY, intent_hash="sha256:" + "1" * 64, order_id=OID, market_hash=MKT,
                  event_id="L1", stake=STAKE, expiry=NOW + 3600, approval_nonce="nonce-0000000000000001")
    led.transition(KEY, od.SIGNED, reason="SIGNED")
    led.transition(KEY, od.UNKNOWN, reason="SEND_ATTEMPT")
    return led, clock


def absent(led, clock):
    return led.apply_evidence(KEY, evidence_from_exchange(led.get(KEY), lookup=NF, client_lookup=NF, fills=EMPTY,
                                                          now=int(clock())))


def lines(tmp_path):
    return len((tmp_path / "l.jsonl").read_text().splitlines())


def test_absent_then_past_expiry_moves_to_expired_unconfirmed(env):
    led, clock = env
    clock.t += od.ABSENT_SETTLE_SECONDS
    assert absent(led, clock).state == od.SIGNED                    # reconciled absent; never re-sent
    clock.t = NOW + 3600 + od.EXPIRY_GRACE_SECONDS + 1
    o = absent(led, clock)
    assert o.state == od.EXPIRED_UNCONFIRMED and led.exposure().total == STAKE    # still held, as designed
    clock.t += od.EXPIRY_CONFIRM_SECONDS
    assert absent(led, clock).state == od.EXPIRED and led.exposure().total == 0


def test_second_send_attempt_can_be_reconciled_absent_again(env):
    led, clock = env
    clock.t += od.ABSENT_SETTLE_SECONDS
    assert absent(led, clock).state == od.SIGNED
    clock.t += 5
    led.transition(KEY, od.UNKNOWN, reason="SEND_ATTEMPT")         # re-send, timed out again
    clock.t += od.ABSENT_SETTLE_SECONDS
    assert absent(led, clock).state == od.SIGNED                    # was: stuck UNKNOWN for ever


def test_same_absent_observation_in_the_same_context_is_still_recorded_once(env, tmp_path):
    led, clock = env
    clock.t += od.ABSENT_SETTLE_SECONDS
    absent(led, clock)
    n = lines(tmp_path)
    clock.t = NOW + 3600 + od.EXPIRY_GRACE_SECONDS + 1
    absent(led, clock)
    absent(led, clock)                                              # same context: no extra line
    assert lines(tmp_path) == n + 1 and led.get(KEY).state == od.EXPIRED_UNCONFIRMED
