"""SX Bet hedge simulator (bridge/sxbet/hedging.py): pure, offline."""
import pytest

from bridge.sxbet import hedging as hg

M = "0x" + "11" * 32
P = 10 ** 18  # one implied-probability point on the 1e20 scale
FEES = {"refundFee": None, "makerPayoutFee": "0.01", "takerPayoutFee": "0.02",
        "makerParlayPayoutFee": None, "takerParlayPayoutFee": None}


def position(p1=300, p2=-200, f1="3", f2="0", conflict=False):
    return {"market_hash": M, "pnl_base_units": {"if_outcome_one": p1, "if_outcome_two": p2, "if_void": 0},
            "projected_payout_fee_base_units": {"if_outcome_one": f1, "if_outcome_two": f2},
            "data_conflict": conflict}


# Maker frame: makers resting on outcome one at 60% (size 150) and 55% (size 1000) offer outcome two to a taker
# at 40% (capacity 150*40/60 = 100) and 45% (capacity 1000*45/55 = 818).
BOOK = {"marketHash": M, "outcomeOne": [{"percentageOdds": str(60 * P), "size": "150"},
                                        {"percentageOdds": str(55 * P), "size": "1000"}],
        "outcomeTwo": [{"percentageOdds": str(30 * P), "size": "10"}], "version": "001"}
MARKET = {"marketHash": M, "status": "ACTIVE"}


def run(**kw):
    args = dict(market_position=position(), book=BOOK, market=MARKET, fees=FEES, data_age_seconds=1.0,
                max_data_age_seconds=10, min_order_base_units=1)
    args.update(kw)
    return hg.simulate_hedge(**args)


def test_full_hedge_walks_levels_and_equalizes_outcomes():
    out = run(max_slippage_percentage_odds=5 * P)
    assert out["status"] == "HEDGE_AVAILABLE" and out["hedge_outcome"] == "outcome_two"
    assert [(l["taker_percentage_odds"], l["stake_base_units"], l["return_base_units"]) for l in out["legs"]] == [
        (str(40 * P), "100", "250"), (str(45 * P), "112", "248")]
    assert out["hedge_stake_base_units"] == "212" and out["hedge_return_base_units"] == "498"
    # P1' = 300 - 212, P2' = -200 + (498 - 212)
    assert out["after"]["pnl_base_units"] == {"if_outcome_one": 88, "if_outcome_two": 86}
    assert out["after"]["residual_gap_base_units"] == 2 and out["liquidity"]["covered"] is True
    assert out["worst_acceptable_taker_percentage_odds"] == str(45 * P)
    fees = out["fees"]
    assert fees["hedge_taker_payout_fee_if_hedge_wins_base_units"] == "5.72"  # 2% of profit 286
    assert fees["pnl_after_projected_fees_base_units"] == {"if_outcome_one": "85", "if_outcome_two": "80.28"}
    assert out["guaranteed_cashout"] is False and out["signed"] is False and out["submitted"] is False
    assert "NOT A GUARANTEED CASHOUT" in out["warning"]
    review = out["order_parameters_for_owner_review"]
    assert review["executable"] is False and review["betting_outcome_one"] is False
    assert review["stake_base_units"] == "212" and review["percentage_odds_limit"] == str(45 * P)
    assert out["provider_book"] is BOOK and out["label"] == "DERIVED"


def test_zero_slippage_gives_partial_hedge_only():
    out = run(max_slippage_percentage_odds=0)
    assert out["status"] == "PARTIAL_HEDGE_ONLY" and len(out["legs"]) == 1
    assert out["liquidity"]["covered"] is False and out["residual_risk"]["unhedged_return_base_units"] == "250"
    assert out["after"]["pnl_base_units"] == {"if_outcome_one": 200, "if_outcome_two": -50}


def test_partial_fraction_and_reduce_modes():
    half = run(mode="PARTIAL", fraction="0.5")
    assert half["status"] == "HEDGE_AVAILABLE" and half["hedge_return_base_units"] == "250"
    assert half["after"]["pnl_base_units"] == {"if_outcome_one": 200, "if_outcome_two": -50}
    red = run(mode="REDUCE", hedge_stake_base_units=50)
    assert red["status"] == "HEDGE_AVAILABLE" and red["hedge_stake_base_units"] == "50"
    assert red["after"]["pnl_base_units"] == {"if_outcome_one": 250, "if_outcome_two": -125}
    # REDUCE is capped at the full hedge: never over-hedges into the other outcome
    big = run(mode="REDUCE", hedge_stake_base_units=10 ** 9, max_slippage_percentage_odds=5 * P)
    assert big["after"]["pnl_base_units"]["if_outcome_one"] >= big["after"]["pnl_base_units"]["if_outcome_two"]


def test_hedge_on_outcome_one_uses_makers_resting_on_outcome_two():
    out = run(market_position=position(p1=-50, p2=100))
    assert out["hedge_outcome"] == "outcome_one" and out["legs"][0]["taker_percentage_odds"] == str(70 * P)


def test_taker_perspective_book_gives_the_same_result():
    taker = {"marketHash": M, "outcomeTwo": [{"percentageOdds": str(40 * P), "size": "100"},
                                             {"percentageOdds": str(45 * P), "size": "818"}], "outcomeOne": []}
    a = run(max_slippage_percentage_odds=5 * P)
    b = run(max_slippage_percentage_odds=5 * P, book=taker, book_perspective="TAKER")
    assert a["legs"][0]["stake_base_units"] == b["legs"][0]["stake_base_units"]
    assert a["after"] == b["after"]


def test_own_resting_orders_are_never_matched_against():
    own = [{"id": "0x" + "b1" * 32, "marketHash": M, "isBettingOutcomeOne": True, "percentageOdds": str(60 * P),
            "remainingSize": "150", "status": "ACTIVE"}]
    out = run(own_resting_orders=own, max_slippage_percentage_odds=5 * P)
    assert out["legs"][0]["taker_percentage_odds"] == str(45 * P)


def test_ladder_rounding_of_worst_acceptable_odds():
    out = run(max_slippage_percentage_odds=3 * 10 ** 17, odds_ladder_step_size=125)  # 40.3% -> 40.25%
    assert out["worst_acceptable_taker_percentage_odds"] == str(4025 * 10 ** 16)
    assert out["worst_acceptable_rounding"] == "ROUNDED_DOWN_TO_LADDER"


@pytest.mark.parametrize("kw,reason", [
    ({"market": {"marketHash": M, "status": "INACTIVE"}}, "MARKET_NOT_ACTIVE"),
    ({"market": None}, "MARKET_STATUS_UNKNOWN"),
    ({"book": None}, "ORDER_BOOK_MISSING"),
    ({"book": {"marketHash": "0x" + "99" * 32, "outcomeOne": []}}, "BOOK_MARKET_MISMATCH"),
    ({"book": {"marketHash": M, "outcomeOne": [], "outcomeTwo": []}}, "NO_LIQUIDITY"),
    ({"book": {"marketHash": M, "outcomeTwo": []}}, "ORDER_BOOK_SIDE_MISSING"),
    ({"book": {"marketHash": M, "outcomeOne": [{"percentageOdds": "x", "size": "1"}]}}, "BOOK_MALFORMED"),
    ({"market_position": None}, "POSITION_DATA_MISSING"),
    ({"market_position": {"market_hash": M, "pnl_base_units": {"if_outcome_one": None, "if_outcome_two": 1}}},
     "POSITION_PNL_MISSING"),
    ({"min_order_base_units": 10 ** 6}, "HEDGE_STAKE_BELOW_MIN_ORDER"),
])
def test_hedge_unavailable(kw, reason):
    out = run(**kw)
    assert out["status"] == "HEDGE_UNAVAILABLE" and out["reason"] == reason
    assert out["guaranteed_cashout"] is False and out["submitted"] is False


def test_own_order_at_best_level_moves_the_real_best_price():
    own = [{"marketHash": M, "isBettingOutcomeOne": True, "percentageOdds": str(60 * P), "remainingSize": "150",
            "status": "ACTIVE"}]
    book = {"marketHash": M, "outcomeOne": [{"percentageOdds": str(60 * P), "size": "150"},
                                            {"percentageOdds": str(20 * P), "size": "1000"}]}
    out = run(book=book, own_resting_orders=own, max_slippage_percentage_odds=0)
    # the 60% level is entirely our own order: the best price we can really get is 1e20 - 20% = 80%
    assert out["status"] == "HEDGE_AVAILABLE" and out["best_taker_percentage_odds"] == str(80 * P)
    assert out["legs"][0]["stake_base_units"] == "400"


def test_gap_too_small_for_one_base_unit_is_unavailable():
    out = run(market_position=position(p1=1, p2=0))
    assert out["status"] == "HEDGE_UNAVAILABLE" and out["reason"] == "GAP_BELOW_ONE_BASE_UNIT_OF_STAKE"


@pytest.mark.parametrize("kw,reason", [
    ({"data_age_seconds": 30.0}, "STALE_DATA"),
    ({"data_age_seconds": None}, "STALE_DATA"),
    ({"fees": None}, "FEE_UNVERIFIABLE_FEES_NOT_SUPPLIED"),
    ({"fees": {"takerPayoutFee": "bad"}}, "FEE_UNVERIFIABLE_FEES_MALFORMED"),
    ({"market_position": position(conflict=True)}, "CONFLICTING_POSITION_DATA"),
    ({"market_position": position(f1=None)}, "POSITION_FEE_UNVERIFIABLE"),
    ({"min_order_base_units": None}, "MIN_ORDER_SIZE_UNCHECKED"),
])
def test_hedge_uncertain(kw, reason):
    out = run(max_slippage_percentage_odds=5 * P, **kw)
    assert out["status"] == "HEDGE_UNCERTAIN" and reason in out["uncertain_reasons"]
    assert out["liquidity_status"] == "HEDGE_AVAILABLE"  # the liquidity answer is still shown


def test_fees_unverifiable_leave_after_fee_pnl_null():
    out = run(fees=None, max_slippage_percentage_odds=5 * P)
    assert out["fees"]["hedge_taker_payout_fee_if_hedge_wins_base_units"] is None
    assert out["fees"]["pnl_after_projected_fees_base_units"] == {"if_outcome_one": None, "if_outcome_two": None}


def test_null_taker_fee_is_zero_derived():
    out = run(fees=dict(FEES, takerPayoutFee=None), max_slippage_percentage_odds=5 * P)
    assert out["status"] == "HEDGE_AVAILABLE"
    assert out["fees"]["hedge_taker_payout_fee_if_hedge_wins_base_units"] == "0"
    assert out["fees"]["provider_raw_rates"]["takerPayoutFee"] is None


def test_balanced_position_needs_no_hedge():
    out = run(market_position=position(p1=10, p2=10))
    assert out["status"] == "HEDGE_AVAILABLE" and out["reason"] == "POSITION_ALREADY_BALANCED" and out["legs"] == []


@pytest.mark.parametrize("kw,code", [
    ({"mode": "CASHOUT"}, "INVALID_HEDGE_MODE"),
    ({"mode": "PARTIAL", "fraction": "1.5"}, "INVALID_HEDGE_FRACTION"),
    ({"mode": "PARTIAL", "fraction": "nan"}, "INVALID_HEDGE_FRACTION"),
    ({"mode": "PARTIAL", "fraction": None}, "INVALID_HEDGE_FRACTION"),
    ({"mode": "FULL", "fraction": "0.5"}, "FRACTION_ONLY_FOR_PARTIAL"),
    ({"mode": "REDUCE", "hedge_stake_base_units": 0}, "INVALID_HEDGE_STAKE"),
    ({"mode": "FULL", "hedge_stake_base_units": 5}, "HEDGE_STAKE_ONLY_FOR_REDUCE"),
    ({"max_slippage_percentage_odds": -1}, "INVALID_MAX_SLIPPAGE"),
    ({"max_slippage_percentage_odds": 51 * P}, "INVALID_MAX_SLIPPAGE"),
    ({"book_perspective": "BOTH"}, "INVALID_BOOK_PERSPECTIVE"),
])
def test_invalid_parameters_raise_fixed_codes(kw, code):
    with pytest.raises(hg.HedgeError, match=code):
        run(**kw)
