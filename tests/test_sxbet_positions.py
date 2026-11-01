"""SX Bet position manager (bridge/sxbet/positions.py): pure, offline, documented payload shapes only."""
import copy
import json

from bridge.sxbet import positions as pm

M = "0x" + "11" * 32
M2 = "0x" + "22" * 32
T1, T2, T3 = ("0x" + c * 32 for c in ("a1", "a2", "a3"))
O1, O2 = "0x" + "b1" * 32, "0x" + "b2" * 32
FEES = {"refundFee": "0.005", "makerPayoutFee": "0.01", "takerPayoutFee": "0.02",
        "makerParlayPayoutFee": "0.03", "takerParlayPayoutFee": "0.04"}
TOKEN, ESCROW = "0x" + "1b" * 20, "0x" + "0d" * 20


def trade(tid, *, stake, ret, one, maker, status="LOCKED", market=M, updated="2026-07-31T18:47:12.837Z", **kw):
    row = {"tradeId": tid, "marketHash": market, "isBettingOutcomeOne": one, "isMaker": maker, "isParlay": False,
           "totalStake": str(stake), "totalReturn": str(ret), "netTotalReturn": str(ret),
           "weightedAverageOdds": str(stake * pm.ODDS // ret), "ceRefundAmount": "0", "ceRefundFeeAmount": "0",
           "fillCount": 1, "ownOrderIds": [O1], "status": status, "updatedAt": updated, "settlement": None}
    row.update(kw)
    return row


def fill(fid_suffix, *, tid, stake, ret, one, maker, status="LOCKED", market=M, updated="2026-07-31T18:47:12.837Z", **kw):
    match = "0x" + fid_suffix * 32
    row = {"id": f"{match}:{O1}:{market}", "matchId": match, "orderId": O1, "tradeId": tid, "marketHash": market,
           "quarterlineMarketHash": None, "isParlay": False, "fillAmount": str(stake), "fillOdds": "40000000000000000000",
           "returnAmount": str(ret), "netReturnAmount": str(ret), "isBettingOutcomeOne": one, "isMaker": maker,
           "status": status, "ceRefundAmount": "0", "ceRefundFeeAmount": "0", "updatedAt": updated, "settlement": None}
    row.update(kw)
    return row


def order(oid, *, total, remaining, one=True, odds="40000000000000000000", status="ACTIVE", market=M,
          updated="2026-07-31T18:45:07.456Z", **kw):
    row = {"id": oid, "marketHash": market, "isBettingOutcomeOne": one, "percentageOdds": odds,
           "totalBetSize": str(total), "remainingSize": str(remaining), "status": status, "inactiveReason": None,
           "eventId": "L12952568", "createdAt": "2026-07-31T18:45:07.437Z", "updatedAt": updated}
    row.update(kw)
    return row


def market_of(rollup, h=M):
    return next(m for m in rollup["markets"] if m["market_hash"] == h)


def test_docs_example_position_reproduced_from_bets_maker_and_taker():
    # positions-v3 example: totalStake 300, maxWin 200, maxLoss -100, isOutcomeOneMaxWin true.
    # 200 on outcome one @40% returns 500; 100 on outcome two @50% returns 200 (return = stake * 1e20 / odds).
    trades = [trade(T1, stake=200_000_000, ret=500_000_000, one=True, maker=True),
              trade(T2, stake=100_000_000, ret=200_000_000, one=False, maker=False)]
    pos = {"positions": [{"marketHash": M, "totalStake": "300000000", "maxWin": "200000000", "netMaxWin": "198400000",
                          "maxLoss": "-100000000", "isOutcomeOneMaxWin": True, "pnl": None, "tradeCount": 2,
                          "market": {"status": "ACTIVE", "marketHash": M, "outcomeOneName": "A", "outcomeTwoName": "B",
                                     "sportXeventId": "L1", "gameTime": 1773453600}}]}
    out = pm.build_positions(positions=pos, trades={"trades": trades}, fees=FEES)
    m = market_of(out)
    assert out["label"] == "DERIVED" and m["grain_used"] == "TRADES"
    assert m["pnl_base_units"] == {"if_outcome_one": 200_000_000, "if_outcome_two": -100_000_000, "if_void": 0}
    assert m["worst_case_liability_base_units"] == 100_000_000
    assert m["matched_stake_base_units"] == {"outcome_one": 200_000_000, "outcome_two": 100_000_000, "total": 300_000_000}
    assert m["potential_gross_payout_base_units"]["if_outcome_one"] == 500_000_000
    # Maker fee 1% on the maker bet's profit (300) if outcome one; taker fee 2% on 100 profit if outcome two.
    assert m["projected_payout_fee_base_units"]["if_outcome_one"] == "3000000"
    assert m["projected_payout_fee_base_units"]["if_outcome_two"] == "2000000"
    assert m["pnl_after_projected_fee_base_units"] == {"if_outcome_one": "197000000", "if_outcome_two": "-102000000",
                                                       "if_void": "0"}
    assert m["cross_checks"][0] == {"check": "POSITION_TOTAL_STAKE_EQUALS_OPEN_BETS", "result": "MATCH",
                                    "provider": "300000000", "derived": 300_000_000}
    assert m["event_id"] == "L1" and m["market"]["outcomeOneName"] == "A"
    assert m["provider_rows"]["position"] == pos["positions"][0]  # raw preserved
    assert out["data_conflict"] is False


def test_position_row_only_grain_uses_documented_maxwin_maxloss_and_no_invented_split():
    pos = [{"marketHash": M, "totalStake": "300000000", "maxWin": "200000000", "maxLoss": "-100000000",
            "isOutcomeOneMaxWin": False, "netMaxWin": "198400000", "pnl": None}]
    m = market_of(pm.build_positions(positions=pos))
    assert m["grain_used"] == "POSITION_ROW"
    assert m["pnl_base_units"] == {"if_outcome_one": -100_000_000, "if_outcome_two": 200_000_000, "if_void": 0}
    assert m["matched_stake_base_units"]["outcome_one"] is None and m["projected_payout_fee_base_units"]["if_outcome_one"] is None
    assert m["provider_net_max_win_base_units"] == 198_400_000


def test_fees_null_means_unset_raw_null_kept_and_derived_zero_separate():
    fees = dict(FEES, takerPayoutFee=None)
    info = pm.parse_fees(fees)
    assert info["raw"]["takerPayoutFee"] is None and info["derived_rates"]["takerPayoutFee"] == "0"
    assert info["null_rate_notes"]["takerPayoutFee"] == "PROVIDER_NULL_UNSET_NOTHING_CHARGED"
    out = pm.build_positions(trades=[trade(T2, stake=100, ret=200, one=False, maker=False)], fees=fees)
    assert market_of(out)["projected_payout_fee_base_units"]["if_outcome_two"] == "0"
    assert out["fees"]["raw"]["takerPayoutFee"] is None


def test_fees_missing_or_malformed_stay_missing():
    t = [trade(T1, stake=100, ret=250, one=True, maker=False)]
    for fees, status in ((None, "FEES_NOT_SUPPLIED"), ({"takerPayoutFee": "abc"}, "FEES_MALFORMED"),
                         (dict(FEES, makerPayoutFee="1.5"), "FEES_MALFORMED")):
        out = pm.build_positions(trades=t, fees=fees)
        m = market_of(out)
        assert out["fees"]["status"] == status
        assert m["projected_payout_fee_base_units"]["if_outcome_one"] is None
        assert m["pnl_after_projected_fee_base_units"]["if_outcome_one"] is None
        assert m["pnl_base_units"]["if_outcome_one"] == 150  # pre-fee value unaffected


def test_partial_fills_resting_orders_and_capital():
    orders = {"orders": [order(O1, total=2_000_000, remaining=500_000),
                         order(O2, total=1_000_000, remaining=1_000_000, one=False, odds="50000000000000000000")]}
    balance = {"balances": [{"tokenAddress": TOKEN, "escrowAddress": ESCROW, "availableAmount": "800000",
                             "pendingAvailableAmount": "-100000", "escrowedAmount": "1500000", "pendingEscrowAmount": "0"}]}
    t = [trade(T1, stake=1_500_000, ret=3_750_000, one=True, maker=True)]
    out = pm.build_positions(trades=t, open_orders=orders, balance=balance, base_token=TOKEN, escrow=ESCROW)
    m = market_of(out)
    r = m["resting_orders"]
    assert r["count"] == 2 and r["remaining_base_units"] == {"outcome_one": 500_000, "outcome_two": 1_000_000}
    assert r["partially_filled"] == [{"id": O1, "filled_base_units": 1_500_000, "remaining_base_units": 500_000}]
    assert r["capital_locked_by_resting"] == 0
    assert r["within_balance_per_market_outcome"] == {"outcome_one": True, "outcome_two": False}
    # if all resting filled: +500000@40% returns 1250000 on one; +1000000@50% returns 2000000 on two
    assert r["pnl_if_all_resting_filled_base_units"] == {"if_outcome_one": 2_250_000 + 1_250_000 - 1_500_000,
                                                         "if_outcome_two": -1_500_000 + 2_000_000 - 1_500_000}
    cap = out["capital"]
    assert cap["token_check"] == "EXACT_TOKEN_ESCROW_MATCH" and cap["available_conservative_base_units"] == 700_000
    assert cap["provider_raw"]["pendingAvailableAmount"] == "-100000" and cap["committed_escrow_base_units"] == 1_500_000


def test_balance_missing_or_ambiguous_is_unresolved_not_zero():
    assert pm.capital(None)["status"] == "BALANCE_UNRESOLVED"
    two = {"balances": [{"availableAmount": "1"}, {"availableAmount": "2"}]}
    assert pm.capital(two)["reason"] == "NO_UNIQUE_BALANCE_ROW"
    out = pm.build_positions(trades=[trade(T1, stake=1, ret=2, one=True, maker=True)])
    assert out["totals"]["available_conservative_base_units"] is None


def test_settlement_realized_pnl_uses_documented_formula_and_failed_moves_no_funds():
    won = trade(T1, stake=1_000_000, ret=2_500_000, one=False, maker=False, status="SETTLED", ceRefundAmount="100",
                settlement={"outcome": 2, "settleReturnAmount": "2488000", "settleFeeAmount": "12000",
                            "settleCeRefundAmount": "0", "settleDate": "2026-08-01T00:00:00Z"})
    lost = trade(T2, stake=500_000, ret=1_000_000, one=True, maker=True, status="SETTLED",
                 settlement={"outcome": 2, "settleReturnAmount": "0", "settleFeeAmount": "0", "settleCeRefundAmount": "0"})
    failed = trade(T3, stake=9_000_000, ret=18_000_000, one=True, maker=False, status="FAILED")
    m = market_of(pm.build_positions(trades=[won, lost, failed], fees=FEES))
    # realised = settleReturnAmount + ceRefundAmount - stake, summed
    assert m["realized_pnl_base_units"] == (2_488_000 + 100 - 1_000_000) + (0 + 0 - 500_000)
    assert m["settle_fees_charged_base_units"] == 12_000
    assert m["failed_bet_rows_excluded"] == 1 and m["open_bet_rows"] == 0
    assert m["matched_stake_base_units"]["total"] == 0 and m["worst_case_liability_base_units"] == 0


def test_settlement_missing_fields_keep_realized_null():
    s = trade(T1, stake=1, ret=2, one=True, maker=True, status="SETTLED", settlement=None)
    m = market_of(pm.build_positions(trades=[s]))
    assert m["realized_pnl_base_units"] is None and m["realized_pnl_reason"] == "SETTLEMENT_FIELDS_MISSING"


def test_fill_grain_preferred_when_it_covers_every_bet_and_cross_checks_trade():
    t = trade(T1, stake=1_000_000, ret=2_500_000, one=False, maker=False, fillCount=2)
    f1 = fill("e1", tid=T1, stake=333_334, ret=833_335, one=False, maker=False)
    f2 = fill("e2", tid=T1, stake=666_666, ret=1_666_665, one=False, maker=False)
    m = market_of(pm.build_positions(trades=[t], fills=[f1, f2]))
    assert m["grain_used"] == "FILLS" and m["pnl_base_units"]["if_outcome_two"] == 1_500_000
    assert m["cross_checks"] == [] and m["data_conflict"] is False
    bad = dict(f2, fillAmount="600000")
    m = market_of(pm.build_positions(trades=[t], fills=[f1, bad]))
    assert {"check": "TRADE_STAKE_EQUALS_SUM_OF_FILLS", "trade_id": T1, "result": "MISMATCH"} in m["cross_checks"]
    assert m["data_conflict"] is True


def test_trade_grain_used_when_fills_miss_a_bet():
    t1 = trade(T1, stake=100, ret=250, one=True, maker=False)
    t2 = trade(T2, stake=100, ret=200, one=False, maker=False)
    f1 = fill("e1", tid=T1, stake=100, ret=250, one=True, maker=False)
    m = market_of(pm.build_positions(trades=[t1, t2], fills=[f1]))
    assert m["grain_used"] == "TRADES" and m["matched_stake_base_units"]["total"] == 200


def test_rest_and_ws_dedupe_exact_duplicates_and_latest_version_wins():
    rest = trade(T1, stake=100, ret=250, one=True, maker=False, status="MATCHED", updated="2026-07-31T18:47:09.000Z")
    ws_same = {"channel": "account:trades_v3_#0xabc", "data": {"trade": copy.deepcopy(rest)}}
    ws_new = {"trade": dict(rest, status="LOCKED", updatedAt="2026-07-31T18:47:12.000Z")}
    out = pm.build_positions(trades=[rest], ws_messages=[ws_same, ws_new])
    rec = out["reconciliation"]
    assert rec["exact_duplicates_dropped"] == 1 and rec["conflicts"] == []
    m = market_of(out)
    assert m["matched_not_locked_stake_base_units"] == 0  # LOCKED version applied


def test_ws_conflicts_are_reported_and_excluded_never_guessed():
    rest = trade(T1, stake=100, ret=250, one=True, maker=False, status="LOCKED", updated="2026-07-31T18:47:12.000Z")
    same_ts = {"trade": dict(rest, totalStake="999")}
    regress = {"trade": dict(rest, status="MATCHED", updatedAt="2026-07-31T18:48:00.000Z")}
    for ws, reason in (([same_ts], "SAME_UPDATED_AT_DIFFERENT_CONTENT"), ([regress], "STATUS_REGRESSION_LOCKED_TO_MATCHED")):
        out = pm.build_positions(trades=[rest], ws_messages=ws)
        assert out["reconciliation"]["conflicts"][0]["reason"] == reason
        assert out["data_conflict"] is True and out["reconciliation"]["conflicting_markets"] == [M]
        assert out["markets"] == [] or market_of(out)["matched_stake_base_units"]["total"] == 0


def test_order_versions_remaining_size_must_not_grow_and_unorderable_versions_conflict():
    a = order(O1, total=10, remaining=5, updated="2026-08-03T13:58:32.000Z")
    grown = {"order": dict(a, remainingSize="8", updatedAt="2026-08-03T13:59:00.000Z")}
    out = pm.build_positions(open_orders=[a], ws_messages=[grown])
    assert out["reconciliation"]["conflicts"][0]["reason"] == "REMAINING_SIZE_INCREASED_OR_MALFORMED"
    no_ts = {"order": {k: v for k, v in dict(a, remainingSize="4").items() if k != "updatedAt"}}
    out = pm.build_positions(open_orders=[a], ws_messages=[no_ts])
    assert out["reconciliation"]["conflicts"][0]["reason"] == "UNORDERABLE_VERSIONS_UPDATED_AT_MISSING"
    filled = {"order": dict(a, remainingSize="0", status="INACTIVE", inactiveReason="FILLED",
                            updatedAt="2026-08-03T14:00:00.000Z")}
    out = pm.build_positions(open_orders=[a], ws_messages=[filled])
    assert out["reconciliation"]["conflicts"] == [] and out["reconciliation"]["inactive_orders_seen"] == 1
    assert out["markets"] == []  # no longer resting: nothing on the book


def test_ws_shape_and_channel_mismatch_are_rejected():
    out = pm.build_positions(ws_messages=[{"channel": "account:fills_v3_#0x1", "data": {"trade": {"tradeId": T1}}},
                                          {"foo": 1}, "x"])
    reasons = [r["reason"] for r in out["reconciliation"]["ws_rejected"]]
    assert reasons == ["WS_CHANNEL_PAYLOAD_MISMATCH", "WS_PAYLOAD_SHAPE_UNKNOWN", "WS_PAYLOAD_SHAPE_UNKNOWN"]


def test_parlay_and_quarter_line_are_not_modelled():
    q = fill("e1", tid=T1, stake=100, ret=200, one=True, maker=False, quarterlineMarketHash=M2)
    p = trade(T2, stake=100, ret=200, one=True, maker=False, isParlay=True)
    out = pm.build_positions(trades=[p], fills=[q])
    assert {u["reason"] for u in out["reconciliation"]["unsupported_rows"]} == {"PARLAY_OR_QUARTER_LINE_NOT_MODELLED"}
    assert out["markets"] == []


def test_more_pages_and_malformed_inputs_mark_incomplete():
    out = pm.build_positions(trades={"trades": [], "nextKey": "abc"}, fills={"oops": 1})
    assert out["completeness"] == {"pages_not_read": ["trades"], "malformed_inputs": ["fills"], "complete": False,
                                   "excluded_rows": {"malformed": 0, "unsupported": 0, "market_hashes": []},
                                   "note": out["completeness"]["note"]}
    bad = trade(T1, stake=100, ret=250, one=True, maker=False)
    bad["totalStake"] = "-5"
    out = pm.build_positions(trades=[bad])
    assert out["reconciliation"]["malformed_rows"][0]["reason"] == "TRADE_ROW_INCOMPLETE" and out["markets"] == []


def test_mark_to_market_only_with_documented_best_odds():
    t = [trade(T1, stake=200, ret=500, one=True, maker=False)]  # pnl1 +300, pnl2 -200, gap 500
    out = pm.build_positions(trades=t)
    mtm = market_of(out)["mark_to_market"]
    assert mtm["value_base_units"] is None and mtm["reason"] == "BEST_ODDS_NOT_SUPPLIED"
    assert out["totals"]["mark_to_market_base_units"] is None
    # maker resting on outcome one at 60% -> taker price for outcome two 40%; s = 500*0.4 = 200 -> both = 100
    best = {"bestOdds": [{"marketHash": M, "outcomeOne": {"percentageOdds": "60000000000000000000", "size": "1000"},
                          "outcomeTwo": None}]}
    mtm = market_of(pm.build_positions(trades=t, best_odds=best))["mark_to_market"]
    assert mtm["value_base_units"] == 100 and mtm["market_label"] == "HYBRID_MARKET_AWARE"
    assert mtm["hedge_outcome"] == "outcome_two" and mtm["taker_percentage_odds"] == "40000000000000000000"
    assert mtm["top_level_covers_hedge"] is True and mtm["fees_included"] is False
    side_null = {"bestOdds": [{"marketHash": M, "outcomeOne": None, "outcomeTwo": {"percentageOdds": "1", "size": "1"}}]}
    assert market_of(pm.build_positions(trades=t, best_odds=side_null))["mark_to_market"]["reason"] == "NO_LIQUIDITY_ON_HEDGE_SIDE"


def test_selection_event_and_totals_roll_up():
    t = [trade(T1, stake=100, ret=250, one=True, maker=False), trade(T2, stake=50, ret=100, one=False, maker=False, market=M2)]
    markets = [{"marketHash": M, "sportXeventId": "L1", "outcomeOneName": "Home", "outcomeTwoName": "Away"},
               {"marketHash": M2, "sportXeventId": "L1"}]
    out = pm.build_positions(trades=t, markets=markets)
    assert out["totals"]["matched_stake_base_units"] == 150
    assert out["totals"]["worst_case_liability_base_units"] == 100 + 50
    assert out["events"] == [{"event_id": "L1", "markets": 2, "worst_case_liability_base_units": 150,
                              "matched_stake_base_units": 150}]
    sel = [s for s in out["selections"] if s["market_hash"] == M]
    assert sel[0] == {"market_hash": M, "selection": "outcome_one", "outcome_name": "Home",
                      "matched_stake_base_units": 100, "potential_gross_payout_base_units": 250,
                      "resting_remaining_base_units": 0}


def test_deterministic_and_inputs_not_mutated():
    t = [trade(T1, stake=100, ret=250, one=True, maker=False)]
    before = json.dumps(t, sort_keys=True)
    a = pm.build_positions(trades=t, fees=FEES)
    b = pm.build_positions(trades=copy.deepcopy(t), fees=FEES)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert json.dumps(t, sort_keys=True) == before
    assert set(pm.DOCS_UNVERIFIED) and all(s.startswith("https://docs.sx.bet/") for s in pm.SOURCES)


def test_raw_bet_rows_echoed_and_return_below_stake_is_malformed():
    t = trade(T1, stake=100, ret=250, one=True, maker=False)
    m = market_of(pm.build_positions(trades=[t]))
    assert m["provider_rows"]["bets"] == [t]
    bad = trade(T2, stake=100, ret=90, one=True, maker=False)  # implied odds above 100%: impossible
    out = pm.build_positions(trades=[bad])
    assert out["markets"] == [] and out["reconciliation"]["malformed_rows"][0]["reason"] == "TRADE_ROW_INCOMPLETE"


def test_resting_only_market_has_zero_exposure_and_zero_fee_with_reason():
    m = market_of(pm.build_positions(open_orders=[order(O1, total=10, remaining=10)]))
    assert m["grain_used"] == "NONE" and m["worst_case_liability_base_units"] == 0
    assert m["projected_payout_fee_base_units"]["reason"] == "NO_BET_ON_MARKET"
    assert m["mark_to_market"]["reason"] == "NO_OPEN_POSITION"


# ---------------------------------------------------------------- review findings (regressions)
def _excluded_cases():
    good = trade(T1, stake=100, ret=250, one=True, maker=False)
    parlay = trade(T2, stake=100, ret=200, one=True, maker=False, isParlay=True)
    quarter = trade(T2, stake=100, ret=200, one=True, maker=False, quarterlineMarketHash=M2)
    malformed = {k: v for k, v in trade(T2, stake=100, ret=200, one=True, maker=False).items() if k != "totalReturn"}
    return good, (("parlay", parlay, "unsupported"), ("quarter_line", quarter, "unsupported"),
                  ("malformed", malformed, "malformed"))


def test_excluded_bet_rows_mark_market_conflicted_and_rollup_incomplete():
    good, cases = _excluded_cases()
    for name, row, bucket in cases:
        out = pm.build_positions(trades=[good, row], fees=FEES)
        assert out["completeness"]["complete"] is False, name
        ex = out["completeness"]["excluded_rows"]
        assert ex[bucket] == 1 and ex["market_hashes"] == [M], name
        assert out["data_conflict"] is True and M in out["reconciliation"]["conflicting_markets"], name
        assert market_of(out)["data_conflict"] is True, name
        # Excluded-only market: still incomplete and conflicted, never a silent zero position.
        out = pm.build_positions(trades=[row], fees=FEES)
        assert out["completeness"]["complete"] is False and out["data_conflict"] is True, name
        assert out["completeness"]["excluded_rows"]["market_hashes"] == [M], name


def test_no_excluded_rows_keeps_rollup_complete():
    out = pm.build_positions(trades=[trade(T1, stake=100, ret=250, one=True, maker=False)], fees=FEES)
    assert out["completeness"]["complete"] is True and out["data_conflict"] is False
    assert out["completeness"]["excluded_rows"] == {"malformed": 0, "unsupported": 0, "market_hashes": []}


def test_fill_count_disagreeing_with_fills_present_is_conflict_and_falls_back_to_trades():
    t = trade(T1, stake=1_000_000, ret=2_500_000, one=False, maker=False, fillCount=2)
    f1 = fill("e1", tid=T1, stake=333_334, ret=833_335, one=False, maker=False)
    m = market_of(pm.build_positions(trades=[t], fills=[f1]))
    assert m["data_conflict"] is True
    assert m["grain_used"] == "TRADES" and m["matched_stake_base_units"]["total"] == 1_000_000
    assert {"check": "TRADE_FILL_COUNT_EQUALS_FILLS_PRESENT", "trade_id": T1, "result": "MISMATCH",
            "provider_fill_count": 2, "fills_present": 1} in m["cross_checks"]
    missing = {k: v for k, v in t.items() if k != "fillCount"}
    f2 = fill("e2", tid=T1, stake=666_666, ret=1_666_665, one=False, maker=False)
    m = market_of(pm.build_positions(trades=[missing], fills=[f1, f2]))
    assert m["data_conflict"] is True and m["grain_used"] == "TRADES"
    assert any(c["check"] == "TRADE_FILL_COUNT_EQUALS_FILLS_PRESENT" and c["result"] == "FILL_COUNT_MISSING"
               for c in m["cross_checks"])


def test_realized_pnl_null_when_settled_rows_not_read():
    t = [trade(T1, stake=100, ret=250, one=True, maker=False)]
    out = pm.build_positions(trades=t, settled_rows_read=False)
    m = market_of(out)
    assert m["realized_pnl_base_units"] is None and m["realized_pnl_reason"] == "SETTLED_ROWS_NOT_READ"
    assert out["totals"]["realized_pnl_base_units"] is None
    assert out["totals"]["realized_pnl_reason"] == "SETTLED_ROWS_NOT_READ"
    # Resting-only market: still not 0 when settled rows were not read.
    out = pm.build_positions(open_orders=[order(O1, total=10, remaining=10)], settled_rows_read=False)
    assert out["totals"]["realized_pnl_base_units"] is None
    # Default (settled rows part of the input): unchanged documented formula.
    assert market_of(pm.build_positions(trades=t))["realized_pnl_base_units"] == 0


def test_fee_key_absent_is_malformed_but_explicit_null_is_unset():
    absent = {k: v for k, v in FEES.items() if k != "takerPayoutFee"}
    info = pm.parse_fees(absent)
    assert info["status"] == "FEES_MALFORMED" and info["reason"] == "FEE_FIELD_MISSING"
    assert info["missing_fields"] == ["takerPayoutFee"] and info["derived_rates"] is None
    out = pm.build_positions(trades=[trade(T2, stake=100, ret=200, one=False, maker=False)], fees=absent)
    assert market_of(out)["projected_payout_fee_base_units"]["if_outcome_two"] is None
    explicit = dict(FEES, takerPayoutFee=None)
    info = pm.parse_fees(explicit)
    assert info["status"] == "FEES_OK" and info["derived_rates"]["takerPayoutFee"] == "0"
    assert info["null_rate_notes"]["takerPayoutFee"] == "PROVIDER_NULL_UNSET_NOTHING_CHARGED"


def test_position_row_with_positive_or_inconsistent_maxloss_is_unverified():
    base = {"marketHash": M, "totalStake": "300000000", "maxWin": "200000000", "isOutcomeOneMaxWin": True,
            "netMaxWin": "198400000", "pnl": None}
    for loss in ("100000000", "300000000"):
        m = market_of(pm.build_positions(positions=[dict(base, maxLoss=loss)]))
        assert m["pnl_base_units"]["if_outcome_one"] is None and m["pnl_base_units"]["if_outcome_two"] is None
        assert m["pnl_reason"] == "MAXLOSS_SIGN_UNVERIFIED"
        assert m["worst_case_liability_base_units"] is None
        assert m["mark_to_market"]["reason"] == "POSITION_PNL_MISSING"
    m = market_of(pm.build_positions(positions=[dict(base, maxLoss="-100000000")]))
    assert m["pnl_base_units"]["if_outcome_two"] == -100_000_000 and m["pnl_reason"] is None
