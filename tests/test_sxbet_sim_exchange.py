"""Self-tests of the SIMULATED exchange (tests/sxbet_sim_exchange.py) against the documented contract.

These pin the simulator's fidelity (frames, capacity, returns, lifecycle, batches, pagination, settlement) so the
e2e tests that rely on it are meaningful. They test the SIMULATOR, not SX Bet.
"""
import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from signer import orders as od  # noqa: E402
from signer import rest_adapter as ra  # noqa: E402
from sxbet_e2e_kit import H, ODDS, P, USDC, Stack, get  # noqa: E402
from sxbet_sim_exchange import SIM_ASSUMPTIONS, SIM_LABEL, SimExchange  # noqa: E402


def test_sim_is_labelled_and_documents_its_assumptions():
    assert SIM_LABEL == "SIMULATED_NOT_NATIVE_EVIDENCE" and len(SIM_ASSUMPTIONS) >= 5
    assert hasattr(SimExchange, "send")              # implements the RestSender protocol (tests only)


def test_sim_book_frames_capacity_and_best_odds(tmp_path):
    s = Stack(tmp_path)
    maker = s.book()
    assert maker["outcomeTwo"] == [{"percentageOdds": str(59 * P), "size": str(59 * USDC)}]
    taker = get(s.sim, "/orderbook-v3/snapshot/market", marketHash=H, showTakerPerspective="true")
    # taker frame: q = 1e20 - p, capacity = size * (1e20 - p) / p
    assert taker["outcomeOne"] == [{"percentageOdds": str(41 * P), "size": str(41 * USDC)}]
    assert taker["outcomeTwo"] == [{"percentageOdds": str(65 * P), "size": str(65 * USDC)}]
    best = get(s.sim, "/orders-v3/odds/best", marketHashes=H)["bestOdds"][0]
    assert best["outcomeOne"]["percentageOdds"] == str(35 * P)


def test_sim_taker_return_and_maker_consumption(tmp_path):
    s = Stack(tmp_path)
    s.place("order-key-Z001", stake=4_100_000, odds=41 * P, tif="IOC")
    (f,) = s.all_fills()
    assert (f["fillAmount"], f["returnAmount"], f["fillOdds"], f["isMaker"]) == ("4100000", "10000000", str(41 * P),
                                                                                  False)
    assert s.book()["outcomeTwo"][0]["size"] == str(59 * USDC - 5_900_000)  # pot - taker stake = maker stake
    assert (s.sim.available, s.sim.escrowed) == (50 * USDC - 4_100_000, 4_100_000)


def test_sim_lifecycle_pending_active_inactive_and_purge(tmp_path):
    s = Stack(tmp_path)
    i, sg, _ = s.place("order-key-Z002")
    row = get(s.sim, "/orders-v3/" + sg["order_id"])["order"]
    assert row["status"] == "ACTIVE" and row["remainingSize"] == "5000000"
    assert get(s.sim, "/orders-v3/client/order-key-Z002")["order"]["id"] == sg["order_id"]
    s.sim.advance(3601)
    row = get(s.sim, "/orders-v3/" + sg["order_id"])["order"]
    assert (row["status"], row["inactiveReason"]) == ("INACTIVE", "EXPIRED")
    s.sim.advance(901)
    assert s.sim.send(ra.RestRequest("GET", "/orders-v3/" + sg["order_id"], (), None)).http_status == 404


def test_sim_post_only_would_cross(tmp_path):
    s = Stack(tmp_path, options=ra.SubmitOptions(post_only=True))
    i, sg, r = s.place("order-key-Z003", stake=2 * USDC, odds=41 * P)   # GTC at 41% would cross the 59% maker
    assert s.gw.reconcile("order-key-Z003")["state"] == od.CANCELLED
    assert get(s.sim, "/orders-v3/" + sg["order_id"])["order"]["inactiveReason"] == "POST_ONLY_WOULD_CROSS"
    assert s.all_fills() == []


def test_sim_self_match_prevention(tmp_path):
    """Our IOC on outcome two crosses only OUR resting maker: the engine never matches us with ourselves.
    Note: the book snapshot includes own orders, so the risk quote saw 'liquidity' that is not usable."""
    s = Stack(tmp_path)
    s.sim.clear_liquidity(H)
    s.sim.add_liquidity(H, outcome_one=False, maker_odds=59 * P, size=59 * USDC)   # quote for our side-1 maker
    i, sg, _ = s.place("order-key-Z004", stake=2 * USDC, odds=40 * P)              # rests: taker price 60%
    i2, sg2, _ = s.place("order-key-Z014", outcome_one=False, stake=3 * USDC, odds=60 * P, tif="IOC")
    assert s.gw.reconcile("order-key-Z014")["state"] == od.CANCELLED and s.ledger.get("order-key-Z014").filled == 0
    assert s.sim.orders[sg["order_id"]].remaining == 2 * USDC and s.all_fills() == []


def test_sim_batch_is_not_atomic_and_client_id_collision_fails_whole_batch(tmp_path):
    s = Stack(tmp_path)
    m2 = "0x" + "cd" * 32
    s.sim.add_market(m2, "L5", "A", "B")
    s.sim.add_liquidity(m2, outcome_one=False, maker_odds=59 * P, size=10 * USDC)
    a, b = s.intent("order-key-Z005"), s.intent("order-key-Z006", market=m2)
    sa, sb = s.sign(a), s.sign(b)
    s.sim.close_market(m2)                              # second order: market no longer ACTIVE
    out = s.gw.submit_batch([(sa, a), (sb, b)])
    assert [r["state"] for r in out] == [od.SUBMITTED, od.REJECTED]
    (post,) = [r for r in s.sim.writes() if r.method == "POST"]
    resp = s.sim.send(ra.RestRequest("POST", "/orders-v3", (), post.body))
    assert resp.http_status == 400 and resp.body["message"] == "DUPLICATE_CLIENT_ORDER_ID"


def test_sim_fills_pagination_through_the_official_adapter(tmp_path):
    s = Stack(tmp_path)
    i, sg, _ = s.place("order-key-Z007")
    for _ in range(150):
        s.sim.take(H, outcome_one=False, taker_stake=3, max_taker_odds=60 * P)
    full = s.adapter.get_fills(sg["order_id"])
    assert full["complete"] is True and len(full["fills"]) == 150
    assert sum(1 for r in s.sim.requests if r.path == "/fills-v3") == 2
    capped = ra.OfficialRestAdapter(s.sim, limits=s.adapter.limits, max_fill_pages=1).get_fills(sg["order_id"])
    assert capped["complete"] is False and len(capped["fills"]) == 100
    page = get(s.sim, "/fills-v3", orderId=sg["order_id"], perPage=100, nextKey=100)
    assert "nextKey" not in page                                    # absent, not null, on the last page


@pytest.mark.parametrize("outcome,back,fee", [(1, 12_275_000, 225_000), (2, 0, 0), (0, 5 * USDC, 0)])
def test_sim_settlement_fee_on_profit_only(tmp_path, outcome, back, fee):
    s = Stack(tmp_path)
    s.place("order-key-Z008", stake=5 * USDC, odds=41 * P, tif="IOC")  # taker, 3% takerPayoutFee
    f = s.all_fills()[0]
    assert f["returnAmount"] == str(5 * USDC * ODDS // (41 * P))
    s.sim.settle(H, outcome)
    st = s.all_fills()[0]["settlement"]
    ret = 5 * USDC * ODDS // (41 * P)
    if outcome == 1:
        fee = (ret - 5 * USDC) * 3 // 100
        back = ret - fee
    assert (st["outcome"], st["settleReturnAmount"], st["settleFeeAmount"]) == (outcome, str(back), str(fee))


def test_sim_rejects_bad_signature_and_has_no_cashout_route(tmp_path):
    s = Stack(tmp_path)
    i = s.intent("order-key-Z009")
    sg = s.sign(i)
    req = s.adapter.build_create({"orders": [dict(sg["order"], totalBetSize="6000000")]})
    assert s.sim.send(req).body["message"] == "BAD_SIGNATURE" and s.sim.accepted_count() == 0
    for path in ("/cashout", "/orders-v3/cashout", "/positions-v3/close"):
        assert s.sim.send(ra.RestRequest("POST", path, (), {})).http_status == 404
