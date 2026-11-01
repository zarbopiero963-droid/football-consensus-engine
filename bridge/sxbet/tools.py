"""READ_ONLY and SIMULATION SX Bet MCP tools (`sxbet_*`).

Every READ_ONLY tool maps 1:1 to a GET route of the official OpenAPI
(https://docs.sx.bet/openapi.json). Parameter names, enums and bounds come from
that spec; nothing is invented. `sxbet_simulate_order` (SIMULATION) only
composes GET reads with the pure functions of `simulation.py`: it never signs,
never holds a key and never submits. There is no write tool: LIVE_TRADING stays
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

from . import simulation as sim
from .client import SXBetError, SXBetReadClient, Settings

DOCS = "https://docs.sx.bet/openapi.json"
SIMULATION_DOCS = ("https://docs.sx.bet/developers/posting-orders", "https://docs.sx.bet/developers/odds-rounding",
                   "https://docs.sx.bet/developers/time-in-force", "https://docs.sx.bet/developers/taking-liquidity")
_MAX_MCP_BYTES = 262_144
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_EVENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CURSOR = re.compile(r"^[\x21-\x7e]{1,512}$")
_DATE = re.compile(r"^[0-9TtZz:+.\-]{1,40}$")
_TRADE_STATUS = ("MATCHED", "LOCKED", "SETTLED", "FAILED")
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
                   "network_calls_made": 0 if exc.code in ("SXBET_LOCAL_RATE_LIMITED", "SXBET_KEY_NOT_CONFIGURED") else 1,
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
                "write_tools": [], "simulation_tools": ["sxbet_simulate_order"],
                "local_rate_limit_per_minute": settings.max_rpm,
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
                if exc.code not in ("SXBET_LOCAL_RATE_LIMITED", "SXBET_KEY_NOT_CONFIGURED"):
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

    return tuple(names)


def _raise(code: str):
    raise InvalidInput(code)
