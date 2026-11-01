"""READ_ONLY and SIMULATION SX Bet MCP tools (`sxbet_*`).

Every READ_ONLY tool maps 1:1 to a GET route of the official OpenAPI
(https://docs.sx.bet/openapi.json). Parameter names, enums and bounds come from
that spec; nothing is invented. `sxbet_simulate_order` (SIMULATION) only
composes GET reads with the pure functions of `simulation.py`: it never signs,
never holds a key and never submits. The DERIVED tools (`sxbet_order_status`,
`sxbet_positions_summary`, `sxbet_exposure_pnl`, `sxbet_resting_orders_scope`,
`sxbet_hedge_simulation`, `sxbet_close_position_preview`, `sxbet_rules_evaluate`,
`sxbet_risk_status`, `sxbet_reconciliation_diagnostics`) compose the same GET
client with the pure modules `positions.py`, `hedging.py` and `rules.py`; their
outputs are labelled DERIVED beside the echoed provider rows, and none of them
can sign, submit or cancel. There is no write tool: LIVE_TRADING stays
UNAVAILABLE; signing lives in the separate top-level `signer/` package, which
nothing under `bridge/` imports (governance §2.6).
"""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from . import hedging as hedge_mod
from . import positions as pos_mod
from . import rules as rules_mod
from . import simulation as sim
from .client import NO_NETWORK_CODES, SXBetError, SXBetReadClient, Settings
from .ratelimit import DOCS as RATE_LIMIT_DOCS

DOCS = "https://docs.sx.bet/openapi.json"
SIMULATION_DOCS = ("https://docs.sx.bet/developers/posting-orders", "https://docs.sx.bet/developers/odds-rounding",
                   "https://docs.sx.bet/developers/time-in-force", "https://docs.sx.bet/developers/taking-liquidity")
_MAX_MCP_BYTES = 262_144
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_EVENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CURSOR = re.compile(r"^[\x21-\x7e]{1,512}$")
_DATE = re.compile(r"^[0-9TtZz:+.\-]{1,40}$")
_TRADE_STATUS = ("MATCHED", "LOCKED", "SETTLED", "FAILED")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")      # OpenAPI: maxLength 64, ^[A-Za-z0-9_-]+$
_PATH_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")   # opaque ids placed in a URL path: path-safe only
_UNITS = ("Units: percentageOdds = implied probability x 10^20 (e.g. 52500000000000000000 = 52.5%); "
          "USDC amounts in base units with 6 decimals (1000000 = 1 USDC). Provider data returned as received.")


class InvalidInput(ValueError):
    pass


def _hash(v, name="market_hash"):
    if not isinstance(v, str) or not _HASH.fullmatch(v):
        raise InvalidInput(f"INVALID_{name.upper()}")
    return v


def _hashes(v, maximum=30):
    if not isinstance(v, list) or not 1 <= len(v) <= maximum:
        raise InvalidInput("INVALID_MARKET_HASHES_COUNT")
    return [_hash(x, "market_hashes") for x in v]


def _event(v):
    if not isinstance(v, str) or not _EVENT.fullmatch(v):
        raise InvalidInput("INVALID_EVENT_ID")
    return v


def _events(v, maximum=30):
    if not isinstance(v, list) or not 1 <= len(v) <= maximum:
        raise InvalidInput("INVALID_EVENT_IDS_COUNT")
    return [_event(x) for x in v]


def _int(v, name, lo=1, hi=2_000_000_000):
    if type(v) is not int or not lo <= v <= hi:
        raise InvalidInput(f"INVALID_{name.upper()}")
    return v


def _address(v, name):
    if not isinstance(v, str) or not _ADDRESS.fullmatch(v):
        raise InvalidInput(f"INVALID_{name.upper()}")
    return v


def _cursor(v):
    if not isinstance(v, str) or not _CURSOR.fullmatch(v):
        raise InvalidInput("INVALID_CURSOR")
    return v


def _enum(v, allowed, name):
    if v not in allowed:
        raise InvalidInput(f"INVALID_{name.upper()}")
    return v


def _date(v):
    """ISO-8601 date-time with an explicit offset or Z (OpenAPI format: date-time)."""
    if not isinstance(v, str) or not _DATE.fullmatch(v):
        raise InvalidInput("INVALID_DATE")
    try:
        parsed = datetime.fromisoformat(v.replace("z", "Z").replace("Z", "+00:00"))
    except ValueError:
        raise InvalidInput("INVALID_DATE") from None
    if parsed.tzinfo is None:
        raise InvalidInput("DATE_TIMEZONE_REQUIRED")
    return v


def _date_range(start, end):
    s = None if start is None else _date(start)
    e = None if end is None else _date(end)
    if s and e and datetime.fromisoformat(e.replace("Z", "+00:00")) < datetime.fromisoformat(s.replace("Z", "+00:00")):
        raise InvalidInput("END_DATE_BEFORE_START_DATE")
    return s, e


def _bool(v, name):
    if not isinstance(v, bool):
        raise InvalidInput(f"INVALID_{name.upper()}")
    return v


def register_sxbet_tools(server: Any, settings: Settings, *, client: SXBetReadClient | None = None) -> tuple[str, ...]:
    if not settings.enabled:
        return ()
    client = client or SXBetReadClient(settings)
    names: list[str] = []

    def add(fn):
        server.tool()(fn)
        names.append(fn.__name__)
        return fn

    async def call(tool: str, path: str, build, *, auth: bool) -> dict[str, Any]:
        at = datetime.now(timezone.utc).isoformat()
        base = {"provider": "SX_BET", "tool": tool, "endpoint": path, "network": settings.network,
                "mode": "READ_ONLY", "source_contract": DOCS, "retrieved_at_utc": at,
                "network_calls_made": 0, "data": None}
        try:
            params = {k: v for k, v in build().items() if v is not None}
        except InvalidInput as exc:
            return {**base, "status": "INVALID_INPUT", "error": {"code": str(exc)}}
        if auth and not settings.api_key:
            return {**base, "status": "KEY_NOT_CONFIGURED", "parameters": params,
                    "error": {"code": "SXBET_KEY_NOT_CONFIGURED"}}
        try:
            reply = await client.get(path, params, auth=auth)
        except SXBetError as exc:
            out = {**base, "status": "PROVIDER_ERROR", "parameters": params,
                   "network_calls_made": 0 if exc.code in NO_NETWORK_CODES else 1,
                   "http_status": exc.http_status, "error": {"code": exc.code}}
            if exc.retry_after is not None:
                out["error"]["retry_after_seconds"] = exc.retry_after
            return out
        data = reply["data"]
        result = {**base, "status": "REAL_API_RESPONSE", "parameters": params, "http_status": reply["http_status"],
                  "network_calls_made": 1, "rate_limit_remaining": reply["rate_limit_remaining"],
                  "next_key": data.get("nextKey") if isinstance(data, dict) else None,
                  "data": data, "warnings": [_UNITS]}
        if len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > _MAX_MCP_BYTES:
            # Never truncate silently: ask for a smaller page instead.
            return {**base, "status": "PAYLOAD_TOO_LARGE", "parameters": params, "network_calls_made": 1,
                    "http_status": reply["http_status"],
                    "error": {"code": "SXBET_RESPONSE_EXCEEDS_MCP_SIZE_LIMIT",
                              "hint": "Use a smaller page_size/per_page or narrower filters."}}
        return result

    @add
    async def sxbet_health() -> dict[str, Any]:
        """SX Bet module status; no provider call. Live trading is unavailable (read-only + simulation module)."""
        return {"provider": "SX_BET", "enabled": True, "network": settings.network,
                "api_key_present": bool(settings.api_key), "mode": "READ_ONLY",
                "available_modes": {"READ_ONLY": "AVAILABLE", "SIMULATION": "AVAILABLE", "LIVE_TRADING": "UNAVAILABLE"},
                "live_trading": "UNAVAILABLE: no isolated signer with signer-verified per-order confirmation (governance 2.6)",
                "write_tools": [], "simulation_tools": ["sxbet_simulate_order", "sxbet_hedge_simulation",
                                                         "sxbet_close_position_preview", "sxbet_rules_evaluate"],
                "derived_read_tools": ["sxbet_order_status", "sxbet_positions_summary", "sxbet_exposure_pnl",
                                       "sxbet_resting_orders_scope", "sxbet_risk_status",
                                       "sxbet_reconciliation_diagnostics"],
                "local_rate_limit_per_minute": settings.max_rpm,
                "rate_coordinator": getattr(getattr(client, "_limiter", None), "kind", "CUSTOM"),
                "rate_limit_contract": RATE_LIMIT_DOCS,
                "source_contract": DOCS, "network_calls_made": 0}

    # ---- public routes ----
    @add
    async def sxbet_sports() -> dict[str, Any]:
        """List sports (GET /sports)."""
        return await call("sxbet_sports", "/sports", lambda: {}, auth=False)

    @add
    async def sxbet_leagues(sport_id: int | None = None, active_only: bool = True) -> dict[str, Any]:
        """List leagues, optionally for one sport (GET /leagues/active or /leagues)."""
        if not isinstance(active_only, bool):
            return await call("sxbet_leagues", "/leagues/active", lambda: _raise("INVALID_ACTIVE_ONLY"), auth=False)
        path = "/leagues/active" if active_only else "/leagues"
        return await call("sxbet_leagues", path,
                          lambda: {"sportId": None if sport_id is None else _int(sport_id, "sport_id")}, auth=False)

    @add
    async def sxbet_fixtures_active(league_id: int) -> dict[str, Any]:
        """Active fixtures for a league (GET /fixture/active, leagueId required)."""
        return await call("sxbet_fixtures_active", "/fixture/active",
                          lambda: {"leagueId": _int(league_id, "league_id")}, auth=False)

    @add
    async def sxbet_fixture_status(sportx_event_ids: list[str]) -> dict[str, Any]:
        """Status of up to 30 events (GET /fixture/status)."""
        return await call("sxbet_fixture_status", "/fixture/status",
                          lambda: {"sportXEventIds": _events(sportx_event_ids)}, auth=False)

    @add
    async def sxbet_live_scores(sportx_event_ids: list[str]) -> dict[str, Any]:
        """Live scores for up to 30 events (GET /live-scores)."""
        return await call("sxbet_live_scores", "/live-scores",
                          lambda: {"sportXEventIds": _events(sportx_event_ids)}, auth=False)

    @add
    async def sxbet_markets_active(sport_ids: list[int] | None = None, league_id: int | None = None,
                                   event_id: str | None = None, only_main_line: bool | None = None,
                                   live_only: bool | None = None, bet_group: str | None = None,
                                   type: list[int] | None = None, page_size: int | None = None,
                                   pagination_key: str | None = None, game_time: int | None = None) -> dict[str, Any]:
        """Active markets (GET /markets/active). Paginate with next_key -> pagination_key; a short page is not the last page. Use type OR bet_group, not both."""
        def build():
            if type is not None and bet_group is not None:
                raise InvalidInput("TYPE_AND_BET_GROUP_EXCLUSIVE")
            return {
                "sportIds": None if sport_ids is None else [
                    _int(x, "sport_ids") for x in (sport_ids if isinstance(sport_ids, list) and 1 <= len(sport_ids) <= 50
                                                   else _raise("INVALID_SPORT_IDS"))],
                "leagueId": None if league_id is None else _int(league_id, "league_id"),
                "eventId": None if event_id is None else _event(event_id),
                "onlyMainLine": None if only_main_line is None else _bool(only_main_line, "only_main_line"),
                "liveOnly": None if live_only is None else _bool(live_only, "live_only"),
                "betGroup": None if bet_group is None else (bet_group if isinstance(bet_group, str) and _EVENT.fullmatch(bet_group) else _raise("INVALID_BET_GROUP")),
                "type": None if type is None else [
                    _int(x, "type") for x in (type if isinstance(type, list) and 1 <= len(type) <= 50 else _raise("INVALID_TYPE"))],
                "pageSize": None if page_size is None else _int(page_size, "page_size", 1, 100),
                "paginationKey": None if pagination_key is None else _cursor(pagination_key),
                "gameTime": None if game_time is None else _int(game_time, "game_time"),
            }
        return await call("sxbet_markets_active", "/markets/active", build, auth=False)

    @add
    async def sxbet_markets_find(market_hashes: list[str]) -> dict[str, Any]:
        """Markets by hash, up to 30 (GET /markets/find); unmatched hashes are skipped by the provider."""
        return await call("sxbet_markets_find", "/markets/find",
                          lambda: {"marketHashes": _hashes(market_hashes)}, auth=False)

    @add
    async def sxbet_markets_popular() -> dict[str, Any]:
        """Popular markets (GET /markets/popular)."""
        return await call("sxbet_markets_popular", "/markets/popular", lambda: {}, auth=False)

    @add
    async def sxbet_metadata() -> dict[str, Any]:
        """Order-book V3 metadata: EIP-712 domain, base token, odds ladder step, size limits (GET /metadata/obv3)."""
        return await call("sxbet_metadata", "/metadata/obv3", lambda: {}, auth=False)

    @add
    async def sxbet_search(query: str) -> dict[str, Any]:
        """Search fixtures/teams/leagues (GET /search)."""
        def build():
            if not isinstance(query, str) or not 1 <= len(query) <= 100 or any(ord(c) < 32 for c in query):
                raise InvalidInput("INVALID_QUERY")
            return {"query": query}
        return await call("sxbet_search", "/search", build, auth=False)

    @add
    async def sxbet_teams(sport_id: int | None = None, id: int | None = None, page: int | None = None,
                          per_page: int | None = None) -> dict[str, Any]:
        """Teams (GET /teams); per_page max 500."""
        return await call("sxbet_teams", "/teams", lambda: {
            "sportId": None if sport_id is None else _int(sport_id, "sport_id"),
            "id": None if id is None else _int(id, "id"),
            "page": None if page is None else _int(page, "page", 0, 100_000),
            "perPage": None if per_page is None else _int(per_page, "per_page", 1, 500)}, auth=False)

    @add
    async def sxbet_public_trades(market_hash: str | None = None, event_id: str | None = None,
                                  per_page: int | None = None, next_key: str | None = None) -> dict[str, Any]:
        """Public trade tape (GET /trades-v3/public); per_page 1..100; paginate with next_key."""
        return await call("sxbet_public_trades", "/trades-v3/public", lambda: {
            "marketHash": None if market_hash is None else _hash(market_hash),
            "eventId": None if event_id is None else _event(event_id),
            "perPage": None if per_page is None else _int(per_page, "per_page", 1, 100),
            "nextKey": None if next_key is None else _cursor(next_key)}, auth=False)

    # ---- authenticated READ routes (x-sx-api-key) ----
    @add
    async def sxbet_orderbook_market(market_hash: str, show_taker_perspective: bool | None = None) -> dict[str, Any]:
        """Order book snapshot for one market (GET /orderbook-v3/snapshot/market). Requires API key."""
        return await call("sxbet_orderbook_market", "/orderbook-v3/snapshot/market", lambda: {
            "marketHash": _hash(market_hash),
            "showTakerPerspective": None if show_taker_perspective is None else _bool(show_taker_perspective, "show_taker_perspective")},
            auth=True)

    @add
    async def sxbet_orderbook_event(event_id: str, next_key: str | None = None,
                                    show_taker_perspective: bool | None = None) -> dict[str, Any]:
        """Order book snapshots for all markets of an event (GET /orderbook-v3/snapshot/event). Requires API key."""
        return await call("sxbet_orderbook_event", "/orderbook-v3/snapshot/event", lambda: {
            "eventId": _event(event_id),
            "nextKey": None if next_key is None else _cursor(next_key),
            "showTakerPerspective": None if show_taker_perspective is None else _bool(show_taker_perspective, "show_taker_perspective")},
            auth=True)

    @add
    async def sxbet_best_odds(market_hashes: list[str], show_taker_perspective: bool | None = None) -> dict[str, Any]:
        """Best available odds per market (GET /orders-v3/odds/best). Requires API key."""
        return await call("sxbet_best_odds", "/orders-v3/odds/best", lambda: {
            "marketHashes": _hashes(market_hashes),
            "showTakerPerspective": None if show_taker_perspective is None else _bool(show_taker_perspective, "show_taker_perspective")},
            auth=True)

    @add
    async def sxbet_balance() -> dict[str, Any]:
        """Account balance (GET /user/balance-v3). Requires API key."""
        return await call("sxbet_balance", "/user/balance-v3", lambda: {}, auth=True)

    @add
    async def sxbet_fees() -> dict[str, Any]:
        """Account fee schedule (GET /user/fees-v3). Requires API key."""
        return await call("sxbet_fees", "/user/fees-v3", lambda: {}, auth=True)

    @add
    async def sxbet_positions(status: list[str], event_id: str | None = None, sort_by: str | None = None,
                              sort_asc: bool | None = None, per_page: int | None = None,
                              next_key: str | None = None) -> dict[str, Any]:
        """Positions (GET /positions-v3). status required: subset of MATCHED, LOCKED, SETTLED, FAILED (MATCHED,LOCKED = open). Requires API key."""
        def build():
            if not isinstance(status, list) or not 1 <= len(status) <= 4 or len(set(status)) != len(status):
                raise InvalidInput("INVALID_STATUS")
            return {"status": [_enum(s, _TRADE_STATUS, "status") for s in status],
                    "eventId": None if event_id is None else _event(event_id),
                    "sortBy": None if sort_by is None else _enum(sort_by, ("gameTime", "netMaxWin", "maxLoss", "pnl"), "sort_by"),
                    "sortAsc": None if sort_asc is None else _bool(sort_asc, "sort_asc"),
                    "perPage": None if per_page is None else _int(per_page, "per_page", 1, 100),
                    "nextKey": None if next_key is None else _cursor(next_key)}
        return await call("sxbet_positions", "/positions-v3", build, auth=True)

    @add
    async def sxbet_open_orders(market_hash: str | None = None, event_id: str | None = None,
                                sort_by: str | None = None, sort_asc: bool | None = None,
                                per_page: int | None = None, next_key: str | None = None) -> dict[str, Any]:
        """Your open orders (GET /orders-v3). Read-only: this module cannot create or cancel orders. Requires API key."""
        return await call("sxbet_open_orders", "/orders-v3", lambda: {
            "marketHash": None if market_hash is None else _hash(market_hash),
            "eventId": None if event_id is None else _event(event_id),
            "sortBy": None if sort_by is None else _enum(sort_by, ("createdAt", "event", "totalBetSize"), "sort_by"),
            "sortAsc": None if sort_asc is None else _bool(sort_asc, "sort_asc"),
            "perPage": None if per_page is None else _int(per_page, "per_page", 1, 100),
            "nextKey": None if next_key is None else _cursor(next_key)}, auth=True)

    def _history(market_hash, status, start_date, end_date, sort_asc, per_page, next_key, extra=None):
        start, end = _date_range(start_date, end_date)
        out = {"marketHash": None if market_hash is None else _hash(market_hash),
               "status": None if status is None else _enum(status, _TRADE_STATUS, "status"),
               "startDate": start,
               "endDate": end,
               "sortAsc": None if sort_asc is None else _bool(sort_asc, "sort_asc"),
               "perPage": None if per_page is None else _int(per_page, "per_page", 1, 100),
               "nextKey": None if next_key is None else _cursor(next_key)}
        out.update(extra or {})
        return out

    @add
    async def sxbet_trades(market_hash: str | None = None, status: str | None = None, start_date: str | None = None,
                           end_date: str | None = None, sort_asc: bool | None = None, per_page: int | None = None,
                           next_key: str | None = None) -> dict[str, Any]:
        """Your bets/trades history (GET /trades-v3). Requires API key."""
        return await call("sxbet_trades", "/trades-v3", lambda: _history(
            market_hash, status, start_date, end_date, sort_asc, per_page, next_key), auth=True)

    @add
    async def sxbet_fills(trade_id: str | None = None, order_id: str | None = None, status: str | None = None,
                          start_date: str | None = None, end_date: str | None = None, sort_asc: bool | None = None,
                          per_page: int | None = None, next_key: str | None = None) -> dict[str, Any]:
        """Per-leg fills (GET /fills-v3). Requires API key."""
        def build():
            extra = {"tradeId": None if trade_id is None else _cursor(trade_id),
                     "orderId": None if order_id is None else _cursor(order_id)}
            out = _history(None, status, start_date, end_date, sort_asc, per_page, next_key, extra)
            out.pop("marketHash")
            return out
        return await call("sxbet_fills", "/fills-v3", build, auth=True)

    @add
    async def sxbet_ledger(event_type: str | None = None, per_page: int | None = None,
                           next_key: str | None = None) -> dict[str, Any]:
        """Account ledger (GET /ledger-v3). Read-only. Requires API key."""
        return await call("sxbet_ledger", "/ledger-v3", lambda: {
            "eventType": None if event_type is None else _enum(
                event_type, ("DEPOSIT", "WITHDRAWAL", "SETTLEMENT", "PAYOUT", "FEE", "REFUND", "ADJUSTMENT"), "event_type"),
            "perPage": None if per_page is None else _int(per_page, "per_page", 1, 100),
            "nextKey": None if next_key is None else _cursor(next_key)}, auth=True)

    # ---- SIMULATION (GET reads + pure simulation; never signs, never submits) ----
    @add
    async def sxbet_simulate_order(market_hash: str, betting_outcome_one: bool, stake_base_units: int,
                                   percentage_odds: str, time_in_force: str, expiry: int,
                                   idempotency_key: str, maker_address: str | None = None,
                                   check_balance: bool = False) -> dict[str, Any]:
        """SIMULATION only: validate an order intent and simulate its fill. Never signs, never submits.

        percentage_odds: implied probability x 1e20 as a decimal string (the highest price you accept);
        it is rounded DOWN onto the odds ladder and the raw value is kept. stake_base_units: USDC
        base units (1000000 = 1 USDC). time_in_force: GTC, IOC or FOK. expiry: unix seconds.
        idempotency_key: 8-64 chars [A-Za-z0-9_-]. maker_address: optional; with it the intent gets a
        fresh salt and its intent_hash is the exact value an out-of-band owner approval must bind to.
        Reads GET /metadata/obv3, /markets/find and (API key) /orderbook-v3/snapshot/market with
        showTakerPerspective=true; check_balance=true also reads /user/balance-v3.
        """
        tool = "sxbet_simulate_order"
        out: dict[str, Any] = {"provider": "SX_BET", "tool": tool, "network": settings.network, "mode": "SIMULATION",
                               "source_contract": SIMULATION_DOCS, "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                               "network_calls_made": 0, "signed": False, "submitted": False,
                               "live_trading": "UNAVAILABLE (governance 2.6)"}
        # Type checks before any network call.
        if not isinstance(percentage_odds, str) or not re.fullmatch(r"[0-9]{1,21}", percentage_odds):
            return {**out, "status": "INVALID_INPUT", "error": {"code": "INVALID_PERCENTAGE_ODDS"}}
        for ok, code in ((isinstance(market_hash, str) and _HASH.fullmatch(market_hash), "INVALID_MARKET_HASH"),
                         (isinstance(betting_outcome_one, bool), "INVALID_SIDE"),
                         (type(stake_base_units) is int and stake_base_units > 0, "INVALID_STAKE_BASE_UNITS"),
                         (time_in_force in sim.TIME_IN_FORCE, "INVALID_TIME_IN_FORCE"),
                         (type(expiry) is int, "INVALID_EXPIRY"),
                         (isinstance(check_balance, bool), "INVALID_CHECK_BALANCE")):
            if not ok:
                return {**out, "status": "INVALID_INPUT", "error": {"code": code}}

        async def fetch(path, params, *, auth):
            try:
                reply = await client.get(path, params, auth=auth)
            except SXBetError as exc:
                if exc.code not in NO_NETWORK_CODES:
                    out["network_calls_made"] += 1
                return None, {"endpoint": path, "code": exc.code, "http_status": exc.http_status}
            out["network_calls_made"] += 1
            return reply["data"], None

        meta_raw, err = await fetch("/metadata/obv3", {}, auth=False)
        if err:
            return {**out, "status": "PROVIDER_ERROR", "error": err}
        try:
            meta = sim.parse_metadata(meta_raw, settings.network)
            intent = sim.build_intent(
                network=settings.network, metadata=meta, market_hash=market_hash,
                betting_outcome_one=betting_outcome_one, stake_base_units=stake_base_units,
                requested_percentage_odds=int(percentage_odds), time_in_force=time_in_force, expiry=expiry,
                idempotency_key=idempotency_key, now=int(time.time()), maker=maker_address,
                salt=None if maker_address is None else secrets.randbits(256))
        except sim.IntentError as exc:
            status = "METADATA_INVALID" if str(exc).startswith("METADATA_") else "INVALID_INPUT"
            return {**out, "status": status, "error": {"code": str(exc)}}
        out.update({"intent": intent, "intent_hash": sim.intent_hash(intent),
                    "signing_ready": sim.signing_ready(intent),
                    "odds_rounding": {"requested": intent["requested_percentage_odds"],
                                      "rounded_down": intent["percentage_odds"],
                                      "changed": intent["requested_percentage_odds"] != intent["percentage_odds"]},
                    "live_eligibility": sim.live_eligibility(intent, sim.ExposureLimits()),
                    "warnings": [_UNITS, "Simulation output is DERIVED_SIMULATION, not provider data: fees, betting "
                                 "delay, self-match prevention and book movement are not modelled."]})
        markets, err = await fetch("/markets/find", {"marketHashes": [intent["market_hash"]]}, auth=False)
        if err:
            return {**out, "status": "PROVIDER_ERROR", "error": err}
        market = sim.check_market(markets, intent["market_hash"])
        out["market"] = market
        if market["status"] != "MARKET_ACTIVE":
            return {**out, "status": "REFUSED", "error": {"code": market["status"]},
                    "fill": {"status": "NOT_SIMULATABLE", "reason": market["status"]}}
        if not settings.api_key:
            out["fill"] = {"status": "NOT_SIMULATABLE", "reason": "SXBET_KEY_NOT_CONFIGURED"}
        else:
            book, err = await fetch("/orderbook-v3/snapshot/market",
                                    {"marketHash": intent["market_hash"], "showTakerPerspective": True}, auth=True)
            out["fill"] = ({"status": "NOT_SIMULATABLE", "reason": err["code"]} if err else
                           sim.simulate_fill(intent, book, min_resting_base_units=meta["min_resting_base_units"]))
        if check_balance:
            if not settings.api_key:
                out["balance"] = {"status": "BALANCE_UNRESOLVED", "reason": "SXBET_KEY_NOT_CONFIGURED"}
            else:
                bal, err = await fetch("/user/balance-v3", {}, auth=True)
                out["balance"] = ({"status": "BALANCE_UNRESOLVED", "reason": err["code"]} if err else
                                  sim.check_balance(bal, base_token=intent["base_token"],
                                                    escrow=intent["verifying_contract"],
                                                    stake=int(intent["total_bet_size"])))
        out["status"] = "SIMULATED"
        if len(json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > _MAX_MCP_BYTES:
            return {k: out[k] for k in ("provider", "tool", "network", "mode", "network_calls_made", "signed",
                                         "submitted", "live_trading", "intent", "intent_hash")} | {
                "status": "PAYLOAD_TOO_LARGE", "error": {"code": "SXBET_RESPONSE_EXCEEDS_MCP_SIZE_LIMIT"}}
        return out

    # ---- remaining official authenticated GET routes (OpenAPI 1.0.0) ----
    async def path_call(tool: str, template: str, build_path, params, scope: str) -> dict[str, Any]:
        try:
            path = build_path()
        except InvalidInput as exc:
            return {"provider": "SX_BET", "tool": tool, "endpoint": template, "network": settings.network,
                    "mode": "READ_ONLY", "source_contract": DOCS, "network_calls_made": 0, "data": None,
                    "status": "INVALID_INPUT", "error": {"code": str(exc)}}
        out = await call(tool, path, params, auth=True)
        out["endpoint"] = template
        out["api_key_scope"] = scope
        return out

    @add
    async def sxbet_order(order_id: str) -> dict[str, Any]:
        """One of your orders by signed order id, any status (GET /orders-v3/{orderId}); inactive orders stay ~10-20 min. Scope: Orders: read."""
        return await path_call("sxbet_order", "/orders-v3/{orderId}",
                               lambda: f"/orders-v3/{_hash(order_id, 'order_id')}", lambda: {}, "Orders: read")

    @add
    async def sxbet_order_by_client_id(client_order_id: str) -> dict[str, Any]:
        """One of your orders by the clientOrderId set on submit (GET /orders-v3/client/{clientOrderId}). Scope: Orders: read."""
        def build():
            if not isinstance(client_order_id, str) or not _CLIENT_ID.fullmatch(client_order_id):
                raise InvalidInput("INVALID_CLIENT_ORDER_ID")
            return f"/orders-v3/client/{client_order_id}"
        return await path_call("sxbet_order_by_client_id", "/orders-v3/client/{clientOrderId}", build,
                               lambda: {}, "Orders: read")

    @add
    async def sxbet_trade(trade_id: str) -> dict[str, Any]:
        """One of your bets by opaque tradeId (GET /trades-v3/{tradeId}); a missing or foreign id returns 404. Scope: Account: read."""
        def build():
            if not isinstance(trade_id, str) or not _PATH_TOKEN.fullmatch(trade_id):
                raise InvalidInput("INVALID_TRADE_ID")
            return f"/trades-v3/{trade_id}"
        return await path_call("sxbet_trade", "/trades-v3/{tradeId}", build, lambda: {}, "Account: read")

    @add
    async def sxbet_public_market_orders(market_hash: str, per_page: int | None = None,
                                         next_key: str | None = None) -> dict[str, Any]:
        """De-anonymized resting orders on one market (GET /orders-v3/public); anonymized orders are omitted. Scope: Market data: read."""
        return await call("sxbet_public_market_orders", "/orders-v3/public", lambda: {
            "marketHash": _hash(market_hash),
            "perPage": None if per_page is None else _int(per_page, "per_page", 1, 100),
            "nextKey": None if next_key is None else _cursor(next_key)}, auth=True)

    @add
    async def sxbet_public_user_orders(user_address: str, per_page: int | None = None,
                                       next_key: str | None = None) -> dict[str, Any]:
        """One maker's de-anonymized resting orders across markets (GET /orders-v3/public/user). Scope: Market data: read."""
        return await call("sxbet_public_user_orders", "/orders-v3/public/user", lambda: {
            "userAddress": _address(user_address, "user_address"),
            "perPage": None if per_page is None else _int(per_page, "per_page", 1, 100),
            "nextKey": None if next_key is None else _cursor(next_key)}, auth=True)

    @add
    async def sxbet_public_positions(user_address: str, status: list[str], event_id: str | None = None,
                                     sort_by: str | None = None, sort_asc: bool | None = None,
                                     per_page: int | None = None, next_key: str | None = None) -> dict[str, Any]:
        """A bettor's de-anonymized positions per market (GET /positions-v3/public); status subset of MATCHED, LOCKED, SETTLED, FAILED. Scope: Market data: read."""
        def build():
            if not isinstance(status, list) or not 1 <= len(status) <= 4 or len(set(status)) != len(status):
                raise InvalidInput("INVALID_STATUS")
            if event_id is not None and not (isinstance(event_id, str) and 2 <= len(event_id) <= 64):
                raise InvalidInput("INVALID_EVENT_ID")
            return {"userAddress": _address(user_address, "user_address"),
                    "status": [_enum(x, _TRADE_STATUS, "status") for x in status],
                    "eventId": None if event_id is None else _event(event_id),
                    "sortBy": None if sort_by is None else _enum(sort_by, ("gameTime", "maxLoss"), "sort_by"),
                    "sortAsc": None if sort_asc is None else _bool(sort_asc, "sort_asc"),
                    "perPage": None if per_page is None else _int(per_page, "per_page", 1, 100),
                    "nextKey": None if next_key is None else _cursor(next_key)}
        return await call("sxbet_public_positions", "/positions-v3/public", build, auth=True)

    @add
    async def sxbet_privacy() -> dict[str, Any]:
        """Whether your betting activity is anonymized (GET /user/privacy-v3); read-only, changing it is not exposed. Scope: Account: read."""
        return await call("sxbet_privacy", "/user/privacy-v3", lambda: {}, auth=True)

    @add
    async def sxbet_proxy_wallet() -> dict[str, Any]:
        """Your proxy wallet address and whether it is deployed (GET /user/proxy). Read-only. Scope: Wallet: read."""
        return await call("sxbet_proxy_wallet", "/user/proxy", lambda: {}, auth=True)

    @add
    async def sxbet_proxy_deploy_pending() -> dict[str, Any]:
        """Whether a proxy deployment is in flight (GET /user/pending-deploy-proxy); pending:false does NOT mean success — read sxbet_proxy_wallet.deployed. Scope: Wallet: read."""
        return await call("sxbet_proxy_deploy_pending", "/user/pending-deploy-proxy", lambda: {}, auth=True)

    @add
    async def sxbet_proxy_funding_pending() -> dict[str, Any]:
        """Unresolved deposits to your proxy, newest first (GET /user/transfer-to-proxy/pending); empty does NOT mean the last deposit succeeded. Scope: Wallet: read."""
        return await call("sxbet_proxy_funding_pending", "/user/transfer-to-proxy/pending", lambda: {}, auth=True)

    @add
    async def sxbet_proxy_funding_status(session_id: str) -> dict[str, Any]:
        """Outcome of one proxy deposit by sessionId (GET /user/transfer-to-proxy/status); poll until SUCCESS or FAILED. Scope: Wallet: read."""
        def build():
            if not isinstance(session_id, str) or not _PATH_TOKEN.fullmatch(session_id):
                raise InvalidInput("INVALID_SESSION_ID")
            return {"sessionId": session_id}
        return await call("sxbet_proxy_funding_status", "/user/transfer-to-proxy/status", build, auth=True)

    @add
    async def sxbet_proxy_address_check(proxy_address: str) -> dict[str, Any]:
        """Whether an address is a registered and deployed SX Bet proxy; does not reveal the owner (GET /user/multisig/proxy/{proxyAddress}). Scope: Wallet: read."""
        return await path_call("sxbet_proxy_address_check", "/user/multisig/proxy/{proxyAddress}",
                               lambda: f"/user/multisig/proxy/{_address(proxy_address, 'proxy_address')}",
                               lambda: {}, "Wallet: read")

    @add
    async def sxbet_safe_info(safe_address: str) -> dict[str, Any]:
        """Signers, threshold, nonce and the single pending transaction on your proxy's Safe (GET /user/multisig/{safeAddress}/info). Read-only. Scope: Wallet: read."""
        return await path_call("sxbet_safe_info", "/user/multisig/{safeAddress}/info",
                               lambda: f"/user/multisig/{_address(safe_address, 'safe_address')}/info",
                               lambda: {}, "Wallet: read")

    # ---- DERIVED read tools (GET reads + pure positions/hedging/rules; never sign, never submit) ----
    def dbase(tool: str) -> dict[str, Any]:
        return {"provider": "SX_BET", "tool": tool, "network": settings.network, "mode": "READ_ONLY",
                "label": "DERIVED", "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "network_calls_made": 0, "reads": [], "signed": False, "submitted": False,
                "write_capability": "NONE", "live_trading": "UNAVAILABLE (governance 2.6)"}

    async def dread(out: dict[str, Any], path: str, params: dict[str, Any], *, auth: bool, template: str | None = None):
        """One GET through the shared client (rate coordinator). Returns (data, error)."""
        entry = {"endpoint": template or path, "parameters": dict(params)}
        try:
            reply = await client.get(path, params, auth=auth)
        except SXBetError as exc:
            if exc.code not in NO_NETWORK_CODES:
                out["network_calls_made"] += 1
            err = {"code": exc.code, "http_status": exc.http_status, "endpoint": template or path}
            if exc.retry_after is not None:
                err["retry_after_seconds"] = exc.retry_after
            out["reads"].append({**entry, "status": "ERROR", **err})
            return None, err
        out["network_calls_made"] += 1
        data = reply["data"]
        out["reads"].append({**entry, "status": "OK", "http_status": reply["http_status"],
                             "more_pages": bool(isinstance(data, dict) and data.get("nextKey"))})
        return data, None

    def dfail(out: dict[str, Any], err: dict[str, Any]) -> dict[str, Any]:
        return {**out, "status": "PROVIDER_ERROR", "error": err}

    def dinvalid(out: dict[str, Any], code: str) -> dict[str, Any]:
        return {**out, "status": "INVALID_INPUT", "error": {"code": code}}

    def dkey(out: dict[str, Any]) -> dict[str, Any] | None:
        if not settings.api_key:
            return {**out, "status": "KEY_NOT_CONFIGURED", "error": {"code": "SXBET_KEY_NOT_CONFIGURED"}}
        return None

    narrow_hint = {
        "sxbet_positions_summary": "Narrow with event_id.",
        "sxbet_exposure_pnl": "Narrow with event_id.",
        "sxbet_resting_orders_scope": "Narrow with fewer order_ids, or market_hash instead of event_id.",
        "sxbet_reconciliation_diagnostics": "Narrow with market_hash or fewer ws_messages.",
    }

    def dcap(out: dict[str, Any]) -> dict[str, Any]:
        """Never truncate: over the MCP cap the whole derived body is replaced by PAYLOAD_TOO_LARGE."""
        if len(json.dumps(out, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")) <= _MAX_MCP_BYTES:
            return out
        keep = ("provider", "tool", "network", "mode", "label", "retrieved_at_utc", "network_calls_made", "reads",
                "signed", "submitted", "write_capability", "live_trading")
        return {k: out[k] for k in keep if k in out} | {
            "status": "PAYLOAD_TOO_LARGE", "error": {"code": "SXBET_RESPONSE_EXCEEDS_MCP_SIZE_LIMIT",
                                                     "hint": narrow_hint.get(out.get("tool"),
                                                                             "This tool has no narrowing parameter; read "
                                                                             "the underlying rows with the paginated "
                                                                             "read tools (per_page / next_key).")}}

    def _slippage(v):
        if not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,21}", v):
            raise InvalidInput("INVALID_MAX_SLIPPAGE")
        return int(v)

    def _event_opt(v):
        if v is not None and not (isinstance(v, str) and _EVENT.fullmatch(v) and len(v) >= 2):
            raise InvalidInput("INVALID_EVENT_ID")
        return v

    def _rows_of(page, key):
        return page[key] if isinstance(page, dict) and isinstance(page.get(key), list) else None

    async def market_bundle(out: dict[str, Any], market_hash: str) -> tuple[dict | None, dict | None]:
        """Reads for one market: markets/find, metadata, open bets, own resting orders, fees, then the book last."""
        started = time.monotonic()
        mh = market_hash.lower()
        bundle: dict[str, Any] = {}
        markets, err = await dread(out, "/markets/find", {"marketHashes": [mh]}, auth=False)
        if err:
            return None, err
        bundle["market_check"] = sim.check_market(markets, mh)
        meta_raw, err = await dread(out, "/metadata/obv3", {}, auth=False)
        if err:
            return None, err
        try:
            bundle["metadata"] = sim.parse_metadata(meta_raw, settings.network)
        except sim.IntentError as exc:
            bundle["metadata"], bundle["metadata_error"] = None, str(exc)
        trades: list = []
        more = False
        for st in ("MATCHED", "LOCKED"):
            page, err = await dread(out, "/trades-v3", {"marketHash": mh, "status": st, "perPage": 100}, auth=True)
            if err:
                return None, err
            rows = _rows_of(page, "trades")
            if rows is None:
                return None, {"code": "SXBET_UNEXPECTED_SHAPE", "endpoint": "/trades-v3"}
            trades += rows
            more = more or bool(page.get("nextKey"))
        bundle["trades"] = {"trades": trades, "nextKey": "MORE_PAGES" if more else None}
        bundle["orders"], err = await dread(out, "/orders-v3", {"marketHash": mh, "perPage": 100}, auth=True)
        if err:
            return None, err
        bundle["fees"], err = await dread(out, "/user/fees-v3", {}, auth=True)
        if err:
            return None, err
        bundle["book"], err = await dread(out, "/orderbook-v3/snapshot/market", {"marketHash": mh}, auth=True)
        if err:
            return None, err
        meta = bundle["metadata"] or {}
        roll = pos_mod.build_positions(trades=bundle["trades"], open_orders=bundle["orders"], fees=bundle["fees"],
                                       markets=markets if isinstance(markets, list) else None,
                                       base_token=meta.get("base_token"), escrow=meta.get("verifying_contract"),
                                       settled_rows_read=False)
        bundle["roll_up"] = roll
        position = next((m for m in roll["markets"] if m["market_hash"] == mh), None)
        if position is None:  # no open bet and no resting order on this market
            position = {"market_hash": mh, "grain_used": "NONE", "data_conflict": roll["data_conflict"],
                        "pnl_base_units": {"if_outcome_one": 0, "if_outcome_two": 0, "if_void": 0},
                        "projected_payout_fee_base_units": {"if_outcome_one": "0", "if_outcome_two": "0"},
                        "matched_stake_base_units": {"total": 0}, "worst_case_liability_base_units": 0}
        # Rows left out of the roll-up (parlay, quarter line, malformed) make the position unknown, never 0/0.
        if (not roll["completeness"]["complete"] or roll["data_conflict"]
                or mh in roll["completeness"]["excluded_rows"]["market_hashes"]):
            position = {**position, "data_conflict": True}
        bundle["market_position"] = position
        bundle["orders_rows"] = _rows_of(bundle["orders"], "orders")
        bundle["data_age_seconds"] = round(time.monotonic() - started, 3)
        return bundle, None

    def _hedge(bundle, *, mode, fraction, stake, slippage, max_age):
        meta = bundle["metadata"] or {}
        return hedge_mod.simulate_hedge(
            market_position=bundle["market_position"], book=bundle["book"],
            market=bundle["market_check"].get("market"), fees=bundle["fees"], mode=mode, fraction=fraction,
            hedge_stake_base_units=stake, max_slippage_percentage_odds=slippage,
            own_resting_orders=bundle["orders_rows"], data_age_seconds=bundle["data_age_seconds"],
            max_data_age_seconds=max_age, odds_ladder_step_size=meta.get("odds_ladder_step_size"),
            min_order_base_units=meta.get("min_order_base_units"))

    def _resting_scope(rows, *, order_ids=None, market_hash=None, event_id=None):
        live = {o["id"].lower(): o for o in (rows or []) if isinstance(o, dict) and isinstance(o.get("id"), str)
                and o.get("status") in ("ACTIVE", "PENDING")}
        if order_ids is not None:
            hit = [live[i.lower()] for i in order_ids if i.lower() in live]
            not_open = [i.lower() for i in order_ids if i.lower() not in live]
        else:
            hit = [o for o in live.values()
                   if (market_hash is None or str(o.get("marketHash", "")).lower() == market_hash.lower())
                   and (event_id is None or o.get("eventId") == event_id)]
            not_open = []
        per = {"outcome_one": 0, "outcome_two": 0}
        malformed = []
        for o in hit:
            rem = pos_mod.units(o.get("remainingSize"))
            if rem is None or not isinstance(o.get("isBettingOutcomeOne"), bool):
                malformed.append(o["id"].lower())
                continue
            per["outcome_one" if o["isBettingOutcomeOne"] else "outcome_two"] += rem
        return {"label": "DERIVED", "would_affect_order_ids": [o["id"].lower() for o in hit],
                "not_open_order_ids": not_open, "orders_affected": len(hit), "remaining_size_base_units": per,
                "remaining_total_base_units": None if malformed else per["outcome_one"] + per["outcome_two"],
                "locked_capital_released_base_units": 0, "malformed_order_ids": malformed,
                "provider_rows": hit, "cancel_sent": False,
                "note": ("Preview only: no cancel is sent. A real cancel request is not a confirmation: the provider "
                         "reports cancelled / notCancelled / unconfirmed, a resting order can still fill before the "
                         "cancel reaches the engine, and only a reconciled INACTIVE with inactiveReason USER_REQUESTED "
                         "proves it (developers/cancelling-orders). Resting orders lock no capital "
                         "(developers/risk-limits), so a cancel releases no escrow.")}

    @add
    async def sxbet_order_status(order_id: str) -> dict[str, Any]:
        """One of your orders plus its fills (GET /orders-v3/{orderId} + GET /fills-v3?orderId=). Read-only: never cancels, signs or submits. A 404 is not proof the order never existed (inactive orders are kept ~10-20 min). Requires API key."""
        out = dbase("sxbet_order_status")
        if not isinstance(order_id, str) or not _HASH.fullmatch(order_id):
            return dinvalid(out, "INVALID_ORDER_ID")
        if (k := dkey(out)):
            return k
        oid = order_id.lower()
        order_data, err = await dread(out, f"/orders-v3/{oid}", {}, auth=True, template="/orders-v3/{orderId}")
        if err and err.get("http_status") != 404:
            return dfail(out, err)
        fills, ferr = await dread(out, "/fills-v3", {"orderId": oid, "perPage": 100}, auth=True)
        if ferr:
            return dfail(out, ferr)
        order = order_data.get("order") if isinstance(order_data, dict) else None
        rows = _rows_of(fills, "fills")
        if rows is None:
            return dfail(out, {"code": "SXBET_UNEXPECTED_SHAPE", "endpoint": "/fills-v3"})
        more = bool(fills.get("nextKey"))
        amounts = [pos_mod.units(f.get("fillAmount")) if isinstance(f, dict) else None for f in rows]
        by_fills = None if None in amounts else sum(amounts)
        derived: dict[str, Any] = {
            "label": "DERIVED", "fills_count": len(rows), "fills_more_pages": more,
            "fills_status_counts": {s: sum(1 for f in rows if isinstance(f, dict) and f.get("status") == s)
                                    for s in pos_mod.BET_STATUS},
            "filled_by_fills_base_units": by_fills}
        if isinstance(order, dict):
            total, rem = pos_mod.units(order.get("totalBetSize")), pos_mod.units(order.get("remainingSize"))
            filled = None if total is None or rem is None or rem > total else total - rem
            derived.update({
                "lookup": "FOUND", "status": order.get("status"), "inactive_reason": order.get("inactiveReason"),
                "filled_by_order_base_units": filled, "remaining_base_units": rem,
                "partially_filled": filled is not None and 0 < filled < total,
                "fills_vs_order": ("FILLS_INCOMPLETE_MORE_PAGES" if more else
                                   "NOT_COMPARABLE" if filled is None or by_fills is None else
                                   "MATCH" if filled == by_fills else "MISMATCH")})
        else:
            derived.update({"lookup": "NOT_FOUND" if err else "UNEXPECTED_SHAPE",
                            "note": "A 404 within ~10-20 min of INACTIVE means gone or never existed; later it proves "
                                    "nothing. The fills are the durable evidence of execution."})
        return dcap({**out, "status": "DERIVED_FROM_REAL_API" if not more else "PARTIAL", "order_id": oid,
                     "provider_order": order, "provider_fills": rows, "derived": derived,
                     "sources": ["https://docs.sx.bet/api-reference/get-order-v3",
                                 "https://docs.sx.bet/api-reference/get-fills-v3",
                                 "https://docs.sx.bet/developers/order-lifecycle"]})

    @add
    async def sxbet_positions_summary(event_id: str | None = None) -> dict[str, Any]:
        """Position manager over live reads: per selection/market/event/total matched stake, payout and P&L per outcome, liability, resting orders, partial fills, projected fees, capital and top-of-book mark-to-market (HYBRID_MARKET_AWARE). GETs only (positions, trades MATCHED+LOCKED, orders, balance, fees, metadata, best odds); never signs or submits. Requires API key."""
        out = dbase("sxbet_positions_summary")
        try:
            _event_opt(event_id)
        except InvalidInput as exc:
            return dinvalid(out, str(exc))
        if (k := dkey(out)):
            return k
        ev = {"eventId": event_id} if event_id else {}
        positions, err = await dread(out, "/positions-v3", {"status": ["MATCHED", "LOCKED"], "perPage": 100, **ev}, auth=True)
        if err:
            return dfail(out, err)
        trades, more = [], False
        for st in ("MATCHED", "LOCKED"):
            page, err = await dread(out, "/trades-v3", {"status": st, "perPage": 100}, auth=True)
            if err:
                return dfail(out, err)
            trades += _rows_of(page, "trades") or []
            more = more or bool(isinstance(page, dict) and page.get("nextKey"))
        orders, err = await dread(out, "/orders-v3", {"perPage": 100, **ev}, auth=True)
        if err:
            return dfail(out, err)
        balance, err = await dread(out, "/user/balance-v3", {}, auth=True)
        if err:
            return dfail(out, err)
        fees, err = await dread(out, "/user/fees-v3", {}, auth=True)
        if err:
            return dfail(out, err)
        meta_raw, err = await dread(out, "/metadata/obv3", {}, auth=False)
        if err:
            return dfail(out, err)
        try:
            meta = sim.parse_metadata(meta_raw, settings.network)
        except sim.IntentError:
            meta = {}
        pos_rows = _rows_of(positions, "positions") or []
        if event_id:
            keep = {str(p.get("marketHash", "")).lower() for p in pos_rows if isinstance(p, dict)}
            keep |= {str(o.get("marketHash", "")).lower() for o in (_rows_of(orders, "orders") or []) if isinstance(o, dict)}
            trades = [t for t in trades if isinstance(t, dict) and str(t.get("marketHash", "")).lower() in keep]
        hashes = sorted({r["marketHash"].lower() for r in [*trades, *pos_rows] if isinstance(r, dict)
                         and isinstance(r.get("marketHash"), str) and _HASH.fullmatch(r["marketHash"])})
        best, best_note = None, None
        if hashes and len(hashes) <= 30:
            best, err = await dread(out, "/orders-v3/odds/best", {"marketHashes": hashes}, auth=True)
            if err:
                return dfail(out, err)
        elif hashes:
            best_note = "MORE_THAN_30_MARKETS_BEST_ODDS_NOT_READ_MARK_TO_MARKET_NULL"
        roll = pos_mod.build_positions(positions=positions, trades={"trades": trades, "nextKey": "MORE_PAGES" if more else None},
                                       open_orders=orders, balance=balance, fees=fees, best_odds=best,
                                       base_token=meta.get("base_token"), escrow=meta.get("verifying_contract"),
                                       settled_rows_read=False)
        return dcap({**out, "status": "DERIVED_FROM_REAL_API" if roll["completeness"]["complete"] else "PARTIAL",
                     "event_id": event_id, "best_odds_note": best_note, "position_manager": roll,
                     "warnings": [_UNITS, "DERIVED from the provider rows echoed in position_manager; mark_to_market is "
                                  "HYBRID_MARKET_AWARE (market quotes), top of book only, before fees."]})

    @add
    async def sxbet_exposure_pnl(event_id: str | None = None) -> dict[str, Any]:
        """Exposure and P&L from the provider's per-market positions: open P&L per outcome and worst case (positions-v3 MATCHED,LOCKED), realised P&L (positions-v3 SETTLED), top-of-book mark-to-market (HYBRID_MARKET_AWARE). GET only; never signs or submits. Requires API key."""
        out = dbase("sxbet_exposure_pnl")
        try:
            _event_opt(event_id)
        except InvalidInput as exc:
            return dinvalid(out, str(exc))
        if (k := dkey(out)):
            return k
        ev = {"eventId": event_id} if event_id else {}
        open_pos, err = await dread(out, "/positions-v3", {"status": ["MATCHED", "LOCKED"], "perPage": 100, **ev}, auth=True)
        if err:
            return dfail(out, err)
        settled, err = await dread(out, "/positions-v3", {"status": ["SETTLED"], "perPage": 100, **ev}, auth=True)
        if err:
            return dfail(out, err)
        hashes = sorted({p["marketHash"].lower() for p in (_rows_of(open_pos, "positions") or [])
                         if isinstance(p, dict) and isinstance(p.get("marketHash"), str) and _HASH.fullmatch(p["marketHash"])})
        best, note = None, None
        if hashes and len(hashes) <= 30:
            best, err = await dread(out, "/orders-v3/odds/best", {"marketHashes": hashes}, auth=True)
            if err:
                return dfail(out, err)
        elif hashes:
            note = "MORE_THAN_30_MARKETS_BEST_ODDS_NOT_READ_MARK_TO_MARKET_NULL"
        o = pos_mod.build_positions(positions=open_pos, best_odds=best)
        s = pos_mod.build_positions(positions=settled)
        compact = [{k: m.get(k) for k in ("market_hash", "event_id", "pnl_base_units", "pnl_reason",
                                          "worst_case_liability_base_units",
                                          "provider_net_max_win_base_units", "mark_to_market", "market", "data_conflict")}
                   for m in o["markets"]]
        realized = [{"market_hash": m["market_hash"], "realized_pnl_base_units": m.get("realized_pnl_base_units"),
                     "reason": m.get("realized_pnl_reason")} for m in s["markets"]]
        complete = o["completeness"]["complete"] and s["completeness"]["complete"]
        rsum = [r["realized_pnl_base_units"] for r in realized]
        return dcap({**out, "status": "DERIVED_FROM_REAL_API" if complete else "PARTIAL", "event_id": event_id,
                     "best_odds_note": note, "open_markets": compact, "settled_markets": realized,
                     "totals": {"open_worst_case_liability_base_units": o["totals"]["worst_case_liability_base_units"],
                                "mark_to_market_base_units": o["totals"]["mark_to_market_base_units"],
                                "mark_to_market_label": "HYBRID_MARKET_AWARE",
                                "realized_pnl_base_units": None if None in rsum else sum(rsum),
                                "realized_source": "provider positions-v3 pnl (status=SETTLED only)"},
                     "completeness": {"open": o["completeness"], "settled": s["completeness"]},
                     "reconciliation": {"open": o["reconciliation"], "settled": s["reconciliation"]},
                     "provider_rows": {"open": _rows_of(open_pos, "positions"), "settled": _rows_of(settled, "positions")},
                     "sources": list(pos_mod.SOURCES), "docs_unverified": list(pos_mod.DOCS_UNVERIFIED),
                     "warnings": [_UNITS]})

    @add
    async def sxbet_resting_orders_scope(order_ids: list[str] | None = None, market_hash: str | None = None,
                                         event_id: str | None = None) -> dict[str, Any]:
        """Cancel PREVIEW: which of your resting orders a cancel by order ids, by market or by event WOULD affect (GET /orders-v3). Sends no cancel; a cancel request is never a confirmation. Pass exactly one of order_ids (1..maxCancelOrders), market_hash, event_id. Requires API key."""
        out = dbase("sxbet_resting_orders_scope")
        if sum(x is not None for x in (order_ids, market_hash, event_id)) != 1:
            return dinvalid(out, "PASS_EXACTLY_ONE_OF_ORDER_IDS_MARKET_HASH_EVENT_ID")
        if order_ids is not None:
            if not isinstance(order_ids, list) or not 1 <= len(order_ids) <= 100 or not all(
                    isinstance(i, str) and _HASH.fullmatch(i) for i in order_ids):
                return dinvalid(out, "INVALID_ORDER_IDS")
            if len({i.lower() for i in order_ids}) != len(order_ids):
                return dinvalid(out, "DUPLICATE_ORDER_ID")
        if market_hash is not None and not (isinstance(market_hash, str) and _HASH.fullmatch(market_hash)):
            return dinvalid(out, "INVALID_MARKET_HASH")
        try:
            _event_opt(event_id)
        except InvalidInput as exc:
            return dinvalid(out, str(exc))
        if (k := dkey(out)):
            return k
        meta_raw, err = await dread(out, "/metadata/obv3", {}, auth=False)
        if err:
            return dfail(out, err)
        limits = meta_raw.get("limits") if isinstance(meta_raw, dict) and isinstance(meta_raw.get("limits"), dict) else {}
        max_cancel = limits.get("maxCancelOrders")
        if order_ids is not None and type(max_cancel) is int and len(order_ids) > max_cancel:
            return dinvalid(out, "MORE_ORDER_IDS_THAN_MAX_CANCEL_ORDERS")
        params: dict[str, Any] = {"perPage": 100}
        if market_hash:
            params["marketHash"] = market_hash.lower()
        if event_id:
            params["eventId"] = event_id
        orders, err = await dread(out, "/orders-v3", params, auth=True)
        if err:
            return dfail(out, err)
        rows = _rows_of(orders, "orders")
        if rows is None:
            return dfail(out, {"code": "SXBET_UNEXPECTED_SHAPE", "endpoint": "/orders-v3"})
        more = bool(orders.get("nextKey"))
        return dcap({**out, "status": "PARTIAL" if more else "DERIVED_FROM_REAL_API", "preview_only": True,
                     "orders_more_pages_not_read": more, "max_cancel_orders_metadata": max_cancel,
                     "scope": _resting_scope(rows, order_ids=order_ids, market_hash=market_hash, event_id=event_id),
                     "sources": ["https://docs.sx.bet/api-reference/get-orders-v3",
                                 "https://docs.sx.bet/developers/cancelling-orders",
                                 "https://docs.sx.bet/developers/risk-limits"]})

    def _hedge_inputs(market_hash, mode, fraction, stake, slippage, max_age):
        _hash(market_hash)
        slip = _slippage(slippage)
        hedge_mod.validate_params(mode, fraction, stake, slip)
        _int(max_age, "max_data_age_seconds", 1, 3600)
        return slip

    @add
    async def sxbet_hedge_simulation(market_hash: str, mode: str = "FULL", fraction: str | None = None,
                                     hedge_stake_base_units: int | None = None,
                                     max_slippage_percentage_odds: str = "0",
                                     max_data_age_seconds: int = 10) -> dict[str, Any]:
        """SIMULATION: hedge your position on one binary market against the live order book. mode FULL (equalize outcomes), PARTIAL (fraction like "0.5" of the gap) or REDUCE (hedge_stake_base_units). max_slippage_percentage_odds: implied probability x 1e20 added to the best taker price, decimal string. Status HEDGE_AVAILABLE / PARTIAL_HEDGE_ONLY / HEDGE_UNAVAILABLE / HEDGE_UNCERTAIN. Never a guaranteed cashout; never signs or submits. Requires API key."""
        out = dbase("sxbet_hedge_simulation") | {"mode": "SIMULATION"}
        try:
            slip = _hedge_inputs(market_hash, mode, fraction, hedge_stake_base_units, max_slippage_percentage_odds,
                                 max_data_age_seconds)
        except (InvalidInput, hedge_mod.HedgeError) as exc:
            return dinvalid(out, str(exc))
        if (k := dkey(out)):
            return k
        bundle, err = await market_bundle(out, market_hash)
        if err:
            return dfail(out, err)
        result = _hedge(bundle, mode=mode, fraction=fraction, stake=hedge_stake_base_units, slippage=slip,
                        max_age=max_data_age_seconds)
        return dcap({**out, "status": result["status"], "hedge": result, "market": bundle["market_check"],
                     "position": bundle["market_position"], "completeness": bundle["roll_up"]["completeness"],
                     "reconciliation": bundle["roll_up"]["reconciliation"]})

    @add
    async def sxbet_close_position_preview(market_hash: str, max_slippage_percentage_odds: str = "0",
                                           max_data_age_seconds: int = 10) -> dict[str, Any]:
        """PREVIEW of closing your position on one market: the resting orders a cancel would have to cover plus a FULL hedge simulation on the live book. Never cancels, signs or submits anything; not a guaranteed cashout. Requires API key."""
        out = dbase("sxbet_close_position_preview") | {"mode": "SIMULATION"}
        try:
            slip = _hedge_inputs(market_hash, "FULL", None, None, max_slippage_percentage_odds, max_data_age_seconds)
        except (InvalidInput, hedge_mod.HedgeError) as exc:
            return dinvalid(out, str(exc))
        if (k := dkey(out)):
            return k
        bundle, err = await market_bundle(out, market_hash)
        if err:
            return dfail(out, err)
        result = _hedge(bundle, mode="FULL", fraction=None, stake=None, slippage=slip, max_age=max_data_age_seconds)
        return dcap({**out, "status": result["status"], "preview_only": True,
                     "steps": ["1. cancel the resting orders on this market (preview below; not sent)",
                               "2. full hedge as a taker order (simulation below; not signed, not submitted)"],
                     "resting_orders_scope": _resting_scope(bundle["orders_rows"], market_hash=market_hash),
                     "hedge": result, "market": bundle["market_check"], "position": bundle["market_position"],
                     "completeness": bundle["roll_up"]["completeness"], "warning": hedge_mod.WARNING})

    @add
    async def sxbet_rules_evaluate(market_hash: str, rules: dict[str, Any] | None = None,
                                   max_slippage_percentage_odds: str = "0") -> dict[str, Any]:
        """Evaluate caller-supplied rules on one market (stop_loss, take_profit, trailing_stop, daily_loss_limit, exposure_limit, close_on_odds_move, close_on_time_remaining, block_on_suspended_market, block_on_stale_data). All disabled unless enabled in this call; nothing is persisted. Output only NO_ACTION / ALERT / ORDER_PROPOSAL; a proposal is never executable, signed or submitted. Requires API key."""
        out = dbase("sxbet_rules_evaluate") | {"mode": "SIMULATION"}
        try:
            slip = _hedge_inputs(market_hash, "FULL", None, None, max_slippage_percentage_odds, 10)
            rules_mod.validate_config(rules)
        except (InvalidInput, hedge_mod.HedgeError, rules_mod.RuleError) as exc:
            return dinvalid(out, str(exc))
        if (k := dkey(out)):
            return k
        bundle, err = await market_bundle(out, market_hash)
        if err:
            return dfail(out, err)
        book = bundle["book"] if isinstance(bundle["book"], dict) else {}
        # Own resting orders are not liquidity we could hedge against (same removal as hedging._levels).
        own: dict[tuple[bool, int], int] = {}
        for o in bundle["orders_rows"] or []:
            if not isinstance(o, dict) or o.get("status") not in ("ACTIVE", "PENDING") \
                    or pos_mod._lower(o.get("marketHash")) not in (None, market_hash.lower()):
                continue
            rem, p, one = pos_mod.units(o.get("remainingSize")), pos_mod.odds(o.get("percentageOdds")), o.get("isBettingOutcomeOne")
            if rem is not None and p is not None and isinstance(one, bool):
                own[(one, p)] = own.get((one, p), 0) + rem

        def top(side):
            levels = []
            for x in book.get(side) or []:
                p = pos_mod.odds(x.get("percentageOdds")) if isinstance(x, dict) else None
                size = pos_mod.units(x.get("size")) if p else None
                if p is None or size is None:
                    continue
                foreign = size - own.get((side == "outcomeOne", p), 0)
                if foreign > 0:
                    levels.append(x if foreign == size else {**x, "size": str(foreign)})
            return max(levels, key=lambda x: pos_mod.odds(x["percentageOdds"])) if levels else None
        best = {"outcomeOne": top("outcomeOne"), "outcomeTwo": top("outcomeTwo")}
        position = dict(bundle["market_position"])
        pnl = position.get("pnl_base_units") or {}
        position["mark_to_market"] = pos_mod.mark_to_market(pnl.get("if_outcome_one"), pnl.get("if_outcome_two"), best)
        # Taker price to back outcome X = 1e20 - best maker odds resting on the other outcome.
        prices = {"outcome_one": None if best["outcomeTwo"] is None else pos_mod.ODDS - pos_mod.odds(best["outcomeTwo"]["percentageOdds"]),
                  "outcome_two": None if best["outcomeOne"] is None else pos_mod.ODDS - pos_mod.odds(best["outcomeOne"]["percentageOdds"])}
        hedge = _hedge(bundle, mode="FULL", fraction=None, stake=None, slippage=slip, max_age=10)
        verdict = rules_mod.evaluate(rules, market_position=position, market=bundle["market_check"].get("market"),
                                     now_unix=int(time.time()), data_age_seconds=bundle["data_age_seconds"],
                                     taker_percentage_odds=prices, hedge=hedge)
        return dcap({**out, "status": verdict["decision"], "rules": verdict, "mark_to_market": position["mark_to_market"],
                     "taker_percentage_odds": {k: None if v is None else str(v) for k, v in prices.items()},
                     "hedge_status": hedge["status"], "market": bundle["market_check"]})

    @add
    async def sxbet_risk_status() -> dict[str, Any]:
        """MCP-side risk status, no provider call: LIVE_TRADING UNAVAILABLE, signer not reachable from MCP, no write tools, which rate coordinator admits SX Bet calls. Real exposure limits live only in the owner-signed signer config and are unknown to MCP."""
        limits = sim.ExposureLimits()
        return {"provider": "SX_BET", "tool": "sxbet_risk_status", "network": settings.network, "mode": "READ_ONLY",
                "network_calls_made": 0, "live_trading": "UNAVAILABLE",
                "live_trading_reason": "Governance 2.6: no isolated signer with signer-verified per-order confirmation "
                                       "is reachable from this process; no tool argument can enable it.",
                "signer_reachable_from_mcp": False, "write_tools": [], "write_capability": "NONE",
                "http_methods_available": ["GET"],
                "rate_coordinator": getattr(getattr(client, "_limiter", None), "kind", "CUSTOM"),
                "rate_limit_contract": RATE_LIMIT_DOCS,
                "exposure_limits": {"known_to_mcp": False, "value": None,
                                    "reason": "Owner limits exist only in the owner-signed signer risk config (default zero)."},
                "simulation_exposure_limits_base_units": {"max_stake": limits.max_stake_base_units,
                                                          "max_exposure": limits.max_exposure_base_units},
                "proposals": "ORDER_PROPOSAL outputs are never executable, signed or submitted."}

    @add
    async def sxbet_reconciliation_diagnostics(market_hash: str | None = None,
                                               ws_messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """REST vs WebSocket reconciliation of your orders, bets and fills: dedupe by documented ids, order by updatedAt, conflicts reported (never guessed). ws_messages: optional publications you received on account:orders_v3_#, account:trades_v3_#, account:fills_v3_# (max 500). GET only; never signs or submits. Requires API key."""
        out = dbase("sxbet_reconciliation_diagnostics")
        if market_hash is not None and not (isinstance(market_hash, str) and _HASH.fullmatch(market_hash)):
            return dinvalid(out, "INVALID_MARKET_HASH")
        if ws_messages is not None:
            if not isinstance(ws_messages, list) or len(ws_messages) > 500 or not all(isinstance(m, dict) for m in ws_messages):
                return dinvalid(out, "INVALID_WS_MESSAGES")
            if len(json.dumps(ws_messages, default=str)) > 131_072:
                return dinvalid(out, "WS_MESSAGES_TOO_LARGE")
        if (k := dkey(out)):
            return k
        mh = {"marketHash": market_hash.lower()} if market_hash else {}
        orders, err = await dread(out, "/orders-v3", {"perPage": 100, **mh}, auth=True)
        if err:
            return dfail(out, err)
        trades, fills, more = [], [], {"trades": False, "fills": False}
        for st in ("MATCHED", "LOCKED"):
            page, err = await dread(out, "/trades-v3", {"status": st, "perPage": 100, **mh}, auth=True)
            if err:
                return dfail(out, err)
            trades += _rows_of(page, "trades") or []
            more["trades"] = more["trades"] or bool(isinstance(page, dict) and page.get("nextKey"))
            page, err = await dread(out, "/fills-v3", {"status": st, "perPage": 100}, auth=True)
            if err:
                return dfail(out, err)
            fills += _rows_of(page, "fills") or []
            more["fills"] = more["fills"] or bool(isinstance(page, dict) and page.get("nextKey"))
        if market_hash:
            fills = [f for f in fills if isinstance(f, dict) and str(f.get("marketHash", "")).lower() == market_hash.lower()]
        roll = pos_mod.build_positions(trades={"trades": trades, "nextKey": "MORE" if more["trades"] else None},
                                       fills={"fills": fills, "nextKey": "MORE" if more["fills"] else None},
                                       open_orders=orders, ws_messages=ws_messages, settled_rows_read=False)
        return dcap({**out, "status": "DERIVED_FROM_REAL_API" if roll["completeness"]["complete"] else "PARTIAL",
                     "ws_messages_label": "CALLER_SUPPLIED" if ws_messages is not None else None,
                     "inputs": roll["inputs"], "completeness": roll["completeness"],
                     "reconciliation": roll["reconciliation"],
                     "cross_checks": [{"market_hash": m["market_hash"], "grain_used": m["grain_used"],
                                       "checks": m["cross_checks"], "data_conflict": m["data_conflict"]}
                                      for m in roll["markets"]],
                     "method": ["dedupe by order id / tradeId / fill id",
                                "latest updatedAt wins only when versions are orderable and follow the documented lifecycle",
                                "otherwise CONFLICT: reported and excluded from every computation"],
                     "sources": list(pos_mod.SOURCES)})

    return tuple(names)


def _raise(code: str):
    raise InvalidInput(code)
