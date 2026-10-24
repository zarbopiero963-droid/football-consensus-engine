"""Typed football-only MCP research tools; NO live provider access.

PR-03 registers optional, authenticated MCP tools only when explicitly
enabled in environment. Without an injected *test-only* mock client every
endpoint returns an explicit non-network authorization gate. Neither
the HTTP transport nor the resilience engine is constructed here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping, Protocol

from .config import Settings
from .contracts import ENDPOINTS, SOURCE, VERSION, ParameterError, validate

_MAX_MCP_DATA_BYTES = 262_144


class MockClient(Protocol):
    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        ...


def register_tools(server: Any, settings: Settings, *, test_mock: MockClient | None = None) -> tuple[str, ...]:
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
        if not settings.configured:
            result["status"] = "KEY_NOT_CONFIGURED"
            return result
        if test_mock is None:
            return result
        # test_mock is supplied only by local unit tests, never from MCP
        # arguments or environment. Preserve raw simulated data losslessly.
        try:
            data = await test_mock.get(contract.path, params)
            if not isinstance(data, dict):
                raise TypeError("Bad mock response type")
            size = len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            if size > _MAX_MCP_DATA_BYTES:
                result["status"] = "PAYLOAD_TOO_LARGE"
                result["error"] = {"code": "MOCK_RESPONSE_TOO_LARGE"}
                return result
            result["status"] = "MOCK_VERIFIED"
            result["data"] = data
            result["pagination"] = data.get("paging")
            result["cache_status"] = "TEST_FIXTURE"
            return result
        except Exception:
            # Never include exception messages, mock secrets or provider body.
            result["status"] = "MOCK_ERROR"
            result["error"] = {"code": "MOCK_FIXTURE_UNAVAILABLE"}
            return result

    @add
    async def apifootball_health() -> dict[str, Any]:
        """Offline adapter health. Never contacts API-FOOTBALL."""
        return {
            "provider": "API-FOOTBALL",
            "module": "FCB-APIFOOTBALL-INTEGRATION-CERT-01/PR-03",
            "status": "MOCK_ONLY" if settings.configured else "KEY_NOT_CONFIGURED",
            "enabled": True,
            "key_present": bool(settings.api_key),
            "provider_requests_authorized": False,
            "network_calls_made": 0,
            "http_transport": "NOT_CONNECTED_TO_MCP",
            "documented_contract": "PARTIALLY_RECONCILED_OFFICIAL",
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "limits_configured_not_active": {
                "per_minute": settings.max_rpm,
                "per_day": settings.max_daily_requests,
                "max_concurrency": settings.max_concurrency,
            },
            "warnings": [
                "Only offline typed tools; no real provider calls authorized.",
                "Coverage and field availability require provider evidence.",
            ],
        }

    @add
    async def apifootball_status() -> dict[str, Any]:
        """Account status/quotas endpoint; offline, no API key in output."""
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
    async def apifootball_fixtures(id: int | None = None, date: str | None = None, league: int | None = None, season: int | None = None, team: int | None = None, live: str | None = None, timezone: str | None = None, last: int | None = None, next: int | None = None, status: str | None = None) -> dict[str, Any]:
        """Fixtures/live calendar. Requires targeted filter; 'live' only 'all'; UTC timezone."""
        return await execute("apifootball_fixtures", dict(id=id, date=date, league=league, season=season, team=team, live=live, timezone=timezone, last=last, next=next, status=status))

    @add
    async def apifootball_fixture_statistics(fixture: int, team: int | None = None) -> dict[str, Any]:
        """Fixture team statistics. No xG/xGoT availability implied; halftime not yet certified."""
        return await execute("apifootball_fixture_statistics", dict(fixture=fixture, team=team))

    @add
    async def apifootball_fixture_events(fixture: int) -> dict[str, Any]:
        """Fixture event feed including goals/cards; source fields not certified until live reads."""
        return await execute("apifootball_fixture_events", dict(fixture=fixture))

    @add
    async def apifootball_fixture_lineups(fixture: int) -> dict[str, Any]:
        """Fixture lineups and formations (availability varies by competition)."""
        return await execute("apifootball_fixture_lineups", dict(fixture=fixture))

    @add
    async def apifootball_teams(id: int | None = None, league: int | None = None, season: int | None = None, search: str | None = None) -> dict[str, Any]:
        """Find teams by ID, competition or name. Requires a filter."""
        return await execute("apifootball_teams", dict(id=id, league=league, season=season, search=search))

    @add
    async def apifootball_team_statistics(league: int, season: int, team: int, date: str | None = None) -> dict[str, Any]:
        """Team statistics for an identified league-season, optional as-of date."""
        return await execute("apifootball_team_statistics", dict(league=league, season=season, team=team, date=date))

    @add
    async def apifootball_players(id: int | None = None, team: int | None = None, season: int | None = None, page: int | None = None) -> dict[str, Any]:
        """Player/season data. Parameters pending official schema reconciliation."""
        return await execute("apifootball_players", dict(id=id, team=team, season=season, page=page))

    @add
    async def apifootball_injuries(fixture: int | None = None, league: int | None = None, season: int | None = None, team: int | None = None) -> dict[str, Any]:
        """Injuries by fixture/league/team. Parameter combination pending verification."""
        return await execute("apifootball_injuries", dict(fixture=fixture, league=league, season=season, team=team))

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
        """In-play odds; bookmaker/market history not guaranteed."""
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
    async def apifootball_quota_report() -> dict[str, Any]:
        """Configured design limits, NOT provider-observed balance or a live DB check."""
        return {
            "provider": "API-FOOTBALL",
            "status": "CONFIGURED_LIMITS_ONLY",
            "provider_quota_verified": False,
            "live_requests_enabled": False,
            "remaining_provider_quota": None,
            "configured_limits": {
                "per_minute": settings.max_rpm,
                "per_day": settings.max_daily_requests,
                "max_concurrency": settings.max_concurrency,
            },
            "network_calls_made": 0,
        }

    assert set(ENDPOINTS).issubset(names)
    return tuple(names)
