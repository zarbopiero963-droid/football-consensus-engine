"""SIMULATED end-to-end trading engine (governance §2.6): intent -> owner command -> risk -> sign -> gateway ->
SimExchange -> reconciliation -> positions / P&L -> settlement.

Everything here is SIMULATED: the exchange is tests/sxbet_sim_exchange.py (in memory, RestSender protocol), keys
are throwaway, the risk config and the LIVE_TRADING flag are signed with a throwaway TEST owner key. These tests
verify OUR engine against the documented contract; they are not evidence of native SX Bet behaviour.
"""
import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")


from bridge.sxbet import simulation as bsim  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.command import OwnerCommandAuthorization, text_sha256  # noqa: E402
from signer.core import SignerRefused  # noqa: E402
from signer.intent import intent_hash  # noqa: E402
from sxbet_e2e_kit import H, P, USDC, Stack  # noqa: E402
from sxbet_signer_kit import sign_with_reads  # noqa: E402


def refused(fn):
    with pytest.raises(SignerRefused) as e:
        fn()
    return e.value.code


def maker_filled(s, key="order-key-M001", stake=5 * USDC):
    """GTC maker on outcome one at 40% (taker price 60%), fully taken by a third party."""
    i, sg, r = s.place(key, stake=stake)
    assert r["state"] == od.SUBMITTED and s.gw.reconcile(key)["state"] == od.OPEN
    s.sim.fill_maker_order(sg["order_id"], stake)
    assert s.gw.reconcile(key)["state"] == od.FILLED
    return i, sg


# ---------------------------------------------------------------- P&L: profit / loss / void (maker)
@pytest.mark.parametrize("outcome,pnl,balance", [(1, 7_500_000, 57_500_000), (2, -5_000_000, 45_000_000),
                                                 (0, 0, 50_000_000)])
@pytest.mark.parametrize("purged", [False, True])
def test_simulated_maker_order_profit_loss_void(tmp_path, outcome, pnl, balance, purged):
    s = Stack(tmp_path)
    maker_filled(s)
    assert s.sim.available == 45 * USDC and s.sim.escrowed == 5 * USDC          # matched stake in escrow
    pos = s.position()
    # 5 USDC @ 40% returns 5 * 1e20 / 40e18 = 12.5 USDC stake included.
    assert pos["pnl_base_units"] == {"if_outcome_one": 7_500_000, "if_outcome_two": -5_000_000, "if_void": 0}
    assert pos["worst_case_liability_base_units"] == 5 * USDC
    ex = s.exposure()
    assert (ex.total, ex.matched, ex.reserved_unmatched) == (5 * USDC, 5 * USDC, 0)
    if purged:
        s.sim.advance(4 * 3600)  # settlement hours later: the order row is purged (404), only fills remain
    s.sim.settle(H, outcome)
    assert s.gw.reconcile("order-key-M001")["state"] == od.SETTLED
    o = s.ledger.get("order-key-M001")
    assert o.realized_pnl == pnl and s.exposure().total == 0
    assert s.sim.available == balance and s.sim.escrowed == 0
    assert s.ledger.realized_pnl_since(0) == pnl
    roll = s.rollup()
    assert roll["totals"]["realized_pnl_base_units"] == pnl                     # positions agree with the ledger


def test_simulated_taker_order_profit_pays_taker_fee_on_profit_only(tmp_path):
    s = Stack(tmp_path)
    # IOC backing outcome one; best taker price 41% (third-party maker on outcome two at 59%).
    i, sg, r = s.place("order-key-T001", stake=4_100_000, odds=41 * P, tif="IOC")
    assert s.gw.reconcile("order-key-T001")["state"] == od.FILLED
    pos = s.position()
    assert pos["pnl_base_units"]["if_outcome_one"] == 5_900_000 and pos["pnl_base_units"]["if_outcome_two"] == -4_100_000
    assert pos["projected_payout_fee_base_units"]["if_outcome_one"] == "177000"   # 3% of 5.9 USDC
    s.sim.settle(H, 1)
    s.gw.reconcile("order-key-T001")
    o = s.ledger.get("order-key-T001")
    assert o.state == od.SETTLED and o.realized_pnl == 5_900_000 - 177_000
    assert s.sim.available == 50 * USDC + 5_723_000  # + profit net of the 3% taker fee
    (f,) = s.all_fills()
    assert f["settlement"]["settleFeeAmount"] == "177000" and f["isMaker"] is False


def test_simulated_taker_order_loss_has_no_fee(tmp_path):
    s = Stack(tmp_path)
    s.place("order-key-T002", stake=4_100_000, odds=41 * P, tif="FOK")
    assert s.gw.reconcile("order-key-T002")["state"] == od.FILLED
    s.sim.settle(H, 2)
    s.gw.reconcile("order-key-T002")
    assert s.ledger.get("order-key-T002").realized_pnl == -4_100_000 and s.sim.available == 50 * USDC - 4_100_000


def test_simulated_realized_loss_feeds_the_session_loss_limit(tmp_path):
    # Intentional change (risk v2): no session limit any more; the daily loss limit counts the realised loss of
    # the UTC day plus the worst case of every open order plus the new order.
    s = Stack(tmp_path, limits={"max_daily_loss_base_units": 8 * USDC})
    maker_filled(s)
    s.sim.settle(H, 2)                                  # lost 5 USDC
    s.gw.reconcile("order-key-M001")
    assert s.ledger.realized_pnl_since(0) == -5 * USDC
    s.sim.add_market("0x" + "cd" * 32, "L999", "A", "B")
    s.sim.add_liquidity("0x" + "cd" * 32, outcome_one=False, maker_odds=59 * P, size=59 * USDC)
    i = s.intent("order-key-M002", stake=4 * USDC, market="0x" + "cd" * 32)
    assert refused(lambda: s.sign(i)) == "DAILY_LOSS_LIMIT"           # 5 lost + 4 at risk > 8
    s.sim.advance(60)
    s.restart()                                                        # restart: the UTC day still counts
    assert refused(lambda: s.sign(i)) == "DAILY_LOSS_LIMIT"
    ok = s.intent("order-key-M003", stake=3 * USDC, market="0x" + "cd" * 32)
    assert s.sign(ok)["cached"] is False                               # 5 + 3 <= 8


# ---------------------------------------------------------------- partial fill: residual exposure
def test_simulated_partial_fill_residual_exposure_then_expiry_and_settlement(tmp_path):
    s = Stack(tmp_path)
    i, sg, _ = s.place("order-key-P001")
    s.sim.fill_maker_order(sg["order_id"], 2 * USDC)                  # a taker takes 2 of our 5 USDC
    assert s.gw.reconcile("order-key-P001")["state"] == od.PARTIALLY_FILLED
    ex = s.exposure()
    assert (ex.total, ex.matched, ex.reserved_unmatched, ex.open_orders) == (5 * USDC, 2 * USDC, 3 * USDC, 1)
    pos = s.position()
    assert pos["pnl_base_units"] == {"if_outcome_one": 3 * USDC, "if_outcome_two": -2 * USDC, "if_void": 0}
    assert pos["resting_orders"]["remaining_base_units"] == {"outcome_one": 3 * USDC, "outcome_two": 0}
    assert pos["resting_orders"]["pnl_if_all_resting_filled_base_units"] == {"if_outcome_one": 7_500_000,
                                                                              "if_outcome_two": -5 * USDC}
    s.sim.advance(3601)                                               # GTC expires on the exchange
    assert s.gw.reconcile("order-key-P001")["state"] == od.EXPIRED
    ex = s.exposure()
    assert (ex.total, ex.matched, ex.reserved_unmatched, ex.open_orders) == (2 * USDC, 2 * USDC, 0, 0)
    s.sim.settle(H, 1)
    s.gw.reconcile("order-key-P001")
    o = s.ledger.get("order-key-P001")
    assert o.state == od.SETTLED and o.realized_pnl == 3 * USDC and s.exposure().total == 0


# ---------------------------------------------------------------- refusals before anything reaches the exchange
def test_simulated_odds_moved_beyond_slippage_refused_and_approval_not_burnt(tmp_path):
    """Intentional change (v2): no slippage tolerance; the owner's minimum odds ARE the signed limit price. A
    taker (FOK) order whose limit the book no longer reaches is refused without burning the command."""
    s = Stack(tmp_path)
    i = s.intent("order-key-R001", odds=41 * P, tif="FOK")                # owner: "a quota minima 2,439..."
    command = s.command(i, s.context(i))
    s.sim.clear_liquidity(H)
    s.sim.add_liquidity(H, outcome_one=False, maker_odds=57 * P, size=57 * USDC)  # taker price now 43% > 41%
    assert refused(lambda: sign_with_reads(s.signer, i, command, s.context(i))) == "INSUFFICIENT_LIQUIDITY"
    assert not s.ledger.command_consumed(command.command_id) and s.sim.writes() == []
    worse = s.intent("order-key-R001", odds=43 * P, tif="FOK")         # the model "helpfully" lowers the odds
    assert refused(lambda: sign_with_reads(s.signer, worse, command, s.context(worse))) == "COMMAND_MISMATCH_ODDS"
    s.sim.clear_liquidity(H)
    s.sim.add_liquidity(H, outcome_one=False, maker_odds=59 * P, size=59 * USDC)  # back within the limit
    assert sign_with_reads(s.signer, i, command, s.context(i))["cached"] is False


@pytest.mark.parametrize("tif", ["IOC", "FOK"])
def test_simulated_insufficient_liquidity_for_taker_refused(tmp_path, tif):
    s = Stack(tmp_path)
    s.sim.clear_liquidity(H)
    s.sim.add_liquidity(H, outcome_one=False, maker_odds=59 * P, size=2_950_000)  # 2.05 USDC of taker capacity
    i = s.intent("order-key-R002", stake=5 * USDC, odds=41 * P, tif=tif)
    assert s.context(i)["quote"]["available_liquidity_base_units"] == "2050000"
    assert refused(lambda: s.sign(i)) == "INSUFFICIENT_LIQUIDITY" and s.sim.writes() == []


def test_simulated_liquidity_gone_before_arrival_ioc_partial_fok_nothing(tmp_path):
    """Risk passed on the quote, the book thinned before the order arrived: the exchange decides, the ledger follows."""
    s = Stack(tmp_path)
    ioc, fok = s.intent("order-key-R003", stake=4_100_000, odds=41 * P, tif="IOC"), \
        s.intent("order-key-R004", stake=4_100_000, odds=41 * P, tif="FOK")
    sig_ioc, sig_fok = s.sign(ioc), s.sign(fok)
    s.sim.clear_liquidity(H)
    s.sim.add_liquidity(H, outcome_one=False, maker_odds=59 * P, size=2_950_000)  # 2.05 USDC capacity left
    s.gw.submit(sig_fok, fok)
    assert s.gw.reconcile("order-key-R004")["state"] == od.CANCELLED          # INACTIVE NO_LIQUIDITY, no fill
    s.gw.submit(sig_ioc, ioc)
    assert s.gw.reconcile("order-key-R003")["state"] == od.CANCELLED
    o = s.ledger.get("order-key-R003")
    assert o.filled == 2_050_000 and s.ledger.get("order-key-R004").filled == 0
    assert s.exposure().total == 2_050_000 and s.position()["pnl_base_units"]["if_outcome_one"] == 2_950_000


def test_simulated_insufficient_balance_refused_by_risk(tmp_path):
    s = Stack(tmp_path, balance=3 * USDC)
    assert refused(lambda: s.sign(s.intent("order-key-R005"))) == "INSUFFICIENT_BALANCE" and s.sim.writes() == []


def test_simulated_balance_drop_after_risk_is_an_exchange_failed_row(tmp_path):
    s = Stack(tmp_path)
    i = s.intent("order-key-R006")
    sg = s.sign(i)
    s.sim.available = 1 * USDC                    # funds moved away (e.g. a bet placed on the web) after the check
    assert s.gw.submit(sg, i)["state"] == od.REJECTED and s.sim.accepted_count() == 0
    # The signature left the signer: the stake stays reserved until expiry + grace, then 0.
    assert s.exposure().total == 5 * USDC
    s.sim.advance(3600 + od.EXPIRY_GRACE_SECONDS + 1)
    assert s.exposure().total == 0


def test_simulated_stake_above_max_refused(tmp_path):
    s = Stack(tmp_path)
    assert refused(lambda: s.sign(s.intent("order-key-R007", stake=11 * USDC))) == "STAKE_ABOVE_LIMIT"


def test_simulated_stake_below_exchange_minimum(tmp_path):
    """The bridge preparation refuses it; a hand-made intent that skips the bridge is now refused by the SIGNER.

    Intentional change (risk v2): the previous GAP is closed. The signer reads orderSizeMinimumBaseUnits from the
    runtime metadata in its risk context (never hardcoded) and refuses before anything is signed or reserved;
    before, it signed and the exchange answered 400 -> REJECTED with the stake reserved until expiry + grace.
    """
    s = Stack(tmp_path)
    with pytest.raises(bsim.IntentError) as e:
        s.intent("order-key-R008", stake=500_000)
    assert str(e.value) == "STAKE_BELOW_MINIMUM"
    i = dict(s.intent("order-key-R009"), total_bet_size="500000")
    assert refused(lambda: s.sign(i)) == "STAKE_BELOW_EXCHANGE_MINIMUM"
    assert s.sim.writes() == [] and s.exposure().total == 0 and s.ledger.orders() == []


@pytest.mark.parametrize("odds", [5 * P, 95 * P])
def test_simulated_odds_outside_owner_band_refused(tmp_path, odds):
    s = Stack(tmp_path)
    assert refused(lambda: s.sign(s.intent("order-key-R010", odds=odds))) == "ODDS_OUTSIDE_OWNER_BAND"
    assert s.sim.writes() == []


# ---------------------------------------------------------------- idempotency, replay, no approval
def test_simulated_same_client_order_id_twice_is_one_exchange_order(tmp_path):
    s = Stack(tmp_path)
    i = s.intent("order-key-I001")
    ctx = s.context(i)
    first = sign_with_reads(s.signer, i, s.command(i, ctx), ctx)
    again = sign_with_reads(s.signer, i, s.command(i, ctx), ctx)              # second request: cached, no new signature
    assert again["cached"] is True and again["order"] == first["order"]
    assert s.gw.submit(first, i)["sent"] is True
    assert s.gw.submit(again, i)["sent"] is False
    assert s.sim.accepted_count("order-key-I001") == 1
    other = s.intent("order-key-I001", stake=6 * USDC)
    assert refused(lambda: s.sign(other)) == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INTENT"


def test_simulated_approval_replay_refused(tmp_path):
    s = Stack(tmp_path)
    i1, i2 = s.intent("order-key-A001"), s.intent("order-key-A002")
    ctx = s.context(i1)
    a1 = s.command(i1, ctx)
    sg = sign_with_reads(s.signer, i1, a1, ctx)
    s.gw.submit(sg, i1)
    # The same command (id AND text) for a second order: the id is consumed (one command = one operation).
    assert refused(lambda: sign_with_reads(s.signer, i2, a1, s.context(i2))) == "COMMAND_ID_ALREADY_CONSUMED"
    reused_id = s.command(i2, s.context(i2), command_id=a1.command_id)   # MCP re-sent a consumed id
    assert refused(lambda: sign_with_reads(s.signer, i2, reused_id, s.context(i2))) == "COMMAND_ID_ALREADY_CONSUMED"
    same_text = s.command(i2, s.context(i2), text=a1.command_text)       # new id, the very same command text
    assert refused(lambda: sign_with_reads(s.signer, i2, same_text, s.context(i2))) == "COMMAND_REPLAYED"
    s.restart()
    assert refused(lambda: sign_with_reads(s.signer, i1, a1, s.context(i1))) in (
        "IDEMPOTENCY_KEY_SIGNED_BEFORE_RESTART_RECONCILE_FIRST", "COMMAND_ID_ALREADY_CONSUMED")
    assert refused(lambda: sign_with_reads(s.signer, i2, reused_id, s.context(i2))) == "COMMAND_ID_ALREADY_CONSUMED"
    assert s.sim.accepted_count() == 1


def test_simulated_nothing_executes_without_owner_approval(tmp_path):
    """Intentional change: the authorization is the owner's literal command (no Ed25519 per order). A bare chat
    string, a record whose text does not hash to the declared hash, or a text that is not a complete command
    never signs."""
    s = Stack(tmp_path)
    i = s.intent("order-key-N001")
    ctx = s.context(i)
    assert refused(lambda: sign_with_reads(s.signer, i, None, ctx)) == "COMMAND_MISSING"
    assert refused(lambda: sign_with_reads(s.signer, i, "approved by the owner in chat: yes", ctx)) == "COMMAND_MISSING"
    good = s.command(i, ctx)
    bad = OwnerCommandAuthorization(good.command_id, good.command_text, text_sha256("something else"),
                                    good.received_at, good.client_ref_sha256)
    assert refused(lambda: sign_with_reads(s.signer, i, bad, ctx)) == "COMMAND_TEXT_HASH_MISMATCH"
    vague = s.command(i, ctx, text="Sì, vai pure con la scommessa che mi hai proposto")
    assert refused(lambda: sign_with_reads(s.signer, i, vague, ctx)) == "COMMAND_UNPARSEABLE"
    # An unsigned order cannot be submitted either: the gateway only forwards ledger-authorized signed orders.
    fake = {"order_id": "0x" + "00" * 32, "intent_hash": intent_hash(i), "order": {}}
    with pytest.raises(Exception):
        s.gw.submit(fake, i)
    assert s.sim.writes() == [] and s.ledger.orders() == []
