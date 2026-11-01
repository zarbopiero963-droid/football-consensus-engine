"""SX Bet position manager: pure, deterministic roll-up of REST/WS account payloads.

Nothing here signs, submits or talks to the network. Every number this module
computes is ``DERIVED`` and sits beside the provider rows it came from, which are
echoed unchanged (governance §2.3). Missing stays missing (``None`` + reason).

Official model (docs.sx.bet, verified 01-11-2026):

* https://docs.sx.bet/developers/order-book and /developers/unit-conversions:
  markets are binary (outcome one / outcome two); ``percentageOdds`` is the
  implied probability x 1e20 of the party that receives it; a maker resting at
  ``p`` offers the taker ``1e20 - p``; a maker level of ``size`` can absorb
  ``size * (1e20 - p) / p`` of taker stake.
* https://docs.sx.bet/api-reference/get-trades-v3 / get-fills-v3 / channel-*:
  every bet row, maker or taker, is "stake on one outcome": ``totalStake`` /
  ``fillAmount`` is what was risked and ``totalReturn`` / ``returnAmount`` is the
  gross return *stake included* if that outcome wins (example: 333334 @ 40% ->
  833335 = stake * 1e20 / odds). Profit = return - stake; a loss costs the stake.
  This is why no Betfair back/lay formula is needed: on SX Bet "laying" outcome
  one IS backing outcome two, and the pot paid to the winner (maker stake + taker
  stake = makerStake * 1e20 / p) is the same number seen from either side.
* https://docs.sx.bet/developers/bet-lifecycle: MATCHED -> LOCKED -> SETTLED,
  MATCHED -> FAILED ("no funds moved"); settlement ``outcome`` 1 / 2 / 0 = void.
* https://docs.sx.bet/developers/which-grain: positions (per market, netted) >
  bets (/trades-v3, ``tradeId``) > fills (/fills-v3, ``id``/``matchId``).
* https://docs.sx.bet/api-reference/get-positions-v3: ``maxWin`` / ``maxLoss``
  are profits (stake excluded), ``isOutcomeOneMaxWin`` names the better case,
  ``pnl`` is realised profit, populated only for status=SETTLED.
* https://docs.sx.bet/developers/capital-efficiency: realised P&L =
  ``settleReturnAmount + ceRefundAmount - totalStake``.
* https://docs.sx.bet/developers/fees and /api-reference/get-user-fees-v3:
  payout fees are charged on profit, at settlement, only on a winning bet, at
  the maker or taker rate of the fill; ``null`` = unset, nothing charged.
* https://docs.sx.bet/api-reference/get-orders-v3 and /developers/order-lifecycle:
  open orders rest now; filled = ``totalBetSize - remainingSize``;
  PENDING -> ACTIVE -> INACTIVE (PENDING may skip ACTIVE).
* https://docs.sx.bet/developers/risk-limits: posting does not lock funds;
  resting size per market:outcome must fit the balance.
* https://docs.sx.bet/developers/balances-and-ledger and get-user-balance-v3.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any, Iterable, Mapping

SCHEMA = "sxbet.position_manager.v1"
ODDS = 10 ** 20
SOURCES = (
    "https://docs.sx.bet/developers/order-book",
    "https://docs.sx.bet/developers/unit-conversions",
    "https://docs.sx.bet/developers/which-grain",
    "https://docs.sx.bet/developers/bet-lifecycle",
    "https://docs.sx.bet/developers/fees",
    "https://docs.sx.bet/developers/capital-efficiency",
    "https://docs.sx.bet/developers/risk-limits",
    "https://docs.sx.bet/developers/balances-and-ledger",
    "https://docs.sx.bet/api-reference/get-positions-v3",
    "https://docs.sx.bet/api-reference/get-trades-v3",
    "https://docs.sx.bet/api-reference/get-fills-v3",
    "https://docs.sx.bet/api-reference/get-orders-v3",
    "https://docs.sx.bet/api-reference/channel-orders-v3",
    "https://docs.sx.bet/api-reference/channel-trades-v3",
    "https://docs.sx.bet/api-reference/channel-fills-v3",
)
# Points the documentation does not settle: reported, never computed around.
DOCS_UNVERIFIED = (
    "FEE_ROUNDING_TO_BASE_UNITS: projected fees are exact decimals; the exchange rounding rule is not documented",
    "SETTLE_RETURN_GROSS_OR_NET_OF_FEE: realised P&L uses the documented formula as is "
    "(settleReturnAmount + ceRefundAmount - totalStake); settleFeeAmount is reported, never subtracted again",
    "FILL_SETTLE_FEE_NULL: settlement.settleFeeAmount may be null at fill grain; its meaning is not documented, kept null",
    "SPENDABLE_BALANCE: developers/balances-and-ledger says available + pendingAvailable, "
    "api-reference/get-user-balance-v3 says availableAmount; the derived figure is the lower of the two",
)
OPEN = ("MATCHED", "LOCKED")
BET_STATUS = ("MATCHED", "LOCKED", "SETTLED", "FAILED")
_BET_MOVES = {("MATCHED", "LOCKED"), ("MATCHED", "FAILED"), ("MATCHED", "SETTLED"), ("LOCKED", "SETTLED")}
_ORDER_MOVES = {("PENDING", "ACTIVE"), ("PENDING", "INACTIVE"), ("ACTIVE", "INACTIVE")}
_UNITS = re.compile(r"^[0-9]{1,40}$")
_SIGNED = re.compile(r"^-?[0-9]{1,40}$")
_RATE = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,18})?$")
_WS = {"order": "account:orders_v3_#", "fill": "account:fills_v3_#", "trade": "account:trades_v3_#"}
FEE_FIELDS = ("makerPayoutFee", "takerPayoutFee", "makerParlayPayoutFee", "takerParlayPayoutFee", "refundFee")


# ---------------------------------------------------------------- parsing
def units(v: Any) -> int | None:
    """Non-negative base-unit decimal string (or int) -> int; anything else -> None (never 0)."""
    if type(v) is int and v >= 0:
        return v
    return int(v) if isinstance(v, str) and _UNITS.fullmatch(v) else None


def signed_units(v: Any) -> int | None:
    if type(v) is int:
        return v
    return int(v) if isinstance(v, str) and _SIGNED.fullmatch(v) else None


def odds(v: Any) -> int | None:
    o = units(v)
    return o if o is not None and 0 < o < ODDS else None


def timestamp(v: Any) -> datetime | None:
    if not isinstance(v, str) or not 10 <= len(v) <= 40:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def dstr(v: Decimal | None) -> str | None:
    if v is None:
        return None
    s = format(v.normalize(), "f")
    return "0" if s in ("-0", "") else s


def parse_fees(fees: Any) -> dict[str, Any]:
    """GET /user/fees-v3 -> rates. null = unset (provider: nothing charged): raw null kept, DERIVED 0 apart."""
    if fees is None:
        return {"status": "FEES_NOT_SUPPLIED", "raw": None, "derived_rates": None}
    if not isinstance(fees, dict):
        return {"status": "FEES_MALFORMED", "raw": None, "derived_rates": None}
    missing = [k for k in FEE_FIELDS if k not in fees]
    if missing:  # an absent key is not a provider null: no derived 0 for it
        return {"status": "FEES_MALFORMED", "reason": "FEE_FIELD_MISSING", "missing_fields": missing,
                "raw": {k: fees[k] for k in FEE_FIELDS if k in fees}, "derived_rates": None}
    derived, notes = {}, {}
    for k in FEE_FIELDS:
        v = fees[k]
        if v is None:
            derived[k] = "0"
            notes[k] = "PROVIDER_NULL_UNSET_NOTHING_CHARGED"
        elif isinstance(v, str) and _RATE.fullmatch(v) and Decimal(v) < 1:
            derived[k] = v
        else:
            return {"status": "FEES_MALFORMED", "raw": {k: fees.get(k) for k in FEE_FIELDS}, "derived_rates": None}
    return {"status": "FEES_OK", "raw": {k: fees.get(k) for k in FEE_FIELDS}, "derived_rates": derived,
            "derived_label": "DERIVED", "null_rate_notes": notes}


def _rows(payload: Any, key: str) -> tuple[list | None, bool]:
    """REST `data` (dict with `key`, optional nextKey) or a bare list -> (rows, more_pages)."""
    if payload is None:
        return None, False
    if isinstance(payload, list):
        return payload, False
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return payload[key], payload.get("nextKey") not in (None, "")
    return None, False


def _canon(row: Any) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def _lower(v: Any) -> str | None:
    return v.lower() if isinstance(v, str) and v else None


# ---------------------------------------------------------------- REST + WS reconciliation
def split_ws(messages: Any) -> tuple[dict[str, list], list[dict]]:
    """Account-channel publications -> rows per kind. Shapes per api-reference/channel-{orders,trades,fills}-v3:
    one object per message wrapped in `order` / `trade` / `fill`; `channel` (optional) must match the wrapper."""
    out: dict[str, list] = {"order": [], "fill": [], "trade": []}
    bad: list[dict] = []
    if messages is None:
        return out, bad
    if not isinstance(messages, list):
        return out, [{"index": None, "reason": "WS_MESSAGES_NOT_A_LIST"}]
    for i, m in enumerate(messages):
        data = m.get("data") if isinstance(m, dict) and isinstance(m.get("data"), dict) else m
        kinds = [k for k in _WS if isinstance(data, dict) and isinstance(data.get(k), dict)]
        if len(kinds) != 1:
            bad.append({"index": i, "reason": "WS_PAYLOAD_SHAPE_UNKNOWN"})
            continue
        channel = m.get("channel") if isinstance(m, dict) else None
        if channel is not None and not (isinstance(channel, str) and channel.startswith(_WS[kinds[0]])):
            bad.append({"index": i, "reason": "WS_CHANNEL_PAYLOAD_MISMATCH"})
            continue
        out[kinds[0]].append(data[kinds[0]])
    return out, bad


def reconcile(kind: str, rest: Iterable[Any], ws: Iterable[Any]) -> dict[str, Any]:
    """Dedupe by documented id, order versions by `updatedAt`, report conflicts instead of guessing.

    kind: "order" (key `id`), "trade" (key `tradeId`), "fill" (key `id` = matchId:orderId:marketHash).
    A key whose versions cannot be ordered, tie on `updatedAt` with different content, or move
    backwards in the documented lifecycle is a conflict: it is excluded from every computation.
    """
    id_field = "tradeId" if kind == "trade" else "id"
    groups: dict[str, list[tuple[str, Any]]] = {}
    malformed: list[dict] = []
    for source, rows in (("REST", rest), ("WS", ws)):
        for row in rows or []:
            key = _lower(row.get(id_field)) if isinstance(row, dict) else None
            if key is None:
                malformed.append({"source": source, "reason": "ROW_WITHOUT_ID", "raw": row})
                continue
            groups.setdefault(key, []).append((source, row))
    records: dict[str, dict] = {}
    conflicts: list[dict] = []
    duplicates = 0
    for key, items in groups.items():
        distinct: dict[str, tuple[str, Any]] = {}
        for source, row in items:
            c = _canon(row)
            if c in distinct:
                duplicates += 1
            else:
                distinct[c] = (source, row)
        versions = list(distinct.values())
        if len(versions) == 1:
            records[key] = {"row": versions[0][1], "sources": sorted({s for s, _ in items}), "versions": 1}
            continue
        stamped = [(timestamp(r.get("updatedAt")), s, r) for s, r in versions]
        if any(t is None for t, _, _ in stamped):
            conflicts.append({"kind": kind, "id": key, "reason": "UNORDERABLE_VERSIONS_UPDATED_AT_MISSING",
                              "versions": [{"source": s, "raw": r} for _, s, r in stamped]})
            continue
        stamped.sort(key=lambda x: x[0])
        if stamped[-1][0] == stamped[-2][0]:
            conflicts.append({"kind": kind, "id": key, "reason": "SAME_UPDATED_AT_DIFFERENT_CONTENT",
                              "versions": [{"source": s, "raw": r} for _, s, r in stamped]})
            continue
        reason = _lifecycle_violation(kind, [r for _, _, r in stamped])
        if reason:
            conflicts.append({"kind": kind, "id": key, "reason": reason,
                              "versions": [{"source": s, "raw": r} for _, s, r in stamped]})
            continue
        records[key] = {"row": stamped[-1][2], "sources": sorted({s for s, _ in items}), "versions": len(stamped)}
    return {"records": records, "conflicts": conflicts, "malformed": malformed, "exact_duplicates_dropped": duplicates}


def _lifecycle_violation(kind: str, ordered: list[dict]) -> str | None:
    moves = _ORDER_MOVES if kind == "order" else _BET_MOVES
    for a, b in zip(ordered, ordered[1:]):
        sa, sb = a.get("status"), b.get("status")
        if sa != sb and (sa, sb) not in moves:
            return f"STATUS_REGRESSION_{sa}_TO_{sb}"
        if kind == "order":
            ra, rb = units(a.get("remainingSize")), units(b.get("remainingSize"))
            if ra is None or rb is None or rb > ra:
                return "REMAINING_SIZE_INCREASED_OR_MALFORMED"
            if a.get("inactiveReason") is not None and b.get("inactiveReason") != a.get("inactiveReason"):
                return "INACTIVE_REASON_CHANGED"
        elif kind == "fill" and a.get("fillAmount") != b.get("fillAmount"):
            return "FILL_AMOUNT_CHANGED"
    return None


# ---------------------------------------------------------------- bet rows
def _bet(kind: str, key: str, row: Mapping[str, Any]) -> dict[str, Any] | None:
    stake = units(row.get("fillAmount" if kind == "fill" else "totalStake"))
    gross = units(row.get("returnAmount" if kind == "fill" else "totalReturn"))
    net = units(row.get("netReturnAmount" if kind == "fill" else "netTotalReturn"))
    side, maker, status = row.get("isBettingOutcomeOne"), row.get("isMaker"), row.get("status")
    # A gross return (stake included) below the stake would mean implied odds above 100%: malformed.
    if stake is None or gross is None or gross < stake or not isinstance(side, bool) or status not in BET_STATUS:
        return None
    settle = row.get("settlement") if isinstance(row.get("settlement"), dict) else None
    return {"grain": kind, "id": key, "trade_id": _lower(row.get("tradeId")),
            "market_hash": _lower(row.get("marketHash")), "outcome_one": side,
            "is_maker": maker if isinstance(maker, bool) else None, "status": status,
            "stake": stake, "gross_return": gross, "provider_net_return": net,
            "ce_refund": units(row.get("ceRefundAmount")), "ce_refund_fee": units(row.get("ceRefundFeeAmount")),
            "parlay": row.get("isParlay") is True, "quarterline": row.get("quarterlineMarketHash") not in (None, ""),
            "settlement": settle, "raw": row}


def _fee_rate(rates: Mapping[str, str] | None, is_maker: bool | None) -> Decimal | None:
    if rates is None or is_maker is None:
        return None
    return Decimal(rates["makerPayoutFee" if is_maker else "takerPayoutFee"])


# ---------------------------------------------------------------- market roll-up
def _market_from_bets(bets: list[dict], rates: Mapping[str, str] | None) -> dict[str, Any]:
    open_rows = [b for b in bets if b["status"] in OPEN]
    stake = {True: 0, False: 0}
    ret = {True: 0, False: 0}
    unlocked = 0
    fee: dict[bool, Decimal | None] = {True: Decimal(0), False: Decimal(0)}
    provider_fee: dict[bool, int | None] = {True: 0, False: 0}
    for b in open_rows:
        s = b["outcome_one"]
        stake[s] += b["stake"]
        ret[s] += b["gross_return"]
        unlocked += b["stake"] if b["status"] == "MATCHED" else 0
        rate = _fee_rate(rates, b["is_maker"])
        fee[s] = None if fee[s] is None or rate is None else fee[s] + rate * (b["gross_return"] - b["stake"])
        pf = None if b["provider_net_return"] is None else b["gross_return"] - b["provider_net_return"]
        provider_fee[s] = None if provider_fee[s] is None or pf is None else provider_fee[s] + pf
    total = stake[True] + stake[False]
    pnl1, pnl2 = ret[True] - total, ret[False] - total
    settled = [b for b in bets if b["status"] == "SETTLED"]
    realized, fees_charged, realized_reason = 0, 0, None
    for b in settled:
        st = b["settlement"] or {}
        back = units(st.get("settleReturnAmount"))
        if back is None or b["ce_refund"] is None:
            realized, realized_reason = None, "SETTLEMENT_FIELDS_MISSING"
            break
        realized += back + b["ce_refund"] - b["stake"]
    for b in settled:
        f = units((b["settlement"] or {}).get("settleFeeAmount"))
        fees_charged = None if fees_charged is None or f is None else fees_charged + f
    ce_fees = 0
    for b in bets:
        ce_fees = None if ce_fees is None or b["ce_refund_fee"] is None else ce_fees + b["ce_refund_fee"]
    net = {k: (None if fee[k] is None else Decimal(v) - fee[k]) for k, v in ((True, pnl1), (False, pnl2))}
    return {
        "open_bet_rows": len(open_rows), "settled_bet_rows": len(settled),
        "failed_bet_rows_excluded": sum(1 for b in bets if b["status"] == "FAILED"),
        "matched_stake_base_units": {"outcome_one": stake[True], "outcome_two": stake[False], "total": total},
        "matched_not_locked_stake_base_units": unlocked,
        "potential_gross_payout_base_units": {"if_outcome_one": ret[True], "if_outcome_two": ret[False],
                                              "if_void": total},
        "pnl_base_units": {"if_outcome_one": pnl1, "if_outcome_two": pnl2, "if_void": 0},
        "worst_case_liability_base_units": max(0, -min(pnl1, pnl2)) if open_rows else 0,
        "projected_payout_fee_base_units": {
            "if_outcome_one": dstr(fee[True]), "if_outcome_two": dstr(fee[False]), "if_void": "0",
            "reason": None if rates is not None and None not in fee.values() else
            ("FEES_UNAVAILABLE" if rates is None else "MAKER_TAKER_SIDE_MISSING_ON_SOME_ROWS")},
        "provider_projected_fee_base_units": {"if_outcome_one": provider_fee[True], "if_outcome_two": provider_fee[False],
                                              "source": "gross return - provider netReturnAmount/netTotalReturn"},
        "pnl_after_projected_fee_base_units": {"if_outcome_one": dstr(net[True]), "if_outcome_two": dstr(net[False]),
                                               "if_void": "0"},
        "realized_pnl_base_units": realized,
        "realized_pnl_reason": realized_reason if realized is None else None,
        "realized_formula": "settleReturnAmount + ceRefundAmount - stake (developers/capital-efficiency)",
        "settle_fees_charged_base_units": fees_charged,
        "ce_refund_fees_charged_base_units": ce_fees,
    }


def _market_from_position_row(p: Mapping[str, Any]) -> dict[str, Any]:
    """Only the provider's aggregated row: per-outcome P&L is documented, the stake split and fees are not."""
    win, loss, one_best = signed_units(p.get("maxWin")), signed_units(p.get("maxLoss")), p.get("isOutcomeOneMaxWin")
    total = units(p.get("totalStake"))
    pnl_reason = None
    if win is None or loss is None or not isinstance(one_best, bool):
        pnl = {"if_outcome_one": None, "if_outcome_two": None, "if_void": None}
        pnl_reason = "POSITION_ROW_FIELDS_MISSING"
    elif loss > 0 or loss > win:
        # Only a negative (or zero) maxLoss not above maxWin is documented; anything else is not guessed.
        pnl = {"if_outcome_one": None, "if_outcome_two": None, "if_void": None}
        pnl_reason = "MAXLOSS_SIGN_UNVERIFIED"
    else:
        pnl = {"if_outcome_one": win if one_best else loss, "if_outcome_two": loss if one_best else win, "if_void": 0}
    known = None not in (pnl["if_outcome_one"], pnl["if_outcome_two"])
    return {
        "matched_stake_base_units": {"outcome_one": None, "outcome_two": None, "total": total,
                                     "reason": "STAKE_SPLIT_NEEDS_BET_GRAIN"},
        "pnl_base_units": pnl, "pnl_reason": pnl_reason,
        "worst_case_liability_base_units": max(0, -min(pnl["if_outcome_one"], pnl["if_outcome_two"])) if known else None,
        "projected_payout_fee_base_units": {"if_outcome_one": None, "if_outcome_two": None,
                                            "reason": "FEE_PROJECTION_NEEDS_BET_GRAIN (maker/taker per bet)"},
        "provider_net_max_win_base_units": signed_units(p.get("netMaxWin")),
        "realized_pnl_base_units": signed_units(p.get("pnl")),
        "realized_pnl_reason": None if p.get("pnl") is not None else "PROVIDER_PNL_NULL_UNLESS_STATUS_SETTLED_ONLY",
        "realized_formula": "provider pnl (positions-v3, status=SETTLED)",
    }


def mark_to_market(pnl1: int | None, pnl2: int | None, best: Any) -> dict[str, Any]:
    """Equal-outcome P&L if the gap were hedged now at the documented top-of-book taker price.

    best: a row of GET /orders-v3/odds/best in the MAKER frame ({outcomeOne, outcomeTwo} levels or null).
    Gap g = pnl1 - pnl2 > 0 is closed by backing outcome two, i.e. taking makers resting on outcome one at
    taker price q = 1e20 - p; stake s = g * q / 1e20 makes both outcomes equal to pnl1 - s
    (derivation in docs/sxbet/README.md). Depth beyond the top level is NOT checked; fees excluded.
    """
    base = {"label": "DERIVED", "market_label": "HYBRID_MARKET_AWARE", "fees_included": False,
            "depth_checked": False, "value_base_units": None}
    if pnl1 is None or pnl2 is None:
        return {**base, "reason": "POSITION_PNL_MISSING"}
    if pnl1 == pnl2:
        return {**base, "value_base_units": pnl1, "reason": "POSITION_ALREADY_BALANCED"}
    if not isinstance(best, dict):
        return {**base, "reason": "BEST_ODDS_NOT_SUPPLIED"}
    gap, side = (pnl1 - pnl2, "outcomeOne") if pnl1 > pnl2 else (pnl2 - pnl1, "outcomeTwo")
    level = best.get(side)
    if level is None:
        return {**base, "reason": "NO_LIQUIDITY_ON_HEDGE_SIDE", "maker_side_read": side}
    p = odds(level.get("percentageOdds")) if isinstance(level, dict) else None
    size = units(level.get("size")) if isinstance(level, dict) else None
    if p is None or size is None:
        return {**base, "reason": "BEST_ODDS_MALFORMED"}
    q = ODDS - p
    stake = Fraction(gap * q, ODDS)
    high = max(pnl1, pnl2)
    value = Fraction(high) - stake
    capacity = size * q // p
    return {**base, "value_base_units": value.numerator // value.denominator,  # floor: conservative
            "hedge_outcome": "outcome_two" if side == "outcomeOne" else "outcome_one",
            "maker_side_read": side, "maker_percentage_odds": str(p), "taker_percentage_odds": str(q),
            "hedge_stake_base_units": str(-(-stake.numerator // stake.denominator)),
            "top_level_taker_capacity_base_units": str(capacity), "top_level_covers_hedge": capacity >= stake,
            "reason": None if capacity >= stake else "TOP_LEVEL_TOO_SMALL_VALUE_INDICATIVE_ONLY"}


# ---------------------------------------------------------------- capital
def capital(balance: Any, *, base_token: str | None = None, escrow: str | None = None) -> dict[str, Any]:
    rows, _ = _rows(balance, "balances")
    if rows is None:
        return {"status": "BALANCE_UNRESOLVED", "reason": "BALANCE_NOT_SUPPLIED_OR_MALFORMED"}
    if base_token and escrow:
        match = [r for r in rows if isinstance(r, dict) and _lower(r.get("tokenAddress")) == base_token.lower()
                 and _lower(r.get("escrowAddress")) == escrow.lower()]
        token_note = "EXACT_TOKEN_ESCROW_MATCH"
    else:
        match = [r for r in rows if isinstance(r, dict)]
        token_note = "TOKEN_NOT_VERIFIED_AGAINST_METADATA"
    if len(match) != 1:
        return {"status": "BALANCE_UNRESOLVED", "reason": "NO_UNIQUE_BALANCE_ROW", "rows": len(rows)}
    r = match[0]
    a, pa = units(r.get("availableAmount")), signed_units(r.get("pendingAvailableAmount"))
    e, pe = units(r.get("escrowedAmount")), signed_units(r.get("pendingEscrowAmount"))
    raw = {k: r.get(k) for k in ("tokenAddress", "escrowAddress", "availableAmount", "pendingAvailableAmount",
                                 "escrowedAmount", "pendingEscrowAmount")}
    if a is None:
        return {"status": "BALANCE_UNRESOLVED", "reason": "AVAILABLE_AMOUNT_MALFORMED", "provider_raw": raw}
    return {"status": "OK", "token_check": token_note, "provider_raw": raw, "label": "DERIVED",
            "available_conservative_base_units": a if pa is None else min(a, a + pa),
            "available_conservative_reason": "min(availableAmount, availableAmount + pendingAvailableAmount); docs differ",
            "committed_escrow_base_units": e, "pending_escrow_delta_base_units": pe}


# ---------------------------------------------------------------- entry point
def build_positions(*, positions: Any = None, trades: Any = None, fills: Any = None, open_orders: Any = None,
                    balance: Any = None, fees: Any = None, ws_messages: Any = None, best_odds: Any = None,
                    markets: Any = None, base_token: str | None = None, escrow: str | None = None,
                    settled_rows_read: bool = True) -> dict[str, Any]:
    """Roll up account payloads per selection / market / event / total. Pure and deterministic.

    Every argument is the provider `data` object (or its row list) exactly as read; None = not read.
    settled_rows_read=False: the caller read only open (MATCHED/LOCKED) rows, so realised P&L is
    missing (None, SETTLED_ROWS_NOT_READ), never 0.
    """
    pos_rows, pos_more = _rows(positions, "positions")
    trade_rows, trade_more = _rows(trades, "trades")
    fill_rows, fill_more = _rows(fills, "fills")
    order_rows, order_more = _rows(open_orders, "orders")
    ws, ws_bad = split_ws(ws_messages)
    fee_info = parse_fees(fees)
    rates = fee_info["derived_rates"]
    incomplete = [name for name, more in (("positions", pos_more), ("trades", trade_more), ("fills", fill_more),
                                          ("open_orders", order_more)) if more]
    malformed_inputs = [name for name, raw, rows in (("positions", positions, pos_rows), ("trades", trades, trade_rows),
                                                     ("fills", fills, fill_rows), ("open_orders", open_orders, order_rows))
                        if raw is not None and rows is None]

    rec_t = reconcile("trade", trade_rows, ws["trade"])
    rec_f = reconcile("fill", fill_rows, ws["fill"])
    rec_o = reconcile("order", order_rows, ws["order"])
    conflicts = rec_t["conflicts"] + rec_f["conflicts"] + rec_o["conflicts"]
    malformed = rec_t["malformed"] + rec_f["malformed"] + rec_o["malformed"]

    meta: dict[str, dict] = {}
    for m in (markets if isinstance(markets, list) else []):
        if isinstance(m, dict) and _lower(m.get("marketHash")):
            meta[_lower(m["marketHash"])] = m
    pos_by_market: dict[str, dict] = {}
    for p in pos_rows or []:
        h = _lower(p.get("marketHash")) if isinstance(p, dict) else None
        if h is None:
            malformed.append({"source": "REST", "reason": "POSITION_ROW_WITHOUT_MARKET", "raw": p})
            continue
        if h in pos_by_market:
            conflicts.append({"kind": "position", "id": h, "reason": "DUPLICATE_POSITION_ROW_FOR_MARKET"})
            continue
        pos_by_market[h] = p
        if isinstance(p.get("market"), dict):
            meta.setdefault(h, p["market"])

    bets: dict[str, dict[str, list]] = {}
    unsupported: list[dict] = []
    conflict_markets = set()
    excluded_markets = set()  # markets with a bet/order row left out of the computation: never complete
    for kind, rec in (("trade", rec_t), ("fill", rec_f)):
        for key, item in rec["records"].items():
            b = _bet(kind, key, item["row"])
            if b is None or b["market_hash"] is None:
                malformed.append({"source": ",".join(item["sources"]), "reason": f"{kind.upper()}_ROW_INCOMPLETE",
                                  "raw": item["row"]})
                h = _lower(item["row"].get("marketHash"))
                if h:
                    excluded_markets.add(h)
                continue
            if b["parlay"] or b["quarterline"]:
                unsupported.append({"grain": kind, "id": key, "market_hash": b["market_hash"],
                                    "reason": "PARLAY_OR_QUARTER_LINE_NOT_MODELLED"})
                excluded_markets.add(b["market_hash"])
                continue
            b["sources"] = item["sources"]
            bets.setdefault(b["market_hash"], {"trade": [], "fill": []})[kind].append(b)
    for c in conflicts:
        for v in c.get("versions", []):
            h = _lower(v["raw"].get("marketHash")) if isinstance(v.get("raw"), dict) else None
            if h:
                conflict_markets.add(h)
        if c["kind"] == "position":
            conflict_markets.add(c["id"])

    resting: dict[str, list] = {}
    inactive_seen = 0
    for key, item in rec_o["records"].items():
        o = item["row"]
        h = _lower(o.get("marketHash"))
        if o.get("status") not in ("PENDING", "ACTIVE"):
            inactive_seen += 1
            continue
        side, total, rem, p = o.get("isBettingOutcomeOne"), units(o.get("totalBetSize")), units(o.get("remainingSize")), odds(o.get("percentageOdds"))
        if h is None or not isinstance(side, bool) or total is None or rem is None or p is None or rem > total:
            malformed.append({"source": ",".join(item["sources"]), "reason": "ORDER_ROW_INCOMPLETE", "raw": o})
            if h:
                excluded_markets.add(h)
            continue
        resting.setdefault(h, []).append({"id": key, "outcome_one": side, "total": total, "remaining": rem, "odds": p,
                                          "status": o["status"], "event_id": o.get("eventId"), "raw": o})

    best_by_market: dict[str, Any] = {}
    best_rows = best_odds.get("bestOdds") if isinstance(best_odds, dict) else best_odds
    for r in best_rows if isinstance(best_rows, list) else []:
        if isinstance(r, dict) and _lower(r.get("marketHash")):
            best_by_market[_lower(r["marketHash"])] = r

    conflict_markets |= excluded_markets
    cap = capital(balance, base_token=base_token, escrow=escrow)
    available = cap.get("available_conservative_base_units")
    market_hashes = sorted(set(pos_by_market) | set(bets) | set(resting))
    markets_out, selections = [], []
    for h in market_hashes:
        grain_bets = bets.get(h, {"trade": [], "fill": []})
        trade_ids = {b["id"] for b in grain_bets["trade"]}
        fill_trade_ids = {b["trade_id"] for b in grain_bets["fill"]}
        cross: list[dict] = []
        if grain_bets["fill"] and trade_ids <= fill_trade_ids:
            grain, used = "FILLS", grain_bets["fill"]
            # Every trade must have exactly its documented fillCount of fills present, else the fill grain
            # under-counts: fall back to the trade grain and flag the market.
            for t in grain_bets["trade"]:
                count = rec_t["records"][t["id"]]["row"].get("fillCount")
                n = sum(1 for f in grain_bets["fill"] if f["trade_id"] == t["id"])
                if type(count) is not int or count != n:
                    cross.append({"check": "TRADE_FILL_COUNT_EQUALS_FILLS_PRESENT", "trade_id": t["id"],
                                  "result": "FILL_COUNT_MISSING" if type(count) is not int else "MISMATCH",
                                  "provider_fill_count": count, "fills_present": n})
                    conflict_markets.add(h)
            if cross:
                grain, used = "TRADES", grain_bets["trade"]
        elif grain_bets["trade"]:
            grain, used = "TRADES", grain_bets["trade"]
        else:
            grain, used = ("POSITION_ROW" if h in pos_by_market else "NONE"), []
        if used:
            summary = _market_from_bets(used, rates)
            if grain == "TRADES" and grain_bets["fill"] and not cross:
                cross.append({"check": "FILLS_DO_NOT_COVER_EVERY_TRADE", "result": "USED_TRADE_GRAIN"})
            for t in grain_bets["trade"] if grain == "FILLS" else []:
                fsum = sum(f["stake"] for f in grain_bets["fill"] if f["trade_id"] == t["id"])
                if fsum != t["stake"]:  # fillCount == fills present is guaranteed above
                    cross.append({"check": "TRADE_STAKE_EQUALS_SUM_OF_FILLS", "trade_id": t["id"], "result": "MISMATCH"})
                    conflict_markets.add(h)
            p = pos_by_market.get(h)
            if p is not None and units(p.get("totalStake")) is not None and summary["open_bet_rows"]:
                ok = units(p["totalStake"]) == summary["matched_stake_base_units"]["total"]
                cross.append({"check": "POSITION_TOTAL_STAKE_EQUALS_OPEN_BETS", "result": "MATCH" if ok else "MISMATCH",
                              "provider": p.get("totalStake"), "derived": summary["matched_stake_base_units"]["total"]})
                if not ok:
                    conflict_markets.add(h)
        elif grain == "POSITION_ROW":
            summary = _market_from_position_row(pos_by_market[h])
        else:
            # Only resting orders: no bet, so nothing is at stake and no payout fee can be charged.
            summary = {"pnl_base_units": {"if_outcome_one": 0, "if_outcome_two": 0, "if_void": 0},
                       "worst_case_liability_base_units": 0, "matched_stake_base_units": {"total": 0},
                       "projected_payout_fee_base_units": {"if_outcome_one": "0", "if_outcome_two": "0", "if_void": "0",
                                                           "reason": "NO_BET_ON_MARKET"}}
        if not settled_rows_read and grain != "POSITION_ROW":
            summary = {**summary, "realized_pnl_base_units": None, "realized_pnl_reason": "SETTLED_ROWS_NOT_READ"}
        rest = resting.get(h, [])
        rest_side = {True: sum(r["remaining"] for r in rest if r["outcome_one"]),
                     False: sum(r["remaining"] for r in rest if not r["outcome_one"])}
        rest_ret = {s: sum(r["remaining"] * ODDS // r["odds"] for r in rest if r["outcome_one"] is s) for s in (True, False)}
        pnl = summary["pnl_base_units"]
        known = pnl["if_outcome_one"] is not None and pnl["if_outcome_two"] is not None
        rest_total = rest_side[True] + rest_side[False]
        if_filled = ({"if_outcome_one": pnl["if_outcome_one"] + rest_ret[True] - rest_total,
                      "if_outcome_two": pnl["if_outcome_two"] + rest_ret[False] - rest_total} if known else None)
        m = meta.get(h, {})
        event = m.get("sportXeventId") or next((r["event_id"] for r in rest if r["event_id"]), None)
        entry = {
            "market_hash": h, "event_id": event, "grain_used": grain,
            "market": {k: m.get(k) for k in ("status", "outcomeOneName", "outcomeTwoName", "teamOneName", "teamTwoName",
                                              "gameTime", "line", "type", "sportXeventId")} if m else None,
            **summary,
            "resting_orders": {
                "count": len(rest), "remaining_base_units": {"outcome_one": rest_side[True], "outcome_two": rest_side[False]},
                "partially_filled": [{"id": r["id"], "filled_base_units": r["total"] - r["remaining"],
                                      "remaining_base_units": r["remaining"]} for r in rest if 0 < r["remaining"] < r["total"]],
                "capital_locked_by_resting": 0,
                "within_balance_per_market_outcome": None if available is None else {
                    "outcome_one": rest_side[True] <= available, "outcome_two": rest_side[False] <= available},
                "pnl_if_all_resting_filled_base_units": if_filled,
                "note": "Posting does not lock funds; only a match does (developers/risk-limits)."},
            "mark_to_market": mark_to_market(pnl["if_outcome_one"], pnl["if_outcome_two"], best_by_market.get(h))
            if (summary.get("open_bet_rows") or grain == "POSITION_ROW") else
            {"label": "DERIVED", "value_base_units": 0 if known else None, "reason": "NO_OPEN_POSITION"},
            "cross_checks": cross,
            "data_conflict": h in conflict_markets,
            "provider_rows": {"position": pos_by_market.get(h), "bets": [b["raw"] for b in used],
                              "orders": [r["raw"] for r in rest]},
        }
        markets_out.append(entry)
        names = (m.get("outcomeOneName"), m.get("outcomeTwoName")) if m else (None, None)
        st = summary.get("matched_stake_base_units", {})
        pg = summary.get("potential_gross_payout_base_units", {})
        for one, label, name in ((True, "outcome_one", names[0]), (False, "outcome_two", names[1])):
            selections.append({"market_hash": h, "selection": label, "outcome_name": name,
                               "matched_stake_base_units": st.get(label),
                               "potential_gross_payout_base_units": pg.get(f"if_{label}"),
                               "resting_remaining_base_units": rest_side[one]})

    events: dict[str, dict] = {}
    for e in markets_out:
        key = e["event_id"] or "UNKNOWN_EVENT"
        ev = events.setdefault(key, {"event_id": e["event_id"], "markets": 0, "worst_case_liability_base_units": 0,
                                     "matched_stake_base_units": 0})
        ev["markets"] += 1
        for f, src in (("worst_case_liability_base_units", e.get("worst_case_liability_base_units")),
                       ("matched_stake_base_units", (e.get("matched_stake_base_units") or {}).get("total"))):
            ev[f] = None if ev[f] is None or src is None else ev[f] + src

    def total(field_fn):
        acc = 0
        for e in markets_out:
            v = field_fn(e)
            if v is None:
                return None
            acc += v
        return acc

    mtm_vals = [e["mark_to_market"].get("value_base_units") for e in markets_out]
    return {
        "schema": SCHEMA, "label": "DERIVED", "sources": list(SOURCES), "docs_unverified": list(DOCS_UNVERIFIED),
        "inputs": {"positions_rows": None if pos_rows is None else len(pos_rows),
                   "trades_rows": None if trade_rows is None else len(trade_rows),
                   "fills_rows": None if fill_rows is None else len(fill_rows),
                   "open_orders_rows": None if order_rows is None else len(order_rows),
                   "ws_rows": {k: len(v) for k, v in ws.items()},
                   "balance_supplied": balance is not None, "fees_supplied": fees is not None,
                   "best_odds_rows": len(best_by_market)},
        "completeness": {"pages_not_read": incomplete, "malformed_inputs": malformed_inputs,
                         "excluded_rows": {"malformed": len(malformed), "unsupported": len(unsupported),
                                           "market_hashes": sorted(excluded_markets)},
                         "complete": not incomplete and not malformed_inputs and not malformed and not unsupported,
                         "note": "A source with more pages is reported, never silently treated as complete."},
        "reconciliation": {"conflicts": conflicts, "malformed_rows": malformed, "ws_rejected": ws_bad,
                           "exact_duplicates_dropped": rec_t["exact_duplicates_dropped"] + rec_f["exact_duplicates_dropped"]
                           + rec_o["exact_duplicates_dropped"], "inactive_orders_seen": inactive_seen,
                           "unsupported_rows": unsupported, "conflicting_markets": sorted(conflict_markets)},
        "fees": fee_info,
        "capital": cap,
        "markets": markets_out,
        "selections": selections,
        "events": list(events.values()),
        "totals": {
            "markets": len(markets_out),
            "matched_stake_base_units": total(lambda e: (e.get("matched_stake_base_units") or {}).get("total")),
            "worst_case_liability_base_units": total(lambda e: e.get("worst_case_liability_base_units")),
            "worst_case_note": "Sum of per-market worst cases (markets treated as independent: conservative).",
            "resting_remaining_base_units": sum(e["resting_orders"]["remaining_base_units"]["outcome_one"]
                                                + e["resting_orders"]["remaining_base_units"]["outcome_two"] for e in markets_out),
            "realized_pnl_base_units": None if not settled_rows_read else
            total(lambda e: e.get("realized_pnl_base_units") if e["grain_used"] != "NONE" else 0),
            "realized_pnl_reason": None if settled_rows_read else "SETTLED_ROWS_NOT_READ",
            "mark_to_market_base_units": None if None in mtm_vals else sum(mtm_vals),
            "mark_to_market_label": "HYBRID_MARKET_AWARE",
            "available_conservative_base_units": available,
            "committed_escrow_base_units": cap.get("committed_escrow_base_units"),
        },
        "data_conflict": bool(conflicts) or bool(conflict_markets),
    }
