"""Typed football-only MCP tools with independently authorized live access.

PR-03 registers optional, authenticated MCP tools only when explicitly
enabled in environment. Without an injected *test-only* mock client every
endpoint returns an explicit non-network authorization gate. Neither
the HTTP transport nor the resilience engine is constructed here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import json
from typing import Any, Mapping, Protocol

from .config import Settings
from .catalog import ALLOWED_PATHS
from .contracts import ENDPOINTS, SOURCE, VERSION, ParameterError, validate
from .full_contract_audit import audit_contract_coverage

_MAX_MCP_DATA_BYTES = 262_144
_OFFLINE_WARNING = "Offline PR-03 tool: never claim real coverage, live odds or PIT data."
# Fixed codes of the client/resilience layer that reach the MCP client unchanged.
_PASS_THROUGH_CODES = frozenset({
    "PROVIDER_TIMEOUT", "PROVIDER_NETWORK_ERROR", "PROVIDER_REDIRECT_REJECTED",
    "PROVIDER_RESPONSE_TOO_LARGE", "PROVIDER_INVALID_JSON",
    "PROVIDER_JSON_NOT_OBJECT", "PROVIDER_DECLARED_ERRORS",
    "PROVIDER_RESPONSE_FIELD_MISSING", "PROVIDER_INVALID_PAGINATION",
    "QUOTA_COORDINATOR_UNAVAILABLE", "QUOTA_RELEASE_FAILED",
    "QUOTA_RECONCILIATION_FAILED", "PROVIDER_INTERNAL_ERROR",
    "PROVIDER_REPLY_CONTRACT_INVALID", "PROVIDER_NO_REPLY", "PROVIDER_NOT_CONFIGURED",
    "INVALID_STATUS_RESPONSE",
})


def _error_for(exc: BaseException, parameters: tuple[str, ...]) -> dict[str, Any]:
    """A fixed code (plus numbers or allowlisted keys only) for a real-client failure.
    Never an exception message, a provider value, a URL or a key."""
    from .client import UnsupportedEndpoint
    from .errors import (CircuitOpen, ProviderDeclaredErrors, ProviderHTTPError, RequestQueueFull,
                         classify_declared_errors)
    from .quota import QuotaExceeded, QuotaConcurrencyBusy
    if isinstance(exc, QuotaExceeded):
        return _local_quota_error(exc)
    if isinstance(exc, QuotaConcurrencyBusy):
        return {"code": "LOCAL_CONCURRENCY_BUSY"}
    if isinstance(exc, CircuitOpen):
        return {"code": "CIRCUIT_OPEN"}
    if isinstance(exc, RequestQueueFull):
        return {"code": "QUEUE_TIMEOUT"}
    if isinstance(exc, ProviderHTTPError) and type(exc.status) is int:
        status = exc.status
        if status in (401, 403):
            return {"code": "PROVIDER_AUTH_FAILED", "http_status": status}
        if status == 429:
            out: dict[str, Any] = {"code": "PROVIDER_RATE_LIMITED", "http_status": status}
            if type(exc.retry_after_seconds) is int:
                out["retry_after_seconds"] = exc.retry_after_seconds
            return out
        if 500 <= status <= 599:
            return {"code": "PROVIDER_SERVER_ERROR", "http_status": status}
        if 400 <= status <= 499:
            # Our request was refused (e.g. a wrong parameter mapping): a request defect.
            return {"code": "PROVIDER_REQUEST_REJECTED", "http_status": status}
    if isinstance(exc, ProviderDeclaredErrors):
        keys, category = classify_declared_errors(exc.error_keys, parameters)
        return {"code": "PROVIDER_DECLARED_ERRORS", "keys": keys, "category": category}
    if isinstance(exc, UnsupportedEndpoint):
        return {"code": "UNSUPPORTED_ENDPOINT"}
    safe_code = getattr(exc, "code", None)
    if isinstance(safe_code, str) and safe_code in _PASS_THROUGH_CODES:
        return {"code": safe_code}
    if isinstance(exc, ValueError):
        return {"code": "REQUEST_VALIDATION_FAILED"}  # refused locally before any request
    return {"code": "PROVIDER_REQUEST_FAILED"}


def _local_quota_error(exc: Any) -> dict[str, Any]:
    """The binding LOCAL limit of the shared coordinator (never the provider's own limit).

    LOCAL_RATE_LIMITED: the rolling 60 s window (or the provider-zero marker) is full;
    retry_after_seconds comes from the coordinator's attempt times. LOCAL_DAILY_QUOTA_EXHAUSTED:
    the UTC calendar-day bucket is spent; reset_at_utc is the next UTC midnight. A refusal
    without a known window is reported as daily, with no reset time: the conservative reading
    (never invite a retry the coordinator did not compute)."""
    window = getattr(exc, "window", None)
    retry = getattr(exc, "retry_after_seconds", None)
    reset = getattr(exc, "reset_at_utc", None)
    if window == "minute":
        out: dict[str, Any] = {"code": "LOCAL_RATE_LIMITED", "window": "ROLLING_60_SECONDS"}
        if type(retry) is int and 1 <= retry <= 60:
            out["retry_after_seconds"] = retry
        return out
    out = {"code": "LOCAL_DAILY_QUOTA_EXHAUSTED", "window": "UTC_CALENDAR_DAY",
           "reset_at_utc": reset if window == "day" and isinstance(reset, str) else None}
    if window == "day" and type(retry) is int and 1 <= retry <= 86_400:
        out["retry_after_seconds"] = retry
    return out


def _attempts(source: Any) -> int:
    n = source.get("provider_attempts") if isinstance(source, dict) else getattr(source, "provider_attempts", None)
    return n if type(n) is int and 0 <= n <= 3 else 0


_SUBJECT_KEYS = ("fixture", "id", "ids", "bet", "bookmaker")


def _subject(params: Mapping[str, Any]) -> dict[str, Any]:
    """The fixture or market identifiers of the request, exactly as validated."""
    return {k: params[k] for k in _SUBJECT_KEYS if k in params}


def _iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0 else None


def _reply_timing(data: dict[str, Any]) -> dict[str, Any]:
    """Times of a real reply as the resilience layer recorded them: the bridge's acquisition
    time (kept on a cache HIT), the provider's own timestamp only if it published one."""
    out: dict[str, Any] = {}
    if _iso_utc(data.get("retrieved_at_utc")) is not None:
        out["retrieved_at_utc"] = data["retrieved_at_utc"]
    if _iso_utc(data.get("provider_timestamp_utc")) is not None \
            and data.get("provider_timestamp_source") == "PROVIDER_HTTP_DATE_HEADER":
        out["provider_timestamp_utc"] = data["provider_timestamp_utc"]
        out["provider_timestamp_source"] = "PROVIDER_HTTP_DATE_HEADER"
    age = data.get("cache_age_seconds")
    if data.get("cache_status") == "HIT" and isinstance(age, (int, float)) and not isinstance(age, bool) and age >= 0:
        out["cache_age_seconds"] = age
    return out


def _finish_timing(result: dict[str, Any]) -> None:
    """Server-side times are labelled as bridge-clock times; the provider timestamp is never
    filled from them. age_seconds = responded - acquired (bridge clock), None if nothing was acquired."""
    responded = datetime.now(timezone.utc)
    result["responded_at_utc"] = responded.isoformat()
    acquired = _iso_utc(result.get("retrieved_at_utc"))
    result["age_seconds"] = round(max(0.0, (responded - acquired).total_seconds()), 3) if acquired else None
    result["time_sources"] = {
        "retrieved_at_utc": "BRIDGE_SERVER_CLOCK_AT_ACQUISITION" if acquired else None,
        "responded_at_utc": "BRIDGE_SERVER_CLOCK",
        "provider_timestamp_utc": result.get("provider_timestamp_source"),
        "cache_age_seconds": "BRIDGE_CACHE_CLOCK" if result.get("cache_age_seconds") is not None else None,
    }


def _real_calls(data: dict[str, Any]) -> int:
    """Provider requests sent for THIS call: none on a cache HIT or when the call joined
    another caller's in-flight request (that caller reports them); otherwise the real attempts."""
    if data.get("cache_status") == "HIT" or data.get("shared_flight") is True:
        return 0
    return _attempts(data) or 1


class MockClient(Protocol):
    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        ...


def register_tools(server: Any, settings: Settings, *, test_mock: MockClient | None = None, client: Any | None = None) -> tuple[str, ...]:
    """Register additive typed tools, or none when optional module disabled."""
    if not settings.enabled:
        return ()

    names: list[str] = []

    def add(func):
        server.tool()(func)
        names.append(func.__name__)
        return func

    async def execute(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await _execute_core(tool, args)
        _finish_timing(result)
        return result

    async def _execute_core(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        at = datetime.now(timezone.utc).isoformat()
        try:
            contract, params = validate(tool, args)
        except ParameterError as exc:
            return {
                "provider": "API-FOOTBALL", "tool": tool, "status": "INVALID_INPUT",
                "error": {"code": str(exc)},
                "retrieved_at_utc": None, "data": None, "network_calls_made": 0,
            }
        result: dict[str, Any] = {
            "provider": "API-FOOTBALL",
            "tool": tool,
            "endpoint": contract.path,
            "parameters": params,
            "status": "REAL_API_TEST_NOT_AUTHORIZED",
            "source_contract": contract.confidence,
            "documentation_url": SOURCE,
            "documentation_version": VERSION,
            # Set only when a reply was acquired (see _reply_timing); None means nothing was.
            "retrieved_at_utc": None,
            "provider_timestamp_utc": None,
            "provider_timestamp_source": None,
            "cache_status": "NOT_USED",
            "cache_age_seconds": None,
            "request_subject": _subject(params),
            "quota_remaining": None,
            "pagination": None,
            "data": None,
            "network_calls_made": 0,
            "warnings": [
                _OFFLINE_WARNING,
                "Provider calls require separate owner authorization and key.",
            ],
        }
        if test_mock is None:
            if not settings.api_key:
                result["status"] = "KEY_NOT_CONFIGURED"
                return result
            if not settings.live_authorized:
                result["status"] = "LIVE_NOT_AUTHORIZED"
                return result
            if client is None:
                result["status"] = "PROVIDER_CLIENT_UNAVAILABLE"
                return result
        # test_mock is supplied only by local unit tests, never from MCP
        # arguments or environment. Preserve raw simulated data losslessly.
        received = 0  # real attempts behind a reply that later fails local checks
        try:
            if test_mock is not None:
                data = await test_mock.get(contract.path, params)
                status = "MOCK_VERIFIED"
                cache_status = "TEST_FIXTURE"
                result["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
            else:
                data = await client.get(contract.path, params)
                status = "REAL_API_RESPONSE"
                cache_status = data.get("cache_status", "UNKNOWN") if isinstance(data, dict) else "UNKNOWN"
                if isinstance(data, dict):
                    received = _real_calls(data)
                    result.update(_reply_timing(data))
            if not isinstance(data, dict):
                from .errors import ProviderDataError
                raise ProviderDataError("PROVIDER_REPLY_CONTRACT_INVALID")
            size = len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            if size > _MAX_MCP_DATA_BYTES and tool in ("apifootball_odds", "apifootball_live_odds"):
                # Never pretend a truncated list of prices is complete.
                from .odds_manifest import live_odds_manifest, odds_page_manifest
                try:
                    manifest = (odds_page_manifest if tool == "apifootball_odds" else live_odds_manifest)(data)
                except ValueError as exc:
                    manifest = None
                    manifest_error = str(exc)
                if manifest is not None and len(json.dumps(
                        manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) <= _MAX_MCP_DATA_BYTES:
                    result["status"] = "PARTIAL_ODDS_MANIFEST"
                    # Keep provenance explicit: a mock manifest is not real evidence.
                    result["source_status"] = status
                    result["data"] = manifest
                    result["pagination"] = manifest["paging"]
                    result["http_status"] = data.get("http_status")
                    result["network_calls_made"] = 0 if status == "MOCK_VERIFIED" else _real_calls(data)
                    result["cache_status"] = cache_status
                    if status == "REAL_API_RESPONSE":
                        result["quota_remaining"] = {
                            "day": data.get("quota_remaining_provider_day"),
                            "minute": data.get("quota_remaining_provider_minute"),
                        }
                    return result
                if manifest is None:
                    result["manifest_error"] = {"code": manifest_error}
                else:
                    result["manifest_error"] = {"code": "ODDS_MANIFEST_EXCEEDS_MCP_SIZE_LIMIT"}
            if size > _MAX_MCP_DATA_BYTES:
                result["status"] = "PAYLOAD_TOO_LARGE"
                result["error"] = {"code": "MOCK_RESPONSE_TOO_LARGE" if status == "MOCK_VERIFIED" else "PROVIDER_RESPONSE_TOO_LARGE"}
                # Keep the provider's page position so the client can still navigate (numbers only).
                envelope = data.get("data", data) if status == "REAL_API_RESPONSE" else data
                paging = envelope.get("paging") if isinstance(envelope, dict) else None
                if isinstance(paging, dict) and type(paging.get("current")) is int and type(paging.get("total")) is int:
                    result["pagination"] = {"current": paging["current"], "total": paging["total"]}
                if status == "REAL_API_RESPONSE":
                    result["network_calls_made"] = _real_calls(data)
                    result["warnings"] = [w for w in result["warnings"] if w != _OFFLINE_WARNING]
                return result
            result["status"] = status
            if status == "REAL_API_RESPONSE":
                # The offline-placeholder warnings contradict a real reply.
                result["warnings"] = [
                    "Provider data returned as received; coverage, availability and update timing vary by league and fixture.",
                ]
            provider_data = data.get("data", data) if status == "REAL_API_RESPONSE" else data
            # The account endpoint exposes private name/email fields upstream:
            # expose only non-personal subscription and quota counters via MCP.
            if tool == "apifootball_status" and status == "REAL_API_RESPONSE":
                payload = provider_data.get("response") if isinstance(provider_data, dict) else None
                from .errors import ProviderDataError
                if not isinstance(payload, dict):
                    raise ProviderDataError("INVALID_STATUS_RESPONSE")
                subscription = payload.get("subscription")
                requests = payload.get("requests")
                if not isinstance(subscription, dict) or not isinstance(requests, dict):
                    raise ProviderDataError("INVALID_STATUS_RESPONSE")
                result["data"] = {
                    "subscription": {k: subscription.get(k) for k in ("plan", "active", "end")},
                    "requests": {k: requests.get(k) for k in ("current", "limit_day")},
                }
            else:
                result["data"] = provider_data
            result["http_status"] = data.get("http_status")
            # Real replies wrap the provider envelope: paging lives in provider_data.
            provider_paging = provider_data.get("paging") if isinstance(provider_data, dict) else None
            result["pagination"] = provider_paging if isinstance(provider_paging, dict) else data.get("pagination", data.get("paging"))
            result["cache_status"] = cache_status
            result["network_calls_made"] = 0 if status == "MOCK_VERIFIED" else _real_calls(data)
            if status == "REAL_API_RESPONSE":
                result["quota_remaining"] = {
                    "day": data.get("quota_remaining_provider_day"),
                    "minute": data.get("quota_remaining_provider_minute"),
                }
            return result
        except Exception as exc:
            # Never reflect provider bodies, exception messages or secrets.
            if test_mock is not None:
                result["status"] = "MOCK_ERROR"
                result["error"] = {"code": "MOCK_FIXTURE_UNAVAILABLE"}
            else:
                result["status"] = "PROVIDER_ERROR"
                result["error"] = _error_for(exc, contract.fields)
                if isinstance(result["error"].get("http_status"), int):
                    result["http_status"] = result["error"]["http_status"]
                # Real attempts of the resilience layer (retries included); 0 when none was sent.
                result["network_calls_made"] = _attempts(exc) if hasattr(exc, "provider_attempts") else received
                result["warnings"] = [w for w in result["warnings"] if w != _OFFLINE_WARNING]
                day = getattr(exc, "quota_remaining_day", None)
                minute = getattr(exc, "quota_remaining_minute", None)
                if type(day) is int or type(minute) is int:  # a declared-errors reply carried headers
                    result["quota_remaining"] = {"day": day if type(day) is int else None,
                                                 "minute": minute if type(minute) is int else None}
            return result

    @add
    async def apifootball_health() -> dict[str, Any]:
        """Local health and read-only shared quota readiness; no provider request."""
        quota_state = "NOT_CONNECTED"
        if settings.real_requests_authorized and client is not None:
            quota_state = "UNKNOWN"
            try:
                if getattr(client, "quota", None) is not None:
                    await asyncio.wait_for(client.quota.snapshot(settings.api_key), timeout=6.0)
                    quota_state = "READY"
            except Exception:
                quota_state = "UNAVAILABLE"
        # FCB-WP-009 (R2): the database ROLE NAMES the runtime connects as (no host, DSN or error).
        from bridge.db_identity import database_identity_async
        db_identity = await database_identity_async()
        return {
            "provider": "API-FOOTBALL",
            "database_identity": db_identity,
            "module": "FCB-APIFOOTBALL-INTEGRATION-CERT-01/PR-03",
            "status": "LIVE_ENABLED" if settings.real_requests_authorized and client is not None else ("LIVE_NOT_AUTHORIZED" if settings.configured else "KEY_NOT_CONFIGURED"),
            "enabled": True,
            "key_present": bool(settings.api_key),
            "provider_requests_authorized": bool(settings.real_requests_authorized and client is not None),
            "network_calls_made": 0,
            "quota_coordinator": quota_state,
            "http_transport": "PINNED_HTTPX" if settings.real_requests_authorized and client is not None else "NOT_CONNECTED_TO_MCP",
            "documented_contract": "PARTIALLY_RECONCILED_OFFICIAL",
            "mcp_contract_audit": {k: v for k, v in audit_contract_coverage().items() if k != "routes"},
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "limits_configured": {
                "per_minute": settings.max_rpm,
                "per_day": settings.max_daily_requests,
                "max_concurrency": settings.max_concurrency,
            },
            "warnings": [
                "Live calls require the independent server-side authorization gate.",
                "Coverage and field availability require provider evidence.",
            ],
        }

    @add
    async def apifootball_status() -> dict[str, Any]:
        """Account plan and request counters (/status); no API key or personal data in output."""
        return await execute("apifootball_status", {})

    @add
    async def apifootball_countries(name: str | None = None, code: str | None = None, search: str | None = None) -> dict[str, Any]:
        """Countries available for soccer leagues; offline typed filters."""
        return await execute("apifootball_countries", dict(name=name, code=code, search=search))

    @add
    async def apifootball_leagues(id: int | None = None, name: str | None = None, country: str | None = None, code: str | None = None, season: int | None = None, team: int | None = None, type: str | None = None, current: bool | None = None, search: str | None = None, last: int | None = None) -> dict[str, Any]:
        """Soccer league catalog and season-specific coverage flags (not fixture guarantees). type: league|cup; search >= 3 chars; last <= 99."""
        return await execute("apifootball_leagues", dict(id=id, name=name, country=country, code=code, season=season, team=team, type=type, current=current, search=search, last=last))

    @add
    async def apifootball_seasons() -> dict[str, Any]:
        """Available soccer season years; offline."""
        return await execute("apifootball_seasons", {})

    @add
    async def apifootball_fixtures(id: int | None = None, date: str | None = None, league: int | None = None, season: int | None = None, team: int | None = None, live: str | None = None, timezone: str | None = None, last: int | None = None, next: int | None = None, status: str | None = None, ids: str | None = None, venue: int | None = None, round: str | None = None, from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        """Fixtures/live calendar. Requires targeted filter; 'live' only 'all'; UTC timezone."""
        return await execute("apifootball_fixtures", dict(id=id, date=date, league=league, season=season, team=team, live=live, timezone=timezone, last=last, next=next, status=status, ids=ids, venue=venue, round=round, **{"from": from_date, "to": to_date}))

    @add
    async def apifootball_fixture_statistics(fixture: int, team: int | None = None, type: str | None = None, half: bool | None = None) -> dict[str, Any]:
        """Fixture statistics; optional statistic type; half is provider-supported for seasons from 2024 onward."""
        return await execute("apifootball_fixture_statistics", dict(fixture=fixture, team=team, type=type, half=half))

    @add
    async def apifootball_fixture_events(fixture: int, team: int | None = None, player: int | None = None, type: str | None = None) -> dict[str, Any]:
        """Fixture event feed (goals, cards, substitutions); optional team/player/type filters."""
        return await execute("apifootball_fixture_events", dict(fixture=fixture, team=team, player=player, type=type))

    @add
    async def apifootball_fixture_lineups(fixture: int, team: int | None = None, player: int | None = None, type: str | None = None) -> dict[str, Any]:
        """Fixture lineups and formations (availability varies by competition); optional team/player/type filters."""
        return await execute("apifootball_fixture_lineups", dict(fixture=fixture, team=team, player=player, type=type))

    @add
    async def apifootball_teams(id: int | None = None, name: str | None = None, league: int | None = None, season: int | None = None, country: str | None = None, search: str | None = None, code: str | None = None, venue: int | None = None) -> dict[str, Any]:
        """Find teams by ID, name, country, competition or search (>= 3 chars). Requires a filter."""
        return await execute("apifootball_teams", dict(id=id, name=name, league=league, season=season, country=country, search=search, code=code, venue=venue))

    @add
    async def apifootball_team_statistics(league: int, season: int, team: int, date: str | None = None) -> dict[str, Any]:
        """Team statistics for an identified league-season, optional as-of date."""
        return await execute("apifootball_team_statistics", dict(league=league, season=season, team=team, date=date))

    @add
    async def apifootball_players(id: int | None = None, team: int | None = None, league: int | None = None, season: int | None = None, search: str | None = None, page: int | None = None) -> dict[str, Any]:
        """Player statistics per season (20 per page). season needs id, league or team; search (>= 4 chars) needs league or team."""
        return await execute("apifootball_players", dict(id=id, team=team, league=league, season=season, search=search, page=page))

    @add
    async def apifootball_injuries(league: int | None = None, season: int | None = None, fixture: int | None = None, team: int | None = None, player: int | None = None, date: str | None = None, ids: str | None = None, timezone: str | None = None) -> dict[str, Any]:
        """Injuries by fixture, fixture ids (max 20), date, or league/team/player with season (season required with those)."""
        return await execute("apifootball_injuries", dict(league=league, season=season, fixture=fixture, team=team, player=player, date=date, ids=ids, timezone=timezone))

    @add
    async def apifootball_predictions(fixture: int) -> dict[str, Any]:
        """Provider prediction for one fixture; do not conflate with betting odds."""
        return await execute("apifootball_predictions", dict(fixture=fixture))

    @add
    async def apifootball_odds(fixture: int | None = None, league: int | None = None, season: int | None = None, date: str | None = None, bookmaker: int | None = None, bet: int | None = None, page: int | None = None, timezone: str | None = None) -> dict[str, Any]:
        """Pre-match odds with explicit pagination; availability and historical retention vary."""
        return await execute("apifootball_odds", dict(fixture=fixture, league=league, season=season, date=date, bookmaker=bookmaker, bet=bet, page=page, timezone=timezone))

    @add
    async def apifootball_live_odds(fixture: int | None = None, league: int | None = None, bet: int | None = None) -> dict[str, Any]:
        """In-play odds (fixture or league required). Oversized replies return a price-free PARTIAL_ODDS_MANIFEST with bet ids; fetch prices with fixture+bet."""
        return await execute("apifootball_live_odds", dict(fixture=fixture, league=league, bet=bet))

    @add
    async def apifootball_bookmakers(id: int | None = None, search: str | None = None) -> dict[str, Any]:
        """Bookmakers catalog for odds (official v3.9.3 parameters: id, search)."""
        return await execute("apifootball_bookmakers", dict(id=id, search=search))

    @add
    async def apifootball_bets(id: int | None = None, search: str | None = None) -> dict[str, Any]:
        """Pre-match bet/market IDs; do not reuse blindly for odds/live."""
        return await execute("apifootball_bets", dict(id=id, search=search))

    @add
    async def apifootball_coverage_report(id: int, season: int) -> dict[str, Any]:
        """Season/league coverage flags only; these do NOT certify complete real field coverage."""
        return await execute("apifootball_coverage_report", dict(id=id, season=season))

    @add
    async def apifootball_timezones() -> dict[str, Any]:
        """Available timezones accepted by the football fixtures API (offline catalog)."""
        return await execute("apifootball_timezones", {})

    @add
    async def apifootball_team_seasons(team: int) -> dict[str, Any]:
        """Available season IDs for one team; never implies complete statistics coverage."""
        return await execute("apifootball_team_seasons", dict(team=team))

    @add
    async def apifootball_team_countries() -> dict[str, Any]:
        """Countries available in the football teams catalog."""
        return await execute("apifootball_team_countries", {})

    @add
    async def apifootball_venues(id: int | None = None, name: str | None = None, city: str | None = None, country: str | None = None, search: str | None = None) -> dict[str, Any]:
        """Football venues and stadiums, with bounded venue-identifying filters."""
        return await execute("apifootball_venues", dict(id=id, name=name, city=city, country=country, search=search))

    @add
    async def apifootball_standings(season: int, league: int | None = None, team: int | None = None) -> dict[str, Any]:
        """Standings for a season (required) by league and/or team (one of them is required)."""
        return await execute("apifootball_standings", dict(league=league, season=season, team=team))

    @add
    async def apifootball_fixture_rounds(league: int, season: int, current: bool | None = None, dates: bool | None = None, timezone: str | None = None) -> dict[str, Any]:
        """Competition rounds, optionally current or with round dates; UTC only."""
        return await execute("apifootball_fixture_rounds", dict(league=league, season=season, current=current, dates=dates, timezone=timezone))

    @add
    async def apifootball_fixture_headtohead(h2h: str, date: str | None = None, league: int | None = None, season: int | None = None, last: int | None = None, next: int | None = None, from_date: str | None = None, to_date: str | None = None, status: str | None = None, venue: int | None = None, timezone: str | None = None) -> dict[str, Any]:
        """Fixtures between two teams identified as 'teamID-teamID', with optional date/league/season/range filters."""
        return await execute("apifootball_fixture_headtohead", dict(h2h=h2h, date=date, league=league, season=season, last=last, next=next, status=status, venue=venue, timezone=timezone, **{"from": from_date, "to": to_date}))

    @add
    async def apifootball_fixture_players(fixture: int, team: int | None = None) -> dict[str, Any]:
        """Per-player match statistics; fixture ID required."""
        return await execute("apifootball_fixture_players", dict(fixture=fixture, team=team))

    @add
    async def apifootball_coaches(id: int | None = None, team: int | None = None, search: str | None = None) -> dict[str, Any]:
        """Coaches by numeric ID, team, or name search; one filter required."""
        return await execute("apifootball_coaches", dict(id=id, team=team, search=search))

    @add
    async def apifootball_player_seasons(player: int | None = None) -> dict[str, Any]:
        """Seasons available for player statistics, optionally for one player."""
        return await execute("apifootball_player_seasons", dict(player=player))

    @add
    async def apifootball_player_profiles(player: int | None = None, search: str | None = None, page: int | None = None) -> dict[str, Any]:
        """Player profiles (250 per page); by player id or lastname search (>= 4 chars), or paginate all."""
        return await execute("apifootball_player_profiles", dict(player=player, search=search, page=page))

    @add
    async def apifootball_player_squads(team: int | None = None, player: int | None = None) -> dict[str, Any]:
        """Current squads filtered by team or player ID, without season-statistics inference."""
        return await execute("apifootball_player_squads", dict(team=team, player=player))

    @add
    async def apifootball_player_teams(player: int) -> dict[str, Any]:
        """Teams and seasons in a player's career; player ID required."""
        return await execute("apifootball_player_teams", dict(player=player))

    @add
    async def apifootball_top_scorers(league: int, season: int) -> dict[str, Any]:
        """League-season leading goal scorers; results only documented, not empirically verified."""
        return await execute("apifootball_top_scorers", dict(league=league, season=season))

    @add
    async def apifootball_top_assists(league: int, season: int) -> dict[str, Any]:
        """League-season leading assists, distinct from scoring and match predictions."""
        return await execute("apifootball_top_assists", dict(league=league, season=season))

    @add
    async def apifootball_top_yellow_cards(league: int, season: int) -> dict[str, Any]:
        """League-season player yellow card leaderboard."""
        return await execute("apifootball_top_yellow_cards", dict(league=league, season=season))

    @add
    async def apifootball_top_red_cards(league: int, season: int) -> dict[str, Any]:
        """League-season player red card leaderboard."""
        return await execute("apifootball_top_red_cards", dict(league=league, season=season))

    @add
    async def apifootball_transfers(player: int | None = None, team: int | None = None) -> dict[str, Any]:
        """Player or club transfer records; no fabricated transfer fees."""
        return await execute("apifootball_transfers", dict(player=player, team=team))

    @add
    async def apifootball_trophies(player: int | None = None, players: str | None = None, coach: int | None = None, coachs: str | None = None) -> dict[str, Any]:
        """Trophies for a player/coach or up to 20 ids ('id-id-id'); no guarantee of complete history."""
        return await execute("apifootball_trophies", dict(player=player, players=players, coach=coach, coachs=coachs))

    @add
    async def apifootball_sidelined(player: int | None = None, players: str | None = None, coach: int | None = None, coachs: str | None = None) -> dict[str, Any]:
        """Sidelined periods (injuries/suspensions) for a player/coach or up to 20 ids ('id-id-id')."""
        return await execute("apifootball_sidelined", dict(player=player, players=players, coach=coach, coachs=coachs))

    @add
    async def apifootball_odds_mapping(page: int | None = None) -> dict[str, Any]:
        """Pre-match odds fixture mapping (provider-paginated, 100 rows/page); only `page`, no filter invented."""
        return await execute("apifootball_odds_mapping", dict(page=page))

    @add
    async def apifootball_live_bets(id: str | None = None, search: str | None = None) -> dict[str, Any]:
        """In-play odds bet type IDs; NOT interchangeable with pre-match bet IDs."""
        return await execute("apifootball_live_bets", dict(id=id, search=search))

    @add
    async def apifootball_quota_report() -> dict[str, Any]:
        """Configured design limits, NOT provider-observed balance or a live DB check."""
        return {
            "provider": "API-FOOTBALL",
            "status": "CONFIGURED_LIMITS_ONLY",
            "provider_quota_verified": False,
            "live_requests_enabled": bool(settings.real_requests_authorized and client is not None),
            "remaining_provider_quota": None,
            "configured_limits": {
                "per_minute": settings.max_rpm,
                "per_day": settings.max_daily_requests,
                "max_concurrency": settings.max_concurrency,
            },
            "network_calls_made": 0,
        }

    assert set(ENDPOINTS).issubset(names)
    # Forty named contracts (including an extra /leagues coverage view)
    # address exactly the official 38 football paths plus /status.
    # Two local diagnostic tools make 42 MCP names in all.
    assert len(names) == 42 and len(set(names)) == 42
    assert {endpoint.path for endpoint in ENDPOINTS.values()} == ALLOWED_PATHS
    return tuple(names)
