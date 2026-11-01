"""SX Bet hedge simulator for one binary market position. Pure; never signs, never submits.

A position is two numbers: P&L if outcome one wins (P1) and if outcome two wins
(P2). On SX Bet every bet is "stake s on one outcome at the bettor's implied
price q" and returns s * 1e20 / q stake included (api-reference/get-fills-v3:
333334 @ 40% -> 833335). Backing the cheaper-for-us outcome is the only way to
hedge: there is no separate lay (developers/order-book: "To bet an outcome, you
consume the orders resting on the *other* outcome").

If P1 > P2, back outcome two with stake s, return r = s * 1e20 / q:
    P1' = P1 - s            P2' = P2 + (r - s)
    gap' = P1' - P2' = (P1 - P2) - r
so every level consumed closes the gap by exactly its *return*; the full hedge
needs total return r = P1 - P2 (stake s = (P1 - P2) * q / 1e20 at one level).
Symmetric for P2 > P1. Levels come from the official snapshot
(GET /orderbook-v3/snapshot/market): in the maker frame (default, the frame the
docs call authoritative) a maker level (p, size) on the *other* outcome offers
taker price q = 1e20 - p for up to size * (1e20 - p) / p of taker stake
(developers/order-book); with showTakerPerspective=true the sides are already
swapped, q = percentageOdds and size = taker stake (api-reference
get-market-orderbook-snapshot). Levels are walked best price first
(price-time priority). The engine never matches you against your own resting
orders (developers/taking-liquidity), so own remaining size at a level is removed.

The taker order's percentageOdds is "the highest implied probability you're
willing to pay" (developers/taking-liquidity): worst acceptable = best price +
max slippage, rounded DOWN onto the odds ladder (developers/odds-rounding).
Fees: a hedge is a taker order, so takerPayoutFee applies to its profit only if
it wins (developers/fees); null rate = unset = 0 DERIVED. Base-unit rounding of
returns is floored here (conservative, DOCS_UNVERIFIED exchange rounding).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from . import positions as pm
from .simulation import on_ladder, round_down_to_ladder

ODDS = pm.ODDS
SCHEMA = "sxbet.hedge_simulation.v1"
MODES = ("FULL", "PARTIAL", "REDUCE")
PERSPECTIVES = ("MAKER", "TAKER")
AVAILABLE, PARTIAL, UNAVAILABLE, UNCERTAIN = ("HEDGE_AVAILABLE", "PARTIAL_HEDGE_ONLY", "HEDGE_UNAVAILABLE",
                                              "HEDGE_UNCERTAIN")
SOURCES = ("https://docs.sx.bet/developers/order-book", "https://docs.sx.bet/developers/taking-liquidity",
           "https://docs.sx.bet/api-reference/get-market-orderbook-snapshot", "https://docs.sx.bet/developers/fees",
           "https://docs.sx.bet/developers/odds-rounding", "https://docs.sx.bet/developers/bet-lifecycle",
           "https://docs.sx.bet/api-reference/get-fills-v3")
WARNING = ("NOT A GUARANTEED CASHOUT. A simulation on a snapshot: the book can move or be withdrawn before any "
           "order arrives, betting delay and self-match prevention apply, MATCHED is not LOCKED, fee rounding is "
           "not documented, and nothing here is signed or submitted. An owner-approved, signer-verified order "
           "is required for any real hedge (governance 2.6).")
MAX_SLIPPAGE = 50 * 10 ** 18  # 50 implied-probability points: input sanity bound, not a provider rule


class HedgeError(ValueError):
    """Fixed-code input error."""


def validate_params(mode: Any, fraction: Any, hedge_stake: Any, slippage: Any, perspective: Any = "MAKER") -> dict:
    if mode not in MODES:
        raise HedgeError("INVALID_HEDGE_MODE")
    if perspective not in PERSPECTIVES:
        raise HedgeError("INVALID_BOOK_PERSPECTIVE")
    if type(slippage) is not int or not 0 <= slippage <= MAX_SLIPPAGE:
        raise HedgeError("INVALID_MAX_SLIPPAGE")
    frac = None
    if mode == "PARTIAL":
        try:
            frac = Decimal(fraction) if isinstance(fraction, str) and len(fraction) <= 20 else None
        except InvalidOperation:
            frac = None
        if frac is None or not frac.is_finite() or not 0 < frac < 1:
            raise HedgeError("INVALID_HEDGE_FRACTION")
    elif fraction is not None:
        raise HedgeError("FRACTION_ONLY_FOR_PARTIAL")
    if mode == "REDUCE":
        if type(hedge_stake) is not int or not 0 < hedge_stake < 2 ** 128:
            raise HedgeError("INVALID_HEDGE_STAKE")
    elif hedge_stake is not None:
        raise HedgeError("HEDGE_STAKE_ONLY_FOR_REDUCE")
    return {"mode": mode, "fraction": frac, "hedge_stake": hedge_stake, "slippage": slippage, "perspective": perspective}


def _levels(book: Mapping[str, Any], hedge_one: bool, perspective: str, own: list[dict]) -> list[tuple[int, int, dict]]:
    """[(taker price q, taker capacity, raw level)] for backing the hedge outcome, own orders removed."""
    if perspective == "MAKER":
        side = "outcomeTwo" if hedge_one else "outcomeOne"  # makers resting on the other outcome
    else:
        side = "outcomeOne" if hedge_one else "outcomeTwo"
    raw = book.get(side)
    if not isinstance(raw, list):
        raise HedgeError("ORDER_BOOK_SIDE_MISSING")
    # own resting maker orders on the consumed maker side (betting the outcome we do NOT hedge)
    own_by_maker_odds: dict[int, int] = {}
    for o in own:
        if o["outcome_one"] is not hedge_one:
            own_by_maker_odds[o["odds"]] = own_by_maker_odds.get(o["odds"], 0) + o["remaining"]
    out = []
    for lv in raw:
        p = pm.odds(lv.get("percentageOdds")) if isinstance(lv, dict) else None
        size = pm.units(lv.get("size")) if isinstance(lv, dict) else None
        if p is None or size is None:
            raise HedgeError("BOOK_MALFORMED")
        if perspective == "MAKER":
            maker_p, maker_size = p, max(0, size - own_by_maker_odds.get(p, 0))
            q, cap = ODDS - maker_p, maker_size * (ODDS - maker_p) // maker_p
        else:
            q, maker_p = p, ODDS - p
            own_cap = own_by_maker_odds.get(maker_p, 0) * (ODDS - maker_p) // maker_p
            cap = max(0, size - own_cap)
        out.append((q, cap, lv))
    out.sort(key=lambda x: x[0])
    return out


def simulate_hedge(*, market_position: Mapping[str, Any] | None, book: Any, market: Any, fees: Any,
                   mode: str = "FULL", fraction: str | None = None, hedge_stake_base_units: int | None = None,
                   max_slippage_percentage_odds: int = 0, book_perspective: str = "MAKER",
                   own_resting_orders: list | None = None, data_age_seconds: float | None = None,
                   max_data_age_seconds: float | None = None, odds_ladder_step_size: int | None = None,
                   min_order_base_units: int | None = None) -> dict[str, Any]:
    """Simulate reduce / partial / full hedge. Status is exactly one of the four HEDGE_* values.

    market_position: one `markets[]` entry of positions.build_positions (pnl_base_units, projected fees,
    data_conflict). market: the GET /markets/find row. fees: GET /user/fees-v3 data (raw) or None.
    own_resting_orders: `resting_orders` rows from the position manager or raw GET /orders-v3 rows.
    Raises HedgeError only for invalid parameters (checked before any read by the MCP tool).
    """
    params = validate_params(mode, fraction, hedge_stake_base_units, max_slippage_percentage_odds, book_perspective)
    inputs = {"mode": mode, "fraction": fraction, "hedge_stake_base_units": hedge_stake_base_units,
              "max_slippage_percentage_odds": str(max_slippage_percentage_odds), "book_perspective": book_perspective,
              "data_age_seconds": data_age_seconds, "max_data_age_seconds": max_data_age_seconds,
              "odds_ladder_step_size": odds_ladder_step_size, "min_order_base_units": min_order_base_units}
    out: dict[str, Any] = {"schema": SCHEMA, "label": "DERIVED", "market_label": "HYBRID_MARKET_AWARE",
                           "sources": list(SOURCES), "inputs": inputs, "warning": WARNING,
                           "guaranteed_cashout": False, "signed": False, "submitted": False,
                           "provider_book": book if isinstance(book, dict) else None}

    def unavailable(reason: str, **extra) -> dict[str, Any]:
        return {**out, "status": UNAVAILABLE, "reason": reason, **extra}

    if not isinstance(market_position, Mapping):
        return unavailable("POSITION_DATA_MISSING")
    pnl = market_position.get("pnl_base_units") or {}
    p1, p2 = pnl.get("if_outcome_one"), pnl.get("if_outcome_two")
    if type(p1) is not int or type(p2) is not int:
        if market_position.get("data_conflict"):  # unknown because of conflicting / excluded rows
            return {**out, "status": UNCERTAIN, "reason": "POSITION_PNL_MISSING",
                    "uncertain_reasons": ["CONFLICTING_POSITION_DATA"]}
        return unavailable("POSITION_PNL_MISSING")
    h = pm._lower(market_position.get("market_hash"))
    if not isinstance(market, Mapping):
        return unavailable("MARKET_STATUS_UNKNOWN")
    if h and pm._lower(market.get("marketHash")) not in (None, h):
        return unavailable("MARKET_MISMATCH")
    if market.get("status") != "ACTIVE":
        return unavailable("MARKET_NOT_ACTIVE", provider_market_status=market.get("status"))
    if not isinstance(book, Mapping):
        return unavailable("ORDER_BOOK_MISSING")
    if h and pm._lower(book.get("marketHash")) not in (None, h):
        return unavailable("BOOK_MARKET_MISMATCH")

    out["before"] = {"pnl_base_units": {"if_outcome_one": p1, "if_outcome_two": p2},
                     "worst_case_base_units": min(p1, p2), "gap_base_units": abs(p1 - p2)}
    uncertain: list[str] = []
    if max_data_age_seconds is not None and (data_age_seconds is None or data_age_seconds > max_data_age_seconds):
        uncertain.append("STALE_DATA")
    if market_position.get("data_conflict"):
        uncertain.append("CONFLICTING_POSITION_DATA")
    fee_info = pm.parse_fees(fees)
    rates = fee_info["derived_rates"]
    if rates is None:
        uncertain.append("FEE_UNVERIFIABLE_" + fee_info["status"])
    pos_fee = market_position.get("projected_payout_fee_base_units") or {}
    pf1, pf2 = pos_fee.get("if_outcome_one"), pos_fee.get("if_outcome_two")
    if pf1 is None or pf2 is None:
        uncertain.append("POSITION_FEE_UNVERIFIABLE")

    gap = p1 - p2
    if gap == 0:
        return {**out, "status": UNCERTAIN if uncertain else AVAILABLE, "uncertain_reasons": uncertain,
                "reason": "POSITION_ALREADY_BALANCED", "legs": [], "hedge_stake_base_units": "0",
                "after": out["before"], "liquidity": None}
    hedge_one = gap < 0
    own = []
    for o in own_resting_orders or []:
        if not isinstance(o, Mapping):
            continue
        if "remaining" in o:  # position-manager row
            own.append({"outcome_one": o["outcome_one"], "odds": o["odds"], "remaining": o["remaining"]})
        elif o.get("status") in ("ACTIVE", "PENDING") and pm._lower(o.get("marketHash")) in (None, h):
            rem, p, side = pm.units(o.get("remainingSize")), pm.odds(o.get("percentageOdds")), o.get("isBettingOutcomeOne")
            if rem is not None and p is not None and isinstance(side, bool):
                own.append({"outcome_one": side, "odds": p, "remaining": rem})
    try:
        levels = _levels(book, hedge_one, book_perspective, own)
    except HedgeError as exc:
        return unavailable(str(exc))
    total_capacity = sum(c for _, c, _ in levels)
    usable = [lv for lv in levels if lv[1] > 0]
    if not usable:
        return unavailable("NO_LIQUIDITY", liquidity={"levels": len(levels), "capacity_base_units": str(total_capacity)})
    best_q = usable[0][0]
    worst = min(best_q + max_slippage_percentage_odds, ODDS - 1)
    ladder_note = "NOT_ROUNDED_LADDER_STEP_UNKNOWN"
    if odds_ladder_step_size is not None:
        rounded = round_down_to_ladder(worst, odds_ladder_step_size)
        worst = rounded if rounded >= best_q and on_ladder(rounded, odds_ladder_step_size) else best_q
        ladder_note = "ROUNDED_DOWN_TO_LADDER"
    eligible = [lv for lv in usable if lv[0] <= worst]

    full_return = abs(gap)
    target = full_return if mode != "PARTIAL" else int(Decimal(full_return) * params["fraction"])
    budget = hedge_stake_base_units if mode == "REDUCE" else None
    remaining, spent, got, legs = target, 0, 0, []
    for q, cap, raw in eligible:
        if remaining <= 0 or (budget is not None and spent >= budget):
            break
        stake = min(cap, remaining * q // ODDS)
        if budget is not None:
            stake = min(stake, budget - spent)
        if stake <= 0:
            break
        ret = stake * ODDS // q
        legs.append({"taker_percentage_odds": str(q), "stake_base_units": str(stake), "return_base_units": str(ret),
                     "level_capacity_base_units": str(cap), "provider_level": raw})
        spent, got, remaining = spent + stake, got + ret, remaining - ret
    # Covered when no further whole base unit of stake at the worst used price could close more gap.
    last_q = eligible[-1][0] if eligible else best_q
    dust = remaining * last_q // ODDS == 0
    covered = (budget is not None and spent >= budget) or dust
    liquidity = {"levels": len(levels), "levels_within_slippage": len(eligible),
                 "capacity_base_units": str(total_capacity),
                 "capacity_within_slippage_base_units": str(sum(c for _, c, _ in eligible)),
                 "target_return_base_units": str(target), "covered": covered}
    if not legs:
        return unavailable("GAP_BELOW_ONE_BASE_UNIT_OF_STAKE" if eligible else "NO_LIQUIDITY_WITHIN_SLIPPAGE",
                           liquidity=liquidity)
    if min_order_base_units is not None and spent < min_order_base_units:
        return unavailable("HEDGE_STAKE_BELOW_MIN_ORDER", liquidity=liquidity,
                           hedge_stake_base_units=str(spent), min_order_base_units=str(min_order_base_units))
    if min_order_base_units is None:
        uncertain.append("MIN_ORDER_SIZE_UNCHECKED")
    profit = got - spent
    if hedge_one:
        a1, a2 = p1 + profit, p2 - spent
    else:
        a1, a2 = p1 - spent, p2 + profit
    hedge_fee = None if rates is None else Decimal(rates["takerPayoutFee"]) * profit
    after_fee = {"if_outcome_one": None, "if_outcome_two": None}
    if hedge_fee is not None and pf1 is not None and pf2 is not None:
        after_fee = {"if_outcome_one": pm.dstr(Decimal(a1) - Decimal(pf1) - (hedge_fee if hedge_one else 0)),
                     "if_outcome_two": pm.dstr(Decimal(a2) - Decimal(pf2) - (0 if hedge_one else hedge_fee))}
    liquidity_status = AVAILABLE if covered else PARTIAL
    return {
        **out,
        "status": UNCERTAIN if uncertain else liquidity_status,
        "liquidity_status": liquidity_status, "uncertain_reasons": uncertain,
        "hedge_outcome": "outcome_one" if hedge_one else "outcome_two",
        "legs": legs,
        "hedge_stake_base_units": str(spent), "hedge_return_base_units": str(got),
        "effective_taker_percentage_odds": str(spent * ODDS // got) if got else None,
        "best_taker_percentage_odds": str(best_q), "worst_acceptable_taker_percentage_odds": str(worst),
        "worst_acceptable_rounding": ladder_note,
        "after": {"pnl_base_units": {"if_outcome_one": a1, "if_outcome_two": a2},
                  "worst_case_base_units": min(a1, a2), "residual_gap_base_units": abs(a1 - a2)},
        "fees": {"fee_status": fee_info["status"], "provider_raw_rates": fee_info.get("raw"),
                 "hedge_taker_payout_fee_if_hedge_wins_base_units": pm.dstr(hedge_fee),
                 "position_projected_fee_base_units": {"if_outcome_one": pf1, "if_outcome_two": pf2},
                 "pnl_after_projected_fees_base_units": after_fee},
        "residual_risk": {"worst_case_after_base_units": min(a1, a2), "residual_gap_base_units": abs(a1 - a2),
                          "unhedged_return_base_units": str(max(0, target - got)) if mode != "REDUCE" else None},
        "liquidity": liquidity,
        "order_parameters_for_owner_review": {
            "executable": False, "signed": False, "submitted": False,
            "requires_owner_out_of_band_approval": True,
            "market_hash": h, "betting_outcome_one": hedge_one, "stake_base_units": str(spent),
            "percentage_odds_limit": str(worst),
            "note": "Taker frame: highest implied probability accepted. Validate with sxbet_simulate_order."},
    }
