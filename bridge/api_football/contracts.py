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
    "bet", "page", "last", "next", "venue", "coach",
})
_DATE_FIELDS = frozenset({"date", "from", "to"})
_TEXT = frozenset({"name", "country", "code", "search", "status", "type", "timezone", "live", "city"})
_ID_LISTS = frozenset({"ids", "players", "coachs"})  # documented "id-id-id", maximum of 20 ids
# Documented minimum search lengths (official docs v3.9.3).
_MIN_SEARCH = {"apifootball_leagues": 3, "apifootball_teams": 3, "apifootball_players": 4,
               "apifootball_player_profiles": 4, "apifootball_live_bets": 3}
_BOOL_FIELDS = frozenset({"current", "dates", "half"})
_IDS = re.compile(r"^[1-9][0-9]{0,9}(?:-[1-9][0-9]{0,9}){0,19}$")
assert not (_POSITIVE & _BOOL_FIELDS), "Boolean fields must not be validated as integers"
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
    "apifootball_leagues": Endpoint("/leagues", ("id", "name", "country", "code", "season", "team", "type", "current", "search", "last"), confidence=_CONFIRMED),
    "apifootball_seasons": Endpoint("/leagues/seasons", (), confidence=_CONFIRMED),
    "apifootball_fixtures": Endpoint("/fixtures", ("id", "ids", "date", "league", "season", "team", "live", "timezone", "last", "next", "status", "venue", "round", "from", "to"), confidence=_CONFIRMED),
    "apifootball_fixture_statistics": Endpoint("/fixtures/statistics", ("fixture", "team", "type", "half"), ("fixture",), confidence=_CONFIRMED),
    "apifootball_fixture_events": Endpoint("/fixtures/events", ("fixture", "team", "player", "type"), ("fixture",), confidence=_CONFIRMED),
    "apifootball_fixture_lineups": Endpoint("/fixtures/lineups", ("fixture", "team", "player", "type"), ("fixture",), confidence=_CONFIRMED),
    "apifootball_teams": Endpoint("/teams", ("id", "name", "league", "season", "country", "search", "code", "venue"), confidence=_CONFIRMED),
    "apifootball_team_statistics": Endpoint("/teams/statistics", ("league", "season", "team", "date"), ("league", "season", "team"), confidence=_CONFIRMED),
    "apifootball_players": Endpoint("/players", ("id", "team", "league", "season", "search", "page"), confidence=_CONFIRMED, pagination=True),
    "apifootball_injuries": Endpoint("/injuries", ("league", "season", "fixture", "team", "player", "date", "ids", "timezone"), confidence=_CONFIRMED),
    "apifootball_predictions": Endpoint("/predictions", ("fixture",), ("fixture",), confidence=_CONFIRMED),
    "apifootball_odds": Endpoint("/odds", ("fixture", "league", "season", "date", "bookmaker", "bet", "page", "timezone"), confidence=_CONFIRMED, pagination=True),
    "apifootball_live_odds": Endpoint("/odds/live", ("fixture", "league", "bet"), confidence=_CONFIRMED),
    "apifootball_bookmakers": Endpoint("/odds/bookmakers", ("id", "search"), confidence=_CONFIRMED),
    "apifootball_bets": Endpoint("/odds/bets", ("id", "search"), confidence=_CONFIRMED),
    "apifootball_coverage_report": Endpoint("/leagues", ("id", "season"), ("id", "season"), confidence=_CONFIRMED),
    # All 22 remaining football endpoints. Confidence labels classify
    # provider-documentary evidence only, NEVER empirical availability.
    "apifootball_timezones": Endpoint("/timezone", (), confidence=_CONFIRMED),
    "apifootball_team_seasons": Endpoint("/teams/seasons", ("team",), ("team",), confidence=_CONFIRMED),
    "apifootball_team_countries": Endpoint("/teams/countries", (), confidence=_CONFIRMED),
    "apifootball_venues": Endpoint("/venues", ("id", "name", "city", "country", "search"), confidence=_CONFIRMED),
    "apifootball_standings": Endpoint("/standings", ("league", "season", "team"), ("season",), confidence=_CONFIRMED),
    "apifootball_fixture_rounds": Endpoint("/fixtures/rounds", ("league", "season", "current", "dates", "timezone"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_fixture_headtohead": Endpoint("/fixtures/headtohead", ("h2h", "date", "league", "season", "last", "next", "from", "to", "status", "venue", "timezone"), ("h2h",), confidence=_CONFIRMED),
    "apifootball_fixture_players": Endpoint("/fixtures/players", ("fixture", "team"), ("fixture",), confidence=_CONFIRMED),
    "apifootball_coaches": Endpoint("/coachs", ("id", "team", "search"), confidence=_CONFIRMED),
    "apifootball_player_seasons": Endpoint("/players/seasons", ("player",), confidence=_CONFIRMED),
    "apifootball_player_profiles": Endpoint("/players/profiles", ("player", "search", "page"), confidence=_CONFIRMED, pagination=True),
    "apifootball_player_squads": Endpoint("/players/squads", ("team", "player"), confidence=_CONFIRMED),
    "apifootball_player_teams": Endpoint("/players/teams", ("player",), ("player",), confidence=_CONFIRMED),
    "apifootball_top_scorers": Endpoint("/players/topscorers", ("league", "season"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_top_assists": Endpoint("/players/topassists", ("league", "season"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_top_yellow_cards": Endpoint("/players/topyellowcards", ("league", "season"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_top_red_cards": Endpoint("/players/topredcards", ("league", "season"), ("league", "season"), confidence=_CONFIRMED),
    "apifootball_transfers": Endpoint("/transfers", ("player", "team"), confidence=_CONFIRMED),
    "apifootball_trophies": Endpoint("/trophies", ("player", "players", "coach", "coachs"), confidence=_CONFIRMED),
    "apifootball_sidelined": Endpoint("/sidelined", ("player", "players", "coach", "coachs"), confidence=_CONFIRMED),
    # /odds/mapping: the official docs (v3.9.3) document `page` only, 100 rows/page.
    "apifootball_odds_mapping": Endpoint("/odds/mapping", ("page",), confidence=_CONFIRMED, pagination=True),
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


def _id_list(value: str) -> str:
    if not isinstance(value, str) or not _IDS.fullmatch(value):
        raise ParameterError("INVALID_ID_LIST")
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
        elif key == "ids":
            if not isinstance(value, str) or not _IDS.fullmatch(value):
                raise ParameterError("INVALID_FIXTURE_IDS")
            provided[key] = value
        elif key in _ID_LISTS:
            provided[key] = _id_list(value)
        elif key == "round":
            provided[key] = _string(value, key)
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
    if "search" in provided and len(str(provided["search"])) < _MIN_SEARCH.get(name, 1):
        raise ParameterError("SEARCH_TOO_SHORT")
    if name == "apifootball_leagues":
        if "type" in provided and provided["type"] not in ("league", "cup"):
            raise ParameterError("INVALID_LEAGUE_TYPE")
        if "last" in provided and provided["last"] > 99:  # documented: <= 2 characters
            raise ParameterError("PAGE_OR_BATCH_TOO_LARGE")
        if "code" in provided and not 2 <= len(str(provided["code"])) <= 6:
            raise ParameterError("INVALID_COUNTRY_CODE")
    if name == "apifootball_standings" and not ("league" in provided or "team" in provided):
        raise ParameterError("STANDINGS_FILTER_REQUIRED")
    if name == "apifootball_fixtures" and not (
        "id" in provided or "ids" in provided or "date" in provided or "live" in provided
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
        or "players" in provided or "coachs" in provided
    ):
        raise ParameterError("PERSON_OR_TEAM_FILTER_REQUIRED")

    # /players (docs): season requires id, league or team; search requires league or team.
    if name == "apifootball_players" and not (
        "id" in provided
        or (("team" in provided or "league" in provided) and ("season" in provided or "search" in provided))
    ):
        raise ParameterError("PLAYER_FILTER_REQUIRED")
    # /injuries (docs): season is required with league, team and player.
    if name == "apifootball_injuries" and not (
        "fixture" in provided or "ids" in provided or "date" in provided
        or (("league" in provided or "team" in provided or "player" in provided) and "season" in provided)
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
