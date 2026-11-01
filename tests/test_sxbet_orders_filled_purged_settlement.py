"""Regression (bug found by the SIMULATED e2e engine): a FILLED order must be able to settle after its row is purged.

Docs (https://docs.sx.bet/openapi.json, GET /orders-v3/{orderId}): an INACTIVE order is returned for about
10-20 minutes, then 404. Settlement happens after the event, i.e. normally hours later, so by then the
order lookup is a 404 and only GET /fills-v3 carries the settlement. Before the fix the NOT_FOUND branch of
`evidence_from_exchange` handled fills-based settlement for EXPIRED_UNCONFIRMED / EXPIRED / CANCELLED but
not for FILLED: a fully filled order stayed FILLED for ever, its matched stake was never released and its
realized P&L never reached the session / daily loss limits.

The fix adds FILLED to that branch. A 404 still teaches nothing else: no release without complete,
SETTLED fills whose sum equals the matched amount.
"""
import pytest

pytest.importorskip("cryptography")

from signer import orders as od  # noqa: E402
from signer.orders import OrderLedger, evidence_from_exchange  # noqa: E402

SIGNER_ID = "signer-test-1"
ADDR = "0x" + "aa" * 20
OID = "0x" + "77" * 32
MKT = "0x" + "ab" * 32
NOW = 1_800_000_000
STAKE = 5_000_000
NOT_FOUND = {"kind": "NOT_FOUND"}


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def fills(*items, complete=True):
    return {"kind": "OK", "fills": list(items), "complete": complete}


def fill(amount, status="SETTLED", ret=None, oid=OID):
    return {"orderId": oid, "fillAmount": str(amount), "status": status,
            "settlement": None if ret is None else {"outcome": 1, "settleReturnAmount": str(ret)}}


@pytest.fixture
def filled(tmp_path):
    clock = Clock()
    led = OrderLedger.create(str(tmp_path / "l.jsonl"), signer_id=SIGNER_ID, signer_address=ADDR, clock=clock)
    led.authorize(client_order_id="order-key-0001", intent_hash="sha256:" + "1" * 64, order_id=OID,
                  market_hash=MKT, event_id="L1", stake=STAKE, expiry=NOW + 3600,
                  approval_nonce="nonce-0000000000000001")
    for to, why in ((od.SIGNED, "SIGNED"), (od.UNKNOWN, "SEND_ATTEMPT"), (od.SUBMITTED, "EXCHANGE_SUBMITTED")):
        led.transition("order-key-0001", to, reason=why)
    ev = evidence_from_exchange(led.get("order-key-0001"), now=NOW, fills=fills(fill(STAKE, status="MATCHED")),
                                lookup={"kind": "FOUND", "order": {"id": OID, "totalBetSize": str(STAKE),
                                                                   "remainingSize": "0", "status": "INACTIVE",
                                                                   "inactiveReason": "FILLED"}})
    assert led.apply_evidence("order-key-0001", ev).state == od.FILLED
    clock.t += 4 * 3600  # hours later: the order row is long purged (404)
    return led, clock


def settle(led, clock, f):
    o = led.get("order-key-0001")
    return led.apply_evidence("order-key-0001", evidence_from_exchange(o, lookup=NOT_FOUND, client_lookup=NOT_FOUND,
                                                                      fills=f, now=int(clock())))


@pytest.mark.parametrize("ret,pnl", [(12_500_000, 7_500_000), (0, -STAKE), (STAKE, 0)])  # win / loss / void
def test_filled_order_settles_from_fills_after_row_purge(filled, ret, pnl):
    led, clock = filled
    o = settle(led, clock, fills(fill(STAKE, ret=ret)))
    assert o.state == od.SETTLED and o.realized_pnl == pnl
    assert led.exposure().total == 0 and led.realized_pnl_since(NOW) == pnl


@pytest.mark.parametrize("bad", [
    fills(fill(STAKE, status="LOCKED")),                       # not settled yet
    fills(fill(STAKE, ret=None)),                              # settlement object missing
    fills(fill(STAKE - 1, ret=0)),                             # sum != matched
    fills(fill(STAKE, ret=0), complete=False),                 # more pages
    fills(fill(STAKE, ret=0, oid="0x" + "99" * 32)),           # another order's fill
    fills(fill(STAKE, ret=0), fill(1, ret=0)),                 # more than the stake
    {"kind": "ERROR"},
])
def test_purged_filled_order_without_complete_settled_fills_learns_nothing(filled, bad):
    led, clock = filled
    o = settle(led, clock, bad)
    assert o.state == od.FILLED and led.exposure().matched == STAKE
