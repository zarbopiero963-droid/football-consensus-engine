"""SIMULATED cancel flows end to end (§2.6: every cancel is owner-approved): single, market, event, all;
unconfirmed; a fill racing the cancel; a late fill after the cancel; heartbeat lapse.

Invariant: a cancel REQUEST never releases exposure. Only exchange READ evidence (INACTIVE + reason, fills)
changes the ledger. SimExchange is in memory; nothing here is native SX Bet evidence.
"""
import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import cancellation as cx  # noqa: E402
from signer import orders as od  # noqa: E402
from sxbet_e2e_kit import EVENT_ID, H, USDC, Stack  # noqa: E402

M2 = "0x" + "cd" * 32


def two_open(s, stake=2 * USDC):
    out = []
    for k in ("order-key-X001", "order-key-X002"):
        i, sg, r = s.place(k, stake=stake)
        assert r["state"] == od.SUBMITTED and s.gw.reconcile(k)["state"] == od.OPEN
        out.append((sg["order_id"], k))
    return out


def test_simulated_cancel_single_confirmed_by_read(tmp_path):
    s = Stack(tmp_path)
    (oid, key), (oid2, key2) = two_open(s)
    rep = s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["orders"][oid]["bucket"] == cx.CANCELLED and s.state(key) == od.CANCELLED
    assert s.state(key2) == od.OPEN and s.exposure().total == 2 * USDC
    (d,) = [r for r in s.sim.writes() if r.method == "DELETE"]
    assert d.path == "/orders-v3" and d.body == {"orders": [{"orderId": oid}]}


def test_simulated_cancelled_bucket_with_stale_read_never_releases(tmp_path):
    s = Stack(tmp_path)
    (oid, key), _ = two_open(s)
    s.sim.read_lag = 5                       # the read replica is 5 s behind the matching engine
    s.sim.advance(1)
    rep = s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["orders"][oid]["bucket"] == cx.CANCELLED and rep["orders"][oid]["state"] == od.OPEN
    assert s.exposure().total == 4 * USDC     # "cancelled" in the reply is not evidence
    s.sim.advance(6)
    assert s.gw.reconcile(key)["state"] == od.CANCELLED and s.exposure().total == 2 * USDC


def test_simulated_unconfirmed_cancel_stays_reserved_until_it_lands(tmp_path):
    s = Stack(tmp_path)
    (oid, key), _ = two_open(s)
    s.sim.inject("DELETE", "/orders-v3", "unconfirmed", land_after=20)
    rep = s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["orders"][oid]["bucket"] == cx.UNCONFIRMED and s.state(key) == od.OPEN
    assert s.exposure().total == 4 * USDC
    s.sim.advance(21)
    assert s.gw.reconcile(key)["state"] == od.CANCELLED and s.exposure().total == 2 * USDC


def test_simulated_fill_racing_the_cancel_keeps_matched_part(tmp_path):
    s = Stack(tmp_path)
    (oid, key), _ = two_open(s)
    # 1.5 USDC of taker stake at 60% consumes 1 USDC of our 2 USDC maker order just before the cancel lands.
    s.sim.inject("DELETE", "/orders-v3", "fill_before_cancel", taker_stake=1_500_000)
    rep = s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    o = s.ledger.get(key)
    assert rep["orders"][oid]["bucket"] == cx.CANCELLED
    assert o.state == od.CANCELLED and o.filled == 1 * USDC
    assert s.exposure().total == 3 * USDC and s.exposure().matched == 1 * USDC
    assert s.position()["pnl_base_units"]["if_outcome_one"] == 1_500_000  # 1 USDC @40% returns 2.5


def test_simulated_full_fill_racing_the_cancel_is_not_cancelled(tmp_path):
    s = Stack(tmp_path)
    (oid, key), _ = two_open(s)
    s.sim.inject("DELETE", "/orders-v3", "fill_before_cancel", taker_stake=3 * USDC)
    rep = s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert rep["orders"][oid]["bucket"] == cx.NOT_CANCELLED and rep["orders"][oid]["reason"] == "NOT_ACTIVE"
    assert s.state(key) == od.FILLED and s.exposure().matched == 2 * USDC


def test_simulated_late_fill_after_cancel_is_recorded_and_counted_again(tmp_path):
    """The cancel is confirmed by a read (released); a fill of that order is reported later, seen only in
    /fills-v3 once the row is purged (404). It is recorded and counted again, then settles. Never dropped."""
    s = Stack(tmp_path)
    (oid, key), _ = two_open(s)
    s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert s.state(key) == od.CANCELLED and s.exposure().total == 2 * USDC
    s.sim.fill_lag = 30
    s.sim.late_fill(oid, 1 * USDC)
    s.sim.advance(1000)                                    # row purged (404); the fill is visible now
    assert s.gw.reconcile(key)["state"] == od.CANCELLED
    o = s.ledger.get(key)
    assert o.filled == 1 * USDC and s.exposure().total == 3 * USDC and s.exposure().matched == 1 * USDC
    s.sim.settle(H, 1)
    s.gw.reconcile(key)
    o = s.ledger.get(key)
    assert o.state == od.SETTLED and o.realized_pnl == 1_500_000 and s.exposure().total == 2 * USDC


@pytest.mark.parametrize("scope", ["MARKET", "EVENT", "ALL"])
def test_simulated_scope_cancel_confirms_only_after_reads(tmp_path, scope):
    s = Stack(tmp_path)
    (oid, key), (oid2, key2) = two_open(s)
    s.sim.add_market(M2, "L777", "C", "D")
    s.sim.add_liquidity(M2, outcome_one=False, maker_odds=59 * 10 ** 18, size=10 * USDC)
    i3, sg3, _ = s.place("order-key-X003", stake=2 * USDC, market=M2)
    s.gw.reconcile("order-key-X003")
    s.sim.inject("DELETE", "/orders-v3/", "async_cancel", land_after=15)   # command accepted, lands later
    kw = {"MARKET": {"market_hash": H}, "EVENT": {"event_id": EVENT_ID}, "ALL": {}}[scope]
    a = s.cancel_command(scope, **kw)
    fn = {"MARKET": lambda: s.cx.cancel_market(H, a), "EVENT": lambda: s.cx.cancel_event(EVENT_ID, a),
          "ALL": lambda: s.cx.cancel_all(a)}[scope]
    rep = fn()
    assert rep["accepted"] is True and all(v["state"] == od.OPEN for v in rep["orders"].values())
    assert s.exposure().total == 6 * USDC                  # command accepted != off the book
    s.sim.advance(16)
    for k in ("order-key-X001", "order-key-X002", "order-key-X003"):
        s.gw.reconcile(k)
    expected_open = 0 if scope == "ALL" else 2 * USDC      # the other market's order survives a market/event cancel
    assert s.exposure().total == expected_open
    assert s.state("order-key-X003") == (od.CANCELLED if scope == "ALL" else od.OPEN)


def test_simulated_cancel_without_owner_approval_sends_nothing(tmp_path):
    s = Stack(tmp_path)
    (oid, key), _ = two_open(s)
    for bad in (None, "owner said cancel in chat", s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=["0x" + "00" * 32])):
        with pytest.raises(cx.CancelRefused):
            s.cx.cancel_orders([oid], bad)
    assert [r for r in s.sim.writes() if r.method == "DELETE"] == [] and s.state(key) == od.OPEN


def test_simulated_heartbeat_lapse_cancels_all_and_reads_confirm(tmp_path):
    """Heartbeat is native (POST /heartbeat/v3) but the repository has NO heartbeat sender: here the TEST plays
    the owner's process. A lapse cancels every order with HEARTBEAT_TIMEOUT; only reads release exposure."""
    from signer.rest_adapter import RestRequest
    s = Stack(tmp_path)
    two_open(s)
    assert s.sim.send(RestRequest("POST", "/heartbeat/v3", (), {"timeoutSeconds": 30})).http_status == 200
    s.sim.advance(31)
    assert s.exposure().total == 4 * USDC
    for k in ("order-key-X001", "order-key-X002"):
        assert s.gw.reconcile(k)["state"] == od.CANCELLED
    assert s.exposure().total == 0


# ---------------------------------------------------------------- S3: CANCEL_AFTER_MATCH (changelog 09-11-2026)
def test_parse_cancel_after_match_is_a_documented_reason():
    from signer import rest_adapter as ra
    oid = "0x" + "ab" * 32
    resp = ra.RestResponse(200, {"status": "success", "data": {"cancelled": [], "unconfirmed": [], "notCancelled": [
        {"orderId": oid, "reason": "CANCEL_AFTER_MATCH", "commandId": "c-1"}]}})
    r = cx.parse_cancel_by_ids(resp, [oid])[oid]
    assert (r.bucket, r.reason, r.command_id) == (cx.NOT_CANCELLED, "CANCEL_AFTER_MATCH", "c-1")


def test_simulated_cancel_after_match_keeps_the_matched_part_and_is_not_repeated(tmp_path):
    """RED on main: the reason was UNDOCUMENTED_REASON. The match is learnt from reads, never assumed."""
    s = Stack(tmp_path)
    (oid, key), _ = two_open(s)
    s.sim.inject("DELETE", "/orders-v3", "cancel_after_match", taker_stake=1_500_000)
    sent_before = len([r for r in s.sim.requests if r.method == "DELETE"]) if hasattr(s.sim, "requests") else None
    rep = s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    row = rep["orders"][oid]
    assert row["bucket"] == cx.NOT_CANCELLED and row["reason"] == "CANCEL_AFTER_MATCH" and row["will_match"] is True
    assert row["reason_meaning"].startswith("WILL_MATCH") and row["command_id"]
    o = s.ledger.get(key)
    assert o.filled == 1 * USDC and s.exposure().matched == 1 * USDC
    if sent_before is not None:
        assert len([r for r in s.sim.requests if r.method == "DELETE"]) == sent_before + 1  # sent once
