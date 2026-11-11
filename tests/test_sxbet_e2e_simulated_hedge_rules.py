"""SIMULATED hedge / close-position ("cashout", OURS) / stop-loss / take-profit / trailing-stop end to end.

Native SX Bet: orders, cancels, heartbeat, reads. NOT native (ours, built on normal orders): hedging,
close-position ("cashout" equivalent), stop-loss, take-profit, trailing stop. The docs list no cashout route
and SimExchange has none. Rules and the hedge simulator only PROPOSE; an order happens only after a fresh owner
approval of the exact parameters through the signer. Exchange: SimExchange (in memory). SIMULATED results.
"""
import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from bridge.sxbet import rules as rl  # noqa: E402
from signer import cancellation as cx  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.monitor import Monitor, MonitorState  # noqa: E402
from signer.rest_adapter import RestRequest  # noqa: E402
from sxbet_e2e_kit import H, ODDS, P, USDC, Stack  # noqa: E402

CLOSE_LABEL = "CLOSE_POSITION_OURS_NOT_NATIVE_SX_BET"


def filled_position(s):
    """Maker 5 USDC on outcome one @40%, fully taken: P1 = +7.5 USDC, P2 = -5 USDC."""
    i, sg, _ = s.place("order-key-H001")
    s.sim.fill_maker_order(sg["order_id"], 5 * USDC)
    assert s.gw.reconcile("order-key-H001")["state"] == od.FILLED
    return sg


def hedge_book(s):
    # Makers on outcome one at 45% (3 USDC) and 44% (10 USDC): a taker backs outcome two at 55% / 56%.
    s.set_book([(True, 45 * P, 3 * USDC), (True, 44 * P, 10 * USDC), (False, 59 * P, 59 * USDC)])


def test_simulated_hedge_executed_matches_simulator_prediction(tmp_path):
    s = Stack(tmp_path)
    filled_position(s)
    hedge_book(s)
    sim = s.snapshot()["hedge"]
    # 2 base units of return stay uncovered (floor rounding): the simulator says PARTIAL, never "complete".
    assert sim["status"] == "PARTIAL_HEDGE_ONLY" and sim["after"]["residual_gap_base_units"] == 2
    assert sim["submitted"] is False and sim["guaranteed_cashout"] is False
    assert [(lg["taker_percentage_odds"], lg["stake_base_units"], lg["return_base_units"]) for lg in sim["legs"]] == [
        (str(55 * P), "3666666", "6666665"), (str(56 * P), "3266667", "5833333")]
    predicted = sim["after"]["pnl_base_units"]
    assert predicted == {"if_outcome_one": 566_667, "if_outcome_two": 566_665}
    writes = len(s.sim.writes())
    params = sim["order_parameters_for_owner_review"]
    assert params["executable"] is False and len(s.sim.writes()) == writes   # simulating sent nothing
    i, sg, r = s.execute_proposal(params, "order-key-H002")                  # owner-approved, signer-checked
    assert s.gw.reconcile("order-key-H002")["state"] == od.FILLED
    pos = s.position()
    assert {k: pos["pnl_base_units"][k] for k in predicted} == predicted     # exchange result == prediction
    # Settlement of both legs: outcome two pays the taker fee on the hedge profit (floored per fill here).
    s.sim.settle(H, 2)
    s.gw.reconcile("order-key-H001")
    s.gw.reconcile("order-key-H002")
    realized = s.ledger.realized_pnl_since(0)
    assert realized == 566_665 - (89_999 + 76_999)
    approx = float(sim["fees"]["pnl_after_projected_fees_base_units"]["if_outcome_two"])
    assert abs(realized - approx) < 3                                        # simulator fee rounding: DOCS_UNVERIFIED


def test_simulated_close_position_is_ours_cancel_then_opposite_outcome_order(tmp_path):
    """'Cashout' equivalent: cancel the resting remainder (owner-approved), then back the other outcome."""
    s = Stack(tmp_path)
    i, sg, _ = s.place("order-key-H010")
    s.sim.fill_maker_order(sg["order_id"], 2 * USDC)
    s.gw.reconcile("order-key-H010")
    assert s.sim.send(RestRequest("POST", "/cashout", (), {})).http_status == 404      # no native route
    hedge_book(s)
    snap = s.snapshot()
    plan = {"label": CLOSE_LABEL, "native": False,
            "step_1_cancel": [o["id"] for o in snap["market_position"]["provider_rows"]["orders"]],
            "step_2_hedge": snap["hedge"]["order_parameters_for_owner_review"]}
    assert plan["step_1_cancel"] == [sg["order_id"]]
    oid = sg["order_id"]
    s.cx.cancel_orders([oid], s.cancel_command(cx.SCOPE_ORDER_IDS, order_ids=[oid]))
    assert s.state("order-key-H010") == od.CANCELLED and s.exposure().total == 2 * USDC
    snap = s.snapshot()                     # re-simulate after the cancel: P1 = 3, P2 = -2 -> back outcome two
    assert snap["market_position"]["pnl_base_units"]["if_outcome_one"] == 3 * USDC
    params = snap["hedge"]["order_parameters_for_owner_review"]
    assert params["betting_outcome_one"] is False
    s.execute_proposal(params, "order-key-H011")
    s.gw.reconcile("order-key-H011")
    after = s.position()["pnl_base_units"]
    assert after["if_outcome_one"] == snap["hedge"]["after"]["pnl_base_units"]["if_outcome_one"]
    assert after["if_outcome_two"] == snap["hedge"]["after"]["pnl_base_units"]["if_outcome_two"]
    assert abs(after["if_outcome_one"] - after["if_outcome_two"]) <= 2      # closed: same P&L either way


def _maker_level(q):
    return [(True, ODDS - q, 10 * USDC), (False, 59 * P, 59 * USDC)]


@pytest.mark.parametrize("rule,cfg,q,reason", [
    ("stop_loss", {"enabled": True, "max_loss_base_units": 500_000}, 65 * P, "STOP_LOSS_HIT"),
    ("take_profit", {"enabled": True, "min_profit_base_units": 1_500_000}, 45 * P, "TAKE_PROFIT_HIT"),
])
def test_simulated_stop_loss_and_take_profit_propose_only_then_owner_executes(tmp_path, rule, cfg, q, reason):
    s = Stack(tmp_path)
    filled_position(s)
    s.set_book(_maker_level(55 * P))
    first = s.snapshot()
    v = rl.evaluate({rule: cfg}, market_position=first["market_position"], market=first["market"], now_unix=int(s.clock()),
                    data_age_seconds=0, taker_percentage_odds=first["taker_percentage_odds"], hedge=first["hedge"])
    assert v["decision"] == rl.NO_ACTION                                    # mtm 0.625 USDC: inside thresholds
    s.set_book(_maker_level(q))
    snap = s.snapshot()
    writes = len(s.sim.writes())
    v = rl.evaluate({rule: cfg}, market_position=snap["market_position"], market=snap["market"], now_unix=int(s.clock()),
                    data_age_seconds=0, taker_percentage_odds=snap["taker_percentage_odds"], hedge=snap["hedge"])
    assert v["decision"] == rl.ORDER_PROPOSAL and v["results"][list(rl.RULES).index(rule)]["reason"] == reason
    p = v["proposal"]
    assert (p["executable"], p["signed"], p["submitted"]) == (False, False, False)
    assert len(s.sim.writes()) == writes and s.ledger.get("order-key-H002") is None   # nothing executed
    s.execute_proposal(p["order_parameters"], "order-key-H002")              # in SIMULATION: owner-approved
    s.gw.reconcile("order-key-H002")
    after = s.position()["pnl_base_units"]
    assert abs(after["if_outcome_one"] - after["if_outcome_two"]) <= 2
    assert after["if_outcome_one"] == snap["hedge"]["after"]["pnl_base_units"]["if_outcome_one"]


def test_simulated_trailing_stop_monitor_persists_peak_across_restart(tmp_path):
    s = Stack(tmp_path)
    filled_position(s)
    state_path = str(tmp_path / "monitor.json")
    cfg = {"trailing_stop": {"enabled": True, "trail_base_units": 1 * USDC, "peak_mark_to_market_base_units": 0}}

    def monitor(state):
        return Monitor(s.ledger, s.adapter, state, clock=s.clock, rules=cfg, evaluate_rules=rl.evaluate,
                       market_snapshot=lambda mh: s.snapshot(mh), lock=s.gw._lock)

    m = monitor(MonitorState.create(state_path, ledger_id=s.ledger.ledger_id))
    writes = len(s.sim.writes())
    for q, mtm in ((55 * P, 625_000), (45 * P, 1_875_000)):                  # the book moves in our favour
        s.set_book(_maker_level(q))
        s.sim.advance(60)
        rep = m.tick()
        assert rep["proposals"] == [] and rep["orders_submitted"] == 0
        assert s.snapshot()["market_position"]["mark_to_market"]["value_base_units"] == mtm
    s.restart()                                                              # signer + monitor process restart
    m = monitor(MonitorState.open(state_path, ledger_id=s.ledger.ledger_id))
    assert m._state.peak(H) == 1_875_000
    s.set_book(_maker_level(53 * P))                                         # mtm 0.875 = peak - trail
    s.sim.advance(60)
    rep = m.tick()
    (p,) = rep["proposals"]
    assert p["triggered_by"] == ["trailing_stop"] and p["status"] == "AWAITING_OWNER_APPROVAL"
    assert (p["executable"], p["signed"], p["submitted"]) == (False, False, False)
    assert len(s.sim.writes()) == writes                                     # the monitor never wrote anything
    assert all(r.method == "GET" for r in s.sim.requests[-20:])
    # A fresh monitor without the persisted peak would NOT have fired at this price.
    fresh = monitor(MonitorState.create(str(tmp_path / "fresh.json"), ledger_id=s.ledger.ledger_id))
    assert fresh.tick()["proposals"] == []
    # Owner-approved execution (SIMULATION) closes the position.
    s.execute_proposal(p["order_parameters"], "order-key-H003")
    s.sim.advance(1)
    m.tick()
    assert s.state("order-key-H003") == od.FILLED                             # reconciled by the monitor
    after = s.position()["pnl_base_units"]
    assert abs(after["if_outcome_one"] - after["if_outcome_two"]) <= 2
