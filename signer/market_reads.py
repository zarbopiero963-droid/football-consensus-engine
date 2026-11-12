"""The signer's network-shaped market reads (owner gate P2-B, issue #187): complete, canonical, fail-closed.

`RestMarketDataProvider` implements `signer.market_data.MarketDataProvider` over an INJECTED read function
`get(path, params) -> data` (the `data` field of an official SX Bet GET response). This package still
imports no network library and reads no API key: the transport (HTTPS through the shared rate coordinator)
is wired by the signer service PR. Only GET routes of the official OpenAPI are used:

    GET /markets/find?marketHashes=<h>                        the order's market row (exact hash, exactly one)
    GET /markets/active?eventId=<e>, every page               every market of the event
    GET /sports                                               the sports to scan (scan_sport_ids="ALL")
    GET /markets/active?sportIds=<s>, every page, per sport   every ACTIVE market of every scanned sport, any
                                                              league or date: the same-team scan of §2.3bis.
                                                              NOT onlyMainLine: its effect on market types
                                                              without lines is not stated by the contract
                                                              (DOCS_UNVERIFIED), so it could hide an event
                                                              (independent review of 98e7525, P1-1).
    GET /orderbook-v3/snapshot/market?showTakerPerspective=true   quote and liquidity at the order's limit
    GET /user/balance-v3                                      availableAmount of the exact (token, escrow) row
    GET /user/fees-v3                                         fee snapshot (null stays null)
    GET /metadata/obv3                                        runtime limits, token and escrow cross-checks

Owner gate P2-B: the provider reads EVERY page (`nextKey` -> `paginationKey` until no `nextKey`; the
contract says "a short page is not the last page, and the final nextKey can point at an empty page"),
normalises team names canonically (Unicode NFKD, accents and case folded, whitespace collapsed: the same
`canonical_label` the command verifier uses), and refuses on ANY missing page, repeated cursor, page
budget exhausted, malformed row, a market hash read twice with different identity/label/event fields
(CONFLICT_FIELDS), inconsistent rows of one
event, a /markets/find row that differs from the /markets/active row of the same hash, or a scan that does
not contain the order's own market or event. Nothing is fuzzy: equal canonical names or nothing (§2.3bis).
Every sport is scanned by default (`scan_sport_ids="ALL"`, review 98e7525 P2-1: the command names no
sport). Cost per order: one /markets/active page per 100 active markets of every scanned sport plus the
event pages; the transport the service injects MUST go through the shared rate coordinator (§2.4).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from .command import canonical_label

ReadFn = Callable[[str, Mapping[str, Any]], Any]
PAGE_SIZE = 100            # official maximum of /markets/active pageSize
DEFAULT_MAX_PAGES = 200    # budget per scan; exceeding it is a refusal (incomplete), never a partial answer
_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_UINT = re.compile(r"^[0-9]{1,78}$")
# Identity and label fields a /markets/find row must share with the /markets/active row of the same hash.
IDENTITY_FIELDS = ("marketHash", "sportXeventId", "teamOneName", "teamTwoName", "outcomeOneName", "outcomeTwoName",
                   "line", "type", "sportId", "leagueId", "gameTime", "status")
EVENT_FIELDS = ("sportXeventId", "teamOneName", "teamTwoName", "gameTime", "leagueId", "leagueLabel", "sportId")
CONFLICT_FIELDS = tuple(dict.fromkeys(IDENTITY_FIELDS + EVENT_FIELDS))
FEE_FIELDS = ("refundFee", "makerPayoutFee", "takerPayoutFee", "makerParlayPayoutFee", "takerParlayPayoutFee")


class MarketReadError(RuntimeError):
    """Fixed-code refusal; the signer turns any provider exception into SIGNER_MARKET_READS_FAILED."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _need(cond: bool, code: str) -> None:
    if not cond:
        raise MarketReadError(code)


def read_all_active_markets(get: ReadFn, params: Mapping[str, Any], *,
                            max_pages: int = DEFAULT_MAX_PAGES) -> list[dict[str, Any]]:
    """Every page of GET /markets/active for `params`; rows de-duplicated by marketHash. Fail closed."""
    rows: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    key: str | None = None
    for _ in range(max_pages):
        q = dict(params, pageSize=PAGE_SIZE)
        if key is not None:
            q["paginationKey"] = key
        data = get("/markets/active", q)
        _need(isinstance(data, Mapping) and isinstance(data.get("markets"), list), "MARKETS_PAGE_MALFORMED")
        for m in data["markets"]:
            _need(isinstance(m, Mapping) and isinstance(m.get("marketHash"), str)
                  and _HASH.fullmatch(m["marketHash"].lower()) is not None, "MARKETS_ROW_MALFORMED")
            h = m["marketHash"].lower()
            prev = rows.get(h)
            # Same market read twice with different identity / label / event data: refusal (review 98e7525 P3-2).
            # Other fields (e.g. mainLine moving during a long scan) do not refuse (review 4dc9981 P3-B).
            if prev is not None and any(prev.get(f) != m.get(f) for f in CONFLICT_FIELDS):
                raise MarketReadError("MARKETS_ROW_CONFLICT")
            rows[h] = dict(m)
        nxt = data.get("nextKey")
        if nxt is None or nxt == "":
            return list(rows.values())
        _need(isinstance(nxt, str) and nxt not in seen, "MARKETS_PAGINATION_LOOP")
        seen.add(nxt)
        key = nxt
    raise MarketReadError("MARKETS_PAGINATION_INCOMPLETE")  # page budget exhausted: never a partial scan


def _team_pair(row: Mapping[str, Any]) -> tuple[str, str] | None:
    one, two = row.get("teamOneName"), row.get("teamTwoName")
    if not isinstance(one, str) or not isinstance(two, str) or not one.strip() or not two.strip():
        return None
    return canonical_label(one), canonical_label(two)


def same_team_events(market: Mapping[str, Any], sport_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every active event whose canonical team names equal the market's, in either order, any league or date.

    Rows of one event must agree on teams, start time and league (else MARKETS_EVENT_INCONSISTENT); the
    order's own event must be in the scan (else SAME_TEAM_EVENTS_INCOMPLETE). A row without team names
    cannot be classified and is a refusal (MARKETS_TEAMS_MISSING): ambiguity is never silently dropped.
    """
    want = _team_pair(market)
    _need(want is not None, "MARKET_TEAMS_MISSING")
    events: dict[str, dict[str, Any]] = {}
    for r in sport_rows:
        pair = _team_pair(r)
        _need(pair is not None, "MARKETS_TEAMS_MISSING")
        if pair != want and pair != (want[1], want[0]):
            continue
        eid = r.get("sportXeventId")
        _need(isinstance(eid, str) and bool(eid), "MARKETS_EVENT_ID_MISSING")
        view = {f: r.get(f) for f in EVENT_FIELDS}
        prev = events.get(eid)
        if prev is not None and prev != view:
            raise MarketReadError("MARKETS_EVENT_INCONSISTENT")
        events[eid] = view
    _need(str(market.get("sportXeventId")) in events, "SAME_TEAM_EVENTS_INCOMPLETE")
    return [events[k] for k in sorted(events)]


class RestMarketDataProvider:
    """`MarketDataProvider` over official GET reads. `base_token` / `escrow` are the owner's signer config."""

    def __init__(self, get: ReadFn, *, base_token: str, escrow: str, max_pages: int = DEFAULT_MAX_PAGES,
                 scan_sport_ids: Any = "ALL"):
        _need(callable(get), "MARKET_READ_FUNCTION_MISSING")
        _need(isinstance(base_token, str) and _ADDRESS.fullmatch(base_token.lower()) is not None, "BASE_TOKEN_INVALID")
        _need(isinstance(escrow, str) and _ADDRESS.fullmatch(escrow.lower()) is not None, "ESCROW_INVALID")
        _need(type(max_pages) is int and 1 <= max_pages <= 10_000, "MAX_PAGES_INVALID")
        self._get = get
        self._token = base_token.lower()
        self._escrow = escrow.lower()
        self._max_pages = max_pages
        _need(scan_sport_ids == "ALL" or (isinstance(scan_sport_ids, (list, tuple)) and scan_sport_ids and all(
            type(x) is int and x > 0 for x in scan_sport_ids)), "SCAN_SPORT_IDS_INVALID")
        self._scan_sports = scan_sport_ids

    def read(self, *, market_hash: str, outcome_one: bool, percentage_odds: int, now: int) -> dict[str, Any]:
        _need(isinstance(market_hash, str) and _HASH.fullmatch(market_hash.lower()) is not None, "MARKET_HASH_INVALID")
        _need(isinstance(outcome_one, bool) and type(percentage_odds) is int and type(now) is int, "READ_ARGS_INVALID")
        h = market_hash.lower()
        found = self._get("/markets/find", {"marketHashes": [h]})
        _need(isinstance(found, list), "MARKET_FIND_MALFORMED")
        rows = [m for m in found if isinstance(m, Mapping) and isinstance(m.get("marketHash"), str)
                and m["marketHash"].lower() == h]
        _need(len(rows) == 1, "MARKET_NOT_FOUND" if not rows else "MARKET_AMBIGUOUS")
        market = dict(rows[0])
        event_id = market.get("sportXeventId")
        _need(isinstance(event_id, str) and bool(event_id), "MARKET_EVENT_ID_MISSING")
        sport_id = market.get("sportId")
        _need(isinstance(sport_id, int) and not isinstance(sport_id, bool), "MARKET_SPORT_MISSING")

        event_markets = read_all_active_markets(self._get, {"eventId": event_id}, max_pages=self._max_pages)
        own = [m for m in event_markets if m["marketHash"].lower() == h]
        _need(len(own) == 1, "EVENT_MARKETS_INCOMPLETE")
        _need(all(m.get("sportXeventId") == event_id for m in event_markets), "EVENT_MARKETS_FOREIGN_ROW")
        # Two official reads of the same market must agree (review 98e7525 P3-3).
        _need(all(own[0].get(f) == market.get(f) for f in IDENTITY_FIELDS), "MARKET_READS_DISAGREE")
        sports = self._sports_to_scan()
        _need(sport_id in sports, "MARKET_SPORT_NOT_SCANNED")
        sport_rows: list[dict[str, Any]] = []
        for sid in sports:
            sport_rows += read_all_active_markets(self._get, {"sportIds": [sid]}, max_pages=self._max_pages)
        _need(any(m["marketHash"].lower() == h for m in sport_rows), "SPORT_SCAN_INCOMPLETE")
        teams = same_team_events(market, sport_rows)

        quote = self._quote(h, outcome_one, percentage_odds, now)
        meta = self._get("/metadata/obv3", {})
        limits = self._limits(meta, now)
        balance = self._balance(self._get("/user/balance-v3", {}), now)
        fees = self._get("/user/fees-v3", {})
        _need(isinstance(fees, Mapping) and all(k in fees for k in FEE_FIELDS), "FEES_MALFORMED")
        return {"market": market, "market_observed_at": now, "event_markets": event_markets,
                "same_team_events": teams, "quote": quote, "balance": balance, "fees": dict(fees),
                "exchange_limits": limits}

    def _sports_to_scan(self) -> list[int]:
        if self._scan_sports != "ALL":
            return list(self._scan_sports)
        data = self._get("/sports", {})
        _need(isinstance(data, list) and data and all(isinstance(x, Mapping) and type(x.get("sportId")) is int
                                                     for x in data), "SPORTS_MALFORMED")
        ids = sorted({x["sportId"] for x in data})
        return ids

    def _quote(self, h: str, outcome_one: bool, limit: int, now: int) -> dict[str, Any]:
        book = self._get("/orderbook-v3/snapshot/market", {"marketHash": h, "showTakerPerspective": True})
        _need(isinstance(book, Mapping), "ORDER_BOOK_MALFORMED")
        _need(isinstance(book.get("marketHash"), str) and book["marketHash"].lower() == h, "ORDER_BOOK_MARKET_MISMATCH")
        levels = book.get("outcomeOne" if outcome_one else "outcomeTwo")
        _need(isinstance(levels, list), "ORDER_BOOK_SIDE_MISSING")
        prices, liquidity = [], 0
        for lv in levels:
            _need(isinstance(lv, Mapping) and isinstance(lv.get("percentageOdds"), str)
                  and isinstance(lv.get("size"), str) and _UINT.fullmatch(lv["percentageOdds"]) is not None
                  and _UINT.fullmatch(lv["size"]) is not None, "ORDER_BOOK_LEVEL_MALFORMED")
            p, size = int(lv["percentageOdds"]), int(lv["size"])
            prices.append(p)
            if p <= limit:
                liquidity += size
        # An empty side has no best price: None (the risk engine refuses QUOTE_PRICE_MISSING), never 0.
        return {"outcome": 1 if outcome_one else 2, "best_percentage_odds": str(min(prices)) if prices else None,
                "available_liquidity_base_units": str(liquidity), "observed_at": now,
                "perspective": "TAKER (showTakerPerspective=true)"}

    def _limits(self, meta: Any, now: int) -> dict[str, Any]:
        _need(isinstance(meta, Mapping), "METADATA_MALFORMED")
        asset, lim = meta.get("activeAsset"), meta.get("limits")
        _need(isinstance(asset, Mapping) and isinstance(lim, Mapping), "METADATA_MALFORMED")
        _need(isinstance(asset.get("baseToken"), str) and asset["baseToken"].lower() == self._token,
              "METADATA_BASE_TOKEN_MISMATCH")
        _need(isinstance(asset.get("escrowAddress"), str) and asset["escrowAddress"].lower() == self._escrow,
              "METADATA_ESCROW_MISMATCH")
        mn, mr = lim.get("orderSizeMinimumBaseUnits"), lim.get("minRestingOrderSizeBaseUnits")
        _need(isinstance(mn, str) and _UINT.fullmatch(mn) is not None and isinstance(mr, str)
              and _UINT.fullmatch(mr) is not None, "METADATA_LIMITS_MALFORMED")
        out = {"order_size_minimum_base_units": mn, "min_resting_order_size_base_units": mr, "observed_at": now}
        if type(asset.get("decimals")) is int:
            out["base_token_decimals"] = asset["decimals"]  # cross-check only (the signer config is the source)
        return out

    def _balance(self, data: Any, now: int) -> dict[str, Any]:
        rows = data.get("balances") if isinstance(data, Mapping) else None
        _need(isinstance(rows, list), "BALANCE_MALFORMED")
        match = [r for r in rows if isinstance(r, Mapping) and isinstance(r.get("tokenAddress"), str)
                 and isinstance(r.get("escrowAddress"), str) and r["tokenAddress"].lower() == self._token
                 and r["escrowAddress"].lower() == self._escrow]
        # No row (never funded) or several rows: unresolved, never a zero balance (§2.3).
        _need(len(match) == 1, "BALANCE_ROW_NOT_UNIQUE")
        amount = match[0].get("availableAmount")
        _need(isinstance(amount, str) and _UINT.fullmatch(amount) is not None, "BALANCE_MALFORMED")
        return {"available_base_units": amount, "observed_at": now}


__all__ = ["DEFAULT_MAX_PAGES", "MarketReadError", "PAGE_SIZE", "RestMarketDataProvider",
           "read_all_active_markets", "same_team_events"]
