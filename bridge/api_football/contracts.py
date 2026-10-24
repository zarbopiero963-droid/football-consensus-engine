"""Curated and typed football-only API-FOOTBALL v3.9.3 MCP contracts.

Scope is intentionally narrower than the 38-path documentary catalog.
Each enabled tool only admits a stable, fixed provider endpoint and
named scalar query parameters; users cannot select a URL or raw path.
Parameter confidence is recorded per tool. No upstream calls at import.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, Mapping

from .catalog import documented_path

VERSION = "v3.9.3"
SOURCE = "https://www.api-football.com/documentation_v3"
_CONFIRMED = "DOCUMENTED_OFFICIAL"
_PENDING = "PENDING_PARAMETER_RECONCILIATION"
_POSITIVE = frozenset({
    "id", "league", "season", "team", "fixture", "player", "bookmaker",
    "bet", "page", "last", "next", "venue", "coach", "current",
})
_DATE_FIELDS = frozenset({"date", "from", "to"})
_TEXT = frozenset({"name", "country", "code", "search", "status", "type", "timezone", "live", "city"})
_BOOL_FIELDS = frozenset({"current", "dates"})
_H2H = re.compile(r"^[1-9][0-9]{0,9}-[1-9][0-9]{0,9}$")
_VALID_VALUE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


class ParameterError(ValueError):
    """Fixed-safe validation failure; never reflects user values or secrets."""


@dataclass(frozen=True)
class Endpoint:
    path: str
    fields: tuple[str, ...]
    required: tuple[str, ...] = ()
    confidence: str = _PENDING
    pagination: bool = False


ENDPOINTS: dict[str, Endpoint] = {
    "apifootball_status": Endpoint("/status", (), confidence=_CONFIRMED),
    "apifootball_countries": Endpoint("/countries", ("name", "code", "search"), confidence=_CONFIRMED),
    "apifootball_leagues": Endpoint("/leagues", ("id", "season", "country", "team", "search"), confidence=_CONFIRMED),
    "apifootball_seasons": Endpoint("/leagues/seasons", (), confidence=_CONFIRMED),
    "apifootball_fixtures": Endpoint("/fixtures", ("id", "date", "league", "season", "team", "live", "timezone", "last", "next", "status"), confidence=_CONFIRMED),
    "apifootball_fixture_statistics": Endpoint("/fixtures/statistics", ("fixture", "team"), ("fixture",), confidence=_CONFIRMED),
    "apifootball_fixture_events": Endpoint("/fixtures/events", ("fixture",), ("fixture",), confidence=_CONFIRMED),
    "apifootball_fixture_lineups": Endpoint("/fixtures/lineups", ("fixture",), ("fixture",), confidence=_CONFIRMED),
    "apifootball_teams": Endpoint("/teams", ("id", "league", "season", "search"), confidence=_CONFIRMED),
    "apifootball_team_statistics": Endpoint("/teams/statistics", ("league", "season", "team", "date"), ("league", "season", "team"), confidence=_CONFIRMED),
    "apifootball_players": Endpoint("/players", ("id", "team", "season", "page"), confidence=_PENDING, pagination=True),
    "apifootball_injuries": Endpoint("/injuries", ("fixture", "league", "season", "team"), confidence=_PENDING),
    "apifootball_predictions": Endpoint("/predictions", ("fixture",), ("fixture",), confidence=_CONFIRMED),
    "apifootball_odds": Endpoint("/odds", ("fixture", "league", "season", "date", "bookmaker", "bet", "page", "timezone"), confidence=_CONFIRMED, pagination=True),
    "apifootball_live_odds": Endpoint("/odds/live", ("fixture", "league", "bet"), confidence=_CONFIRMED),
    "apifootball_bookmakers": Endpoint("/odds/bookmakers", ("id", "search"), confidence=_PENDING),
    "apifootball_bets": Endpoint("/odds/bets", ("id", "search"), confidence=_PENDING),
    "apifootball_coverage_report": Endpoint("/leagues", ("id", "season"), ("id", "season"), confidence=_CONFIRMED),
    # All 22 remaining football endpoints. Confidence labels classify
    # provider-documentary evidence only, NEVER empirical availability.
    "apifootball_timezones": Endpoint("/timezone", (), confidence=_CONFIRMED),
    "apifootball_team_seasons": Endpoint("/teams/seasons", ("team",), ("team",), confidence=_CONFIRMED),
    "apifootball_team_countries": Endpoint("/teams/countries", (), confidence=_CONFIRMED),
    "apifootball_venues": Endpoint("/venues", ("id", "name", "city", "country", "search"), confidence=_CONFIRMED),
    "apifootball_standings": Endpoint("/standings", ("league", "season", "team"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_fixture_rounds": Endpoint("/fixtures/rounds", ("league", "season", "current", "dates", "timezone"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_fixture_headtohead": Endpoint("/fixtures/headtohead", ("h2h", "last", "next"), ("h2h",), confidence=_CONFIRMED),
    "apifootball_fixture_players": Endpoint("/fixtures/players", ("fixture", "team"), ("fixture",), confidence=_CONFIRMED),
    "apifootball_coaches": Endpoint("/coachs", ("id", "team", "search"), confidence=_CONFIRMED),
    "apifootball_player_seasons": Endpoint("/players/seasons", (), confidence=_CONFIRMED),
    # A full OpenAPI parameter reconciliation is still pending for profiles.
    "apifootball_player_profiles": Endpoint("/players/profiles", ("page",), confidence=_PENDING, pagination=True),
    "apifootball_player_squads": Endpoint("/players/squads", ("team", "player"), confidence=_CONFIRMED),
    "apifootball_player_teams": Endpoint("/players/teams", ("player",), ("player",), confidence=_CONFIRMED),
    "apifootball_top_scorers": Endpoint("/players/topscorers", ("league", "season"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_top_assists": Endpoint("/players/topassists", ("league", "season"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_top_yellow_cards": Endpoint("/players/topyellowcards", ("league", "season"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_top_red_cards": Endpoint("/players/topredcards", ("league", "season"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_transfers": Endpoint("/transfers", ("player", "team"), confidence=_CONFIRMED),
    "apifootball_trophies": Endpoint("/trophies", ("player", "coach"), confidence=_CONFIRMED),
    "apifootball_sidelined": Endpoint("/sidelined", ("player", "coach"), confidence=_CONFIRMED),
    # The documentation identifies /odds/mapping but further filter
    # parameter semantics are not yet verified. Publish zero-argument
    # read-only diagnostic discovery only; do not invent page support.
    "apifootball_odds_mapping": Endpoint("/odds/mapping", (), confidence=_PENDING),
    "apifootball_live_bets": Endpoint("/odds/live/bets", ("id", "search"), confidence=_CONFIRMED),
}
assert all(documented_path(endpoint.path) for endpoint in ENDPOINTS.values())


def _string(value: str, field: str) -> str:
    if not isinstance(value, str) or not _VALID_VALUE.fullmatch(value):
        raise ParameterError("INVALID_TEXT_PARAMETER")
    if field == "live" and value != "all":
        raise ParameterError("INVALID_LIVE_FILTER")
    if field == "timezone":
        # Unverified/untrusted timezone IDs may be invalid upstream.
        # For PR-03, return timestamps in UTC only; future TZ conversion
        # can use the provider's /timezone catalog.
        if value != "UTC":
            raise ParameterError("TIMEZONE_NOT_CERTIFIED")
    return value


def _positive_int(value: int, field: str) -> int:
    if type(value) is not int or not (1 <= value <= 2_000_000_000):
        raise ParameterError("INVALID_INTEGER_PARAMETER")
    if field == "season" and not (1990 <= value <= 2100):
        raise ParameterError("INVALID_SEASON")
    if field in ("page", "last", "next") and value > 100:
        raise ParameterError("PAGE_OR_BATCH_TOO_LARGE")
    return value


def validate(name: str, values: Mapping[str, Any]) -> tuple[Endpoint, dict[str, str | int]]:
    endpoint = ENDPOINTS.get(name)
    if endpoint is None:
        raise ParameterError("UNKNOWN_TOOL")
    if not isinstance(values, Mapping):
        raise ParameterError("INVALID_PARAMETER_MAP")
    if any(key not in endpoint.fields for key in values):
        raise ParameterError("PARAMETER_NOT_SUPPORTED")
    provided: dict[str, str | int] = {}
    for key, value in values.items():
        if value is None:
            continue
        if name == "apifootball_live_bets" and key == "id":
            value = _string(value, key)
            if not value.isdecimal() or len(value) > 10:
                raise ParameterError("INVALID_LIVE_BET_ID")
            provided[key] = value
        elif key in _POSITIVE:
            provided[key] = _positive_int(value, key)
        elif key in _BOOL_FIELDS:
            if type(value) is not bool:
                raise ParameterError("INVALID_BOOLEAN_PARAMETER")
            provided[key] = "true" if value else "false"
        elif key == "h2h":
            if not isinstance(value, str) or not _H2H.fullmatch(value):
                raise ParameterError("INVALID_HEAD_TO_HEAD_FILTER")
            provided[key] = value
        elif key in _DATE_FIELDS:
            value = _string(value, key)
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ParameterError("INVALID_DATE") from None
            if len(value) != 10:
                raise ParameterError("INVALID_DATE")
            provided[key] = value
        elif key in _TEXT:
            provided[key] = _string(value, key)
        else:
            raise ParameterError("PARAMETER_TYPE_UNVERIFIED")
    if any(k not in provided for k in endpoint.required):
        raise ParameterError("REQUIRED_PARAMETER_MISSING")
    if name == "apifootball_fixtures" and not (
        "id" in provided or "date" in provided or "live" in provided
        or ("league" in provided and "season" in provided)
        or ("team" in provided and ("last" in provided or "next" in provided))
    ):
        raise ParameterError("FIXTURE_FILTER_REQUIRED")
    if name == "apifootball_teams" and not provided:
        raise ParameterError("TEAM_FILTER_REQUIRED")
    if name == "apifootball_venues" and not provided:
        raise ParameterError("VENUE_FILTER_REQUIRED")
    if name == "apifootball_coaches" and not provided:
        raise ParameterError("COACH_FILTER_REQUIRED")
    if name == "apifootball_player_squads" and not (
        "team" in provided or "player" in provided
    ):
        raise ParameterError("SQUAD_FILTER_REQUIRED")
    if name in ("apifootball_transfers", "apifootball_trophies",
                "apifootball_sidelined") and not (
        "player" in provided or "team" in provided or "coach" in provided
    ):
        raise ParameterError("PERSON_OR_TEAM_FILTER_REQUIRED")
    if name == "apifootball_live_bets" and "search" in provided and (
        len(provided["search"]) < 3
    ):
        raise ParameterError("SEARCH_TOO_SHORT")

    if name == "apifootball_players" and not (
        "id" in provided or ("team" in provided and "season" in provided)
    ):
        raise ParameterError("PLAYER_FILTER_REQUIRED")
    if name == "apifootball_injuries" and not (
        "fixture" in provided or ("league" in provided and "season" in provided)
        or "team" in provided
    ):
        raise ParameterError("INJURY_FILTER_REQUIRED")
    if name == "apifootball_odds" and not (
        "fixture" in provided or ("league" in provided and "season" in provided)
        or "date" in provided
    ):
        raise ParameterError("ODDS_FILTER_REQUIRED")
    if name == "apifootball_live_odds" and not (
        "fixture" in provided or "league" in provided
    ):
        raise ParameterError("LIVE_ODDS_FILTER_REQUIRED")
    if ("league" in provided) != ("season" in provided) and name in (
        "apifootball_fixtures", "apifootball_odds",
    ) and "id" not in provided and "fixture" not in provided and "date" not in provided and "live" not in provided:
        raise ParameterError("LEAGUE_SEASON_PAIR_REQUIRED")
    return endpoint, provided
