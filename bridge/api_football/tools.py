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
        at = datetime.now(timezone.utc).isoformat()
        try:
            contract, params = validate(tool, args)
        except ParameterError as exc:
            return {
                "provider": "API-FOOTBALL", "tool": tool, "status": "INVALID_INPUT",
                "error": {"code": str(exc)},
                "retrieved_at_utc": at, "data": None, "network_calls_made": 0,
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
            "retrieved_at_utc": at,
            "provider_timestamp_utc": None,
            "cache_status": "NOT_USED",
            "quota_remaining": None,
            "pagination": None,
            "data": None,
            "network_calls_made": 0,
            "warnings": [
                "Offline PR-03 tool: never claim real coverage, live odds or PIT data.",
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
        try:
            if test_mock is not None:
                data = await test_mock.get(contract.path, params)
                status = "MOCK_VERIFIED"
                cache_status = "TEST_FIXTURE"
            else:
                data = await client.get(contract.path, params)
                status = "REAL_API_RESPONSE"
                cache_status = data.get("cache_status", "UNKNOWN") if isinstance(data, dict) else "UNKNOWN"
            if not isinstance(data, dict):
                raise TypeError("Bad provider response type")
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
                    result["network_calls_made"] = 0 if status == "MOCK_VERIFIED" or cache_status == "HIT" else 1
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
                if not isinstance(payload, dict):
                    raise ValueError("INVALID_STATUS_RESPONSE")
                subscription = payload.get("subscription")
                requests = payload.get("requests")
                if not isinstance(subscription, dict) or not isinstance(requests, dict):
                    raise ValueError("INVALID_STATUS_RESPONSE")
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
            result["network_calls_made"] = 0 if status == "MOCK_VERIFIED" or cache_status == "HIT" else 1
            if status == "REAL_API_RESPONSE":
                result["quota_remaining"] = {
                    "day": data.get("quota_remaining_provider_day"),
                    "minute": data.get("quota_remaining_provider_minute"),
                }
            return result
        except Exception as exc:
            # Never reflect provider bodies, exception messages or secrets.
            from .quota import QuotaExceeded, QuotaConcurrencyBusy
            if test_mock is not None:
                result["status"] = "MOCK_ERROR"
                result["error"] = {"code": "MOCK_FIXTURE_UNAVAILABLE"}
            else:
                result["status"] = "PROVIDER_ERROR"
                safe_code = getattr(exc, "code", None)
                result["error"] = {"code": ("LOCAL_QUOTA_EXHAUSTED" if isinstance(exc, QuotaExceeded) else
                    "LOCAL_CONCURRENCY_BUSY" if isinstance(exc, QuotaConcurrencyBusy) else
                    safe_code if isinstance(safe_code, str) and safe_code in {
                    "PROVIDER_TIMEOUT", "PROVIDER_NETWORK_ERROR", "PROVIDER_REDIRECT_REJECTED",
                    "PROVIDER_RESPONSE_TOO_LARGE", "PROVIDER_INVALID_JSON",
                    "PROVIDER_JSON_NOT_OBJECT", "PROVIDER_DECLARED_ERRORS",
                    "PROVIDER_RESPONSE_FIELD_MISSING", "PROVIDER_INVALID_PAGINATION",
                    "QUOTA_COORDINATOR_UNAVAILABLE", "QUOTA_RELEASE_FAILED",
                    "QUOTA_RECONCILIATION_FAILED", "PROVIDER_INTERNAL_ERROR",
                } else "PROVIDER_REQUEST_FAILED")}
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
        return {
            "provider": "API-FOOTBALL",
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
    async def apifootball_leagues(id: int | None = None, season: int | None = None, country: str | None = None, team: int | None = None, search: str | None = None) -> dict[str, Any]:
        """Soccer league catalog and season-specific coverage flags (not fixture guarantees)."""
        return await execute("apifootball_leagues", dict(id=id, season=season, country=country, team=team, search=search))

    @add
    async def apifootball_seasons() -> dict[str, Any]:
        """Available soccer season years; offline."""
        return await execute("apifootball_seasons", {})

    @add
    async def apifootball_fixtures(id: int | None = None, date: str | None = None, league: int | None = None, season: int | None = None, team: int | None = None, live: str | None = None, timezone: str | None = None, last: int | None = None, next: int | None = None, status: str | None = None, ids: str | None = None, venue: int | None = None, round: str | None = None, from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
        """Fixtures/live calendar. Requires targeted filter; 'live' only 'all'; UTC timezone."""
        return await execute("apifootball_fixtures", dict(id=id, date=date, league=league, season=season, team=team, live=live, timezone=timezone, last=last, next=next, status=status, ids=ids, venue=venue, round=round, **{"from": from_date, "to": to_date}))

    @add
    async def apifootball_fixture_statistics(fixture: int, team: int | None = None, half: bool | None = None) -> dict[str, Any]:
        """Fixture statistics; half is provider-supported for seasons from 2024 onward."""
        return await execute("apifootball_fixture_statistics", dict(fixture=fixture, team=team, half=half))

    @add
    async def apifootball_fixture_events(fixture: int) -> dict[str, Any]:
        """Fixture event feed including goals/cards; source fields not certified until live reads."""
        return await execute("apifootball_fixture_events", dict(fixture=fixture))

    @add
    async def apifootball_fixture_lineups(fixture: int) -> dict[str, Any]:
        """Fixture lineups and formations (availability varies by competition)."""
        return await execute("apifootball_fixture_lineups", dict(fixture=fixture))

    @add
    async def apifootball_teams(id: int | None = None, league: int | None = None, season: int | None = None, search: str | None = None, code: str | None = None, venue: int | None = None) -> dict[str, Any]:
        """Find teams by ID, competition or name. Requires a filter."""
        return await execute("apifootball_teams", dict(id=id, league=league, season=season, search=search, code=code, venue=venue))

    @add
    async def apifootball_team_statistics(league: int, season: int, team: int, date: str | None = None) -> dict[str, Any]:
        """Team statistics for an identified league-season, optional as-of date."""
        return await execute("apifootball_team_statistics", dict(league=league, season=season, team=team, date=date))

    @add
    async def apifootball_players(id: int | None = None, team: int | None = None, season: int | None = None, page: int | None = None) -> dict[str, Any]:
        """Player/season data. Parameters pending official schema reconciliation."""
        return await execute("apifootball_players", dict(id=id, team=team, season=season, page=page))

    @add
    async def apifootball_injuries(fixture: int | None = None, league: int | None = None, season: int | None = None, team: int | None = None, ids: str | None = None) -> dict[str, Any]:
        """Injuries by fixture/league/team. Parameter combination pending verification."""
        return await execute("apifootball_injuries", dict(fixture=fixture, league=league, season=season, team=team, ids=ids))

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
        """Bookmakers catalog for odds; query parameters pending official reconciliation."""
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
    async def apifootball_standings(league: int, season: int, team: int | None = None) -> dict[str, Any]:
        """League-season standings; league and season required to bound access."""
        return await execute("apifootball_standings", dict(league=league, season=season, team=team))

    @add
    async def apifootball_fixture_rounds(league: int, season: int, current: bool | None = None, dates: bool | None = None, timezone: str | None = None) -> dict[str, Any]:
        """Competition rounds, optionally current or with round dates; UTC only."""
        return await execute("apifootball_fixture_rounds", dict(league=league, season=season, current=current, dates=dates, timezone=timezone))

    @add
    async def apifootball_fixture_headtohead(h2h: str, last: int | None = None, next: int | None = None, status: str | None = None, venue: int | None = None) -> dict[str, Any]:
        """Historical fixtures between two teams identified as 'homeID-awayID'."""
        return await execute("apifootball_fixture_headtohead", dict(h2h=h2h, last=last, next=next, status=status, venue=venue))

    @add
    async def apifootball_fixture_players(fixture: int, team: int | None = None) -> dict[str, Any]:
        """Per-player match statistics; fixture ID required."""
        return await execute("apifootball_fixture_players", dict(fixture=fixture, team=team))

    @add
    async def apifootball_coaches(id: int | None = None, team: int | None = None, search: str | None = None) -> dict[str, Any]:
        """Coaches by numeric ID, team, or name search; one filter required."""
        return await execute("apifootball_coaches", dict(id=id, team=team, search=search))

    @add
    async def apifootball_player_seasons() -> dict[str, Any]:
        """Season catalog for player statistics; no provider data fetched in PR-03."""
        return await execute("apifootball_player_seasons", {})

    @add
    async def apifootball_player_profiles(page: int | None = None) -> dict[str, Any]:
        """Paginated player profile catalog; page parameter requires full OpenAPI reconciliation."""
        return await execute("apifootball_player_profiles", dict(page=page))

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
    async def apifootball_trophies(player: int | None = None, coach: int | None = None) -> dict[str, Any]:
        """Trophies for one player or coach; no guarantee of complete history."""
        return await execute("apifootball_trophies", dict(player=player, coach=coach))

    @add
    async def apifootball_sidelined(player: int | None = None, coach: int | None = None) -> dict[str, Any]:
        """Historical player or coach sidelined periods (injuries/suspensions)."""
        return await execute("apifootball_sidelined", dict(player=player, coach=coach))

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
