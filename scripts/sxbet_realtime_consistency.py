"""Pure analysis for the SX Bet mainnet READ-ONLY audit (issue #187, R2; owner directive 16-11-2026).

No network, no secrets, no I/O: the live audit (scripts/sxbet_live_readonly_certification.py, run only by the owner
through the protected `sxbet-live-readonly` workflow) collects real data and these functions judge it. Every result
is PASS / PARTIAL / FAIL / NOT_TESTED / EMPTY_VALID with the reason; nothing missing is ever promoted to PASS.

Sources (https://docs.sx.bet, read 16-11-2026):
- realtime-reliability: at-least-once delivery inside the recovery window, `ctx.tags.messageId` for dedup,
  `wasRecovering` / `recovered` after a reconnect (recovered=false -> re-seed from REST);
- realtime-reference: per-namespace history (orderbook_v3_event / recent_trades_v3 / markets: 100-1000 msgs, 5 min;
  orderbook_v3: 1 msg; best_odds_v3: none);
- channel-recent-trades-v3: body {"trade": {...}}, the inner object is the GET /trades-v3/public row (tradeId);
- channel-orderbook-v3(-event): full book per market with a per-market `version` that only increases; apply a
  publication only when its version is strictly greater (book-versioning);
- testnet-and-mainnet / references: chain 4162, RPC https://rpc-rollup.sx.technology, USDC
  0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B (6 decimals), OBv3 escrow 0x890482680C3a0116aBB003B17e7D694D13a6c1eB;
- get-user-balance-v3 / balances-and-ledger: availableAmount (settled, not locked), escrowedAmount (locked behind
  open bets), pending* signed deltas; amounts are escrow accounting: they need not equal any token balance.
DOCS_UNVERIFIED and therefore never asserted as an equality: that the proxy's on-chain USDC equals any API field.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

MAINNET = {"chain_id": 4162, "rpc": "https://rpc-rollup.sx.technology",
           "usdc": "0x6629ce1cf35cc1329ebb4f63202f3f197b3f050b", "usdc_decimals": 6,
           "escrow": "0x890482680c3a0116abb003b17e7d694d13a6c1eb"}
RECOVERABLE_NAMESPACES = frozenset({"orderbook_v3_event", "recent_trades_v3", "markets", "main_line", "fixtures",
                                    "account"})
_DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,77})$")
_VERSION = re.compile(r"^[0-9]{1,64}$")


def namespace(channel: str) -> str:
    return channel.split(":", 1)[0]


def units(v: Any) -> int | None:
    return int(v) if isinstance(v, str) and _DECIMAL.fullmatch(v) else None


# ---------------------------------------------------------------- stream integrity (dedup, order, gaps)
def requires_message_id(channel: str) -> bool:
    """Book channels carry the full book with a per-market `version` (book-versioning): a client dedups them by
    version, so a missing messageId there is not a defect (review 9729fe1 P3). Every other channel: messageId."""
    return namespace(channel) not in ("orderbook_v3", "orderbook_v3_event")


def analyze_channel(pubs: Sequence[Mapping[str, Any]], *, require_message_id: bool = True) -> dict[str, Any]:
    """`pubs`: publications of ONE channel in arrival order, each {"offset": int|None, "message_id": str|None}.
    Duplicates are reported (at-least-once delivery makes them legal) and must be dropped by messageId; offsets of
    the unique publications must increase; a jump > 1 is a gap (missed publications)."""
    seen: set[str] = set()
    duplicates = no_id = 0
    offsets: list[int] = []
    for p in pubs:
        mid = p.get("message_id")
        if not isinstance(mid, str) or not mid:
            no_id += 1
        elif mid in seen:
            duplicates += 1
            continue
        else:
            seen.add(mid)
        off = p.get("offset")
        if type(off) is int:
            offsets.append(off)
    out_of_order = sum(1 for a, b in zip(offsets, offsets[1:]) if b <= a)
    gaps = [(a, b) for a, b in zip(offsets, offsets[1:]) if b > a + 1]
    if not pubs:
        outcome, reason = "EMPTY_VALID", "NO_PUBLICATION_IN_WINDOW"
    elif no_id and require_message_id:
        outcome, reason = "FAIL", "PUBLICATION_WITHOUT_MESSAGE_ID"
    elif len(offsets) < len(pubs) - duplicates or not offsets:
        outcome, reason = "PARTIAL", "OFFSETS_NOT_PROVIDED"
    elif out_of_order:
        outcome, reason = "FAIL", "OFFSETS_OUT_OF_ORDER"
    elif gaps:
        outcome, reason = "PARTIAL", "OFFSET_GAPS_IN_LIVE_STREAM"
    else:
        outcome, reason = "PASS", "UNIQUE_IDS_CONTIGUOUS_OFFSETS"
    return {"outcome": outcome, "reason": reason, "publications": len(pubs), "unique": len(seen),
            "duplicates_dropped": duplicates, "without_message_id": no_id, "out_of_order": out_of_order,
            "gaps": gaps[:20]}


def analyze_recovery(channel: str, *, before: Sequence[Mapping[str, Any]], resubscribe: Mapping[str, Any] | None,
                     replayed: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A deliberate reconnect with recovery from the last stream position. `resubscribe` is the subscribe result of
    the reconnect ({"was_recovering", "recovered", "epoch", "offset"}); `replayed` its publications; `after` the
    live ones that followed. PASS needs: recovery attempted and granted, no message id applied twice after dedup,
    and offsets contiguous from the last one seen before the disconnect."""
    if namespace(channel) not in RECOVERABLE_NAMESPACES:
        return {"outcome": "NOT_TESTED", "reason": "NAMESPACE_HAS_NO_RECOVERY_HISTORY"}
    if not resubscribe:
        return {"outcome": "FAIL", "reason": "NO_RESUBSCRIBE_RESULT"}
    if not before:
        return {"outcome": "NOT_TESTED", "reason": "NO_PUBLICATION_BEFORE_DISCONNECT"}
    was, rec = resubscribe.get("was_recovering"), resubscribe.get("recovered")
    if was is not True:
        return {"outcome": "FAIL", "reason": "RECOVERY_NOT_ATTEMPTED"}
    if rec is not True:
        # Documented outcome when history was pruned: re-seed from REST. Legal, but recovery is not proven.
        return {"outcome": "PARTIAL", "reason": "RECOVERY_NOT_GRANTED_RESEED_FROM_REST"}
    stream = analyze_channel([*before, *replayed, *after])
    last_before = max((p["offset"] for p in before if type(p.get("offset")) is int), default=None)
    first_next = min((p["offset"] for p in [*replayed, *after] if type(p.get("offset")) is int), default=None)
    # PASS needs positive evidence on both sides of the reconnect (review 9729fe1 P1): a recovered=true with nothing
    # delivered afterwards, or publications without ids / offsets, prove nothing.
    if stream["without_message_id"]:
        return {"outcome": "FAIL", "reason": "PUBLICATION_WITHOUT_MESSAGE_ID", "replayed": len(replayed)}
    if last_before is None:
        return {"outcome": "PARTIAL", "reason": "NO_OFFSET_BEFORE_DISCONNECT", "replayed": len(replayed)}
    if not replayed and not after:
        return {"outcome": "NOT_TESTED", "reason": "RECOVERED_NOTHING_DELIVERED_AFTER_RECONNECT", "replayed": 0}
    if first_next is None:
        return {"outcome": "PARTIAL", "reason": "NO_OFFSET_AFTER_RECONNECT", "replayed": len(replayed)}
    contiguous = first_next <= last_before + 1
    if stream["out_of_order"] or not contiguous:
        outcome, reason = "FAIL", "GAP_ACROSS_RECONNECT" if not contiguous else "OFFSETS_OUT_OF_ORDER"
    elif stream["gaps"]:
        outcome, reason = "PARTIAL", "OFFSET_GAPS_IN_LIVE_STREAM"
    else:
        outcome, reason = "PASS", "RECOVERED_DEDUPED_CONTIGUOUS"
    return {"outcome": outcome, "reason": reason, "replayed": len(replayed),
            "duplicates_dropped": stream["duplicates_dropped"], "last_offset_before": last_before,
            "first_offset_after": first_next}


# ---------------------------------------------------------------- REST vs realtime, same identifiers
def compare_trades(ws_trade_ids: Iterable[str], rest_trade_ids: Iterable[str], *, rest_complete: bool) -> dict:
    """recent_trades_v3:global vs GET /trades-v3/public read AFTER the listen window. The OpenAPI documents the
    REST tape as LOCKED bets only (taker side), and does not say when the channel publishes: a socket trade still
    MATCHED (or later FAILED) can be absent from REST. A miss is therefore PARTIAL, never FAIL (review 9729fe1 P2,
    DOCS_UNVERIFIED timing); PASS needs every socket tradeId on REST."""
    ws = [t for t in ws_trade_ids if isinstance(t, str) and t]
    rest = {t for t in rest_trade_ids if isinstance(t, str) and t}
    if not ws:
        return {"outcome": "NOT_TESTED", "reason": "NO_TRADE_ON_THE_SOCKET_IN_WINDOW", "ws": 0}
    missing = sorted(set(ws) - rest)
    if not missing:
        return {"outcome": "PASS", "reason": "EVERY_WS_TRADE_ID_IN_REST", "ws": len(set(ws)), "matched": len(set(ws))}
    return {"outcome": "PARTIAL",
            "reason": "MISSING_MAY_BE_NOT_YET_LOCKED" if rest_complete else "MISSING_REST_PAGE_INCOMPLETE",
            "ws": len(set(ws)), "matched": len(set(ws)) - len(missing), "missing": len(missing)}


def _version_key(v: Any) -> str | None:
    # book-versioning: "compare as strings" (review 9729fe1 P3: no length-first ordering).
    return v if isinstance(v, str) and _VERSION.fullmatch(v) else None


def compare_book_versions(ws_books: Sequence[Mapping[str, Any]], rest_book: Mapping[str, Any] | None) -> dict:
    """orderbook_v3(_event) publications for ONE market vs GET /orderbook-v3/snapshot/market read after them.
    Per-market versions must strictly increase on the socket (a non-increasing one must be ignored by a client) and
    the REST snapshot must not be older than the last socket version. Equal versions must carry the same levels."""
    versions = [_version_key(b.get("version")) for b in ws_books]
    if not ws_books:
        return {"outcome": "NOT_TESTED", "reason": "NO_BOOK_PUBLICATION_IN_WINDOW"}
    if None in versions:
        return {"outcome": "FAIL", "reason": "BOOK_VERSION_MALFORMED"}
    stale = sum(1 for a, b in zip(versions, versions[1:]) if b <= a)
    if not rest_book:
        return {"outcome": "PARTIAL", "reason": "REST_SNAPSHOT_NOT_READ", "stale_publications": stale}
    rv = _version_key(rest_book.get("version"))
    if rv is None:
        return {"outcome": "FAIL", "reason": "REST_VERSION_MALFORMED"}
    last = max(versions)
    if rv < last:
        return {"outcome": "FAIL", "reason": "REST_SNAPSHOT_OLDER_THAN_SOCKET", "stale_publications": stale}
    if rv == last:
        ws_last = next(b for b, k in zip(reversed(ws_books), reversed(versions)) if k == last)
        pairs = [(_levels(ws_last.get(s)), _levels(rest_book.get(s))) for s in ("outcomeOne", "outcomeTwo")]
        if any(a is None or b is None for a, b in pairs):
            return {"outcome": "PARTIAL", "reason": "LEVELS_MISSING", "stale_publications": stale}  # P3: not vacuous
        same = all(a == b for a, b in pairs)
        return {"outcome": "PASS" if same else "FAIL",
                "reason": "SAME_VERSION_SAME_LEVELS" if same else "SAME_VERSION_DIFFERENT_LEVELS",
                "stale_publications": stale}
    return {"outcome": "PASS", "reason": "REST_NEWER_THAN_LAST_SOCKET_VERSION", "stale_publications": stale}


def _levels(levels: Any) -> list[tuple[str, str]] | None:
    if not isinstance(levels, list):
        return None
    return [(str(lv.get("percentageOdds")), str(lv.get("size"))) for lv in levels if isinstance(lv, Mapping)]


# ---------------------------------------------------------------- independent on-chain balance
def classify_balances(*, api_rows: Sequence[Mapping[str, Any]] | None, proxy: str | None, eoa: str | None,
                      onchain: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the USDC escrow accounting of GET /user/balance-v3 with independent on-chain reads.

    `onchain`: {"chain_id", "usdc_decimals", "proxy_usdc", "eoa_usdc"} read from the official RPC (ints or None).
    The API amounts are escrow accounting (available / escrowed / pending deltas): an equality with a token balance
    is NOT documented, so a difference is explained and classified, never declared an anomaly by itself.
    FAIL only for what the documentation does pin: the chain, the token decimals, the row's token and escrow."""
    if onchain.get("chain_id") != MAINNET["chain_id"]:
        return {"outcome": "FAIL", "reason": "RPC_CHAIN_ID_NOT_4162"}
    if onchain.get("usdc_decimals") != MAINNET["usdc_decimals"]:
        return {"outcome": "FAIL", "reason": "USDC_DECIMALS_NOT_6"}
    if api_rows is None or proxy is None:
        return {"outcome": "NOT_TESTED", "reason": "API_BALANCE_OR_PROXY_NOT_READ"}
    proxy_usdc, eoa_usdc, escrow_usdc = onchain.get("proxy_usdc"), onchain.get("eoa_usdc"), onchain.get("escrow_usdc")
    rows = [r for r in api_rows if isinstance(r, Mapping)
            and str(r.get("tokenAddress", "")).lower() == MAINNET["usdc"]]
    if not rows:
        if type(proxy_usdc) is int and proxy_usdc > 0:
            # On-chain funds the API does not account for (review 9729fe1 P2): never "empty".
            return {"outcome": "PARTIAL", "reason": "PROXY_FUNDED_BUT_NO_API_USDC_ROW"}
        return {"outcome": "EMPTY_VALID" if not api_rows else "PARTIAL",
                "reason": "NO_USDC_ROW" if not api_rows else "NO_ROW_FOR_THE_DOCUMENTED_USDC"}
    if len(rows) != 1 or str(rows[0].get("escrowAddress", "")).lower() != MAINNET["escrow"]:
        return {"outcome": "FAIL", "reason": "USDC_ROW_NOT_ON_THE_DOCUMENTED_ESCROW"}
    r = rows[0]
    if str(r.get("wallet", "")).lower() != proxy.lower():
        return {"outcome": "FAIL", "reason": "BALANCE_ROW_WALLET_IS_NOT_THE_PROXY"}
    available, escrowed = units(r.get("availableAmount")), units(r.get("escrowedAmount"))
    pend_a, pend_e = _signed(r.get("pendingAvailableAmount")), _signed(r.get("pendingEscrowAmount"))
    if None in (available, escrowed, pend_a, pend_e) or type(proxy_usdc) is not int:
        return {"outcome": "PARTIAL", "reason": "AMOUNT_NOT_READABLE"}
    if available == escrowed == pend_a == pend_e == proxy_usdc == 0:
        return {"outcome": "EMPTY_VALID", "reason": "ALL_ZERO_NOTHING_TO_COMPARE"}   # review 9729fe1 P2: no PASS
    held = available + escrowed
    if proxy_usdc == available:
        relation = "PROXY_TOKEN_EQUALS_AVAILABLE"
    elif proxy_usdc == held:
        relation = "PROXY_TOKEN_EQUALS_AVAILABLE_PLUS_ESCROWED"
    elif proxy_usdc == available + pend_a:
        relation = "PROXY_TOKEN_EQUALS_AVAILABLE_PLUS_PENDING"
    elif proxy_usdc == 0 and held > 0:
        # A hypothesis, checked only as far as the chain allows: the (shared) escrow contract must hold at least
        # what the API attributes to this account (a lower bound, never an equality).
        relation = ("PROXY_TOKEN_ZERO_ESCROW_HOLDS_AT_LEAST_THE_API_AMOUNT"
                    if type(escrow_usdc) is int and escrow_usdc >= held else "EXPLANATION_UNVERIFIED")
    else:
        relation = "DIFFERENT"
    explained = relation not in ("DIFFERENT", "EXPLANATION_UNVERIFIED")
    return {"outcome": "PASS" if explained else "PARTIAL",
            "reason": relation if explained else ("EXPLANATION_UNVERIFIED" if relation == "EXPLANATION_UNVERIFIED"
                                                  else "DIFFERENCE_NOT_EXPLAINED_BY_DOCUMENTED_FIELDS"),
            "api": {"available": str(available), "escrowed": str(escrowed), "pending_available": str(pend_a),
                    "pending_escrow": str(pend_e)},
            "onchain": {"proxy_usdc": str(proxy_usdc), "eoa_usdc": None if eoa_usdc is None else str(eoa_usdc),
                        "escrow_usdc": None if escrow_usdc is None else str(escrow_usdc)},
            "difference_proxy_minus_available": str(proxy_usdc - available),
            "note": ("API amounts are escrow accounting; the documentation pins no equality with a token balance. "
                     "A difference is reported with its likely cause, not as an anomaly by itself.")}


def _signed(v: Any) -> int | None:
    if isinstance(v, str) and v.startswith("-"):
        u = units(v[1:])
        return None if u is None else -u
    return units(v)


__all__ = ["MAINNET", "analyze_channel", "analyze_recovery", "classify_balances", "compare_book_versions",
           "compare_trades", "namespace", "requires_message_id", "units"]
