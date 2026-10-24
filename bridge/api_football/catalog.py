"""DOCUMENTARY-ONLY API-FOOTBALL path inventory (MatchPilot #248).

No parameters or response fields are certified in PR-01.  Do not infer
access rights, coverage, real availability, or response types from this list.
"""
from __future__ import annotations

DOCUMENTATION_URL = "https://www.api-football.com/documentation_v3"
EVIDENCE = "PENDING_OFFICIAL_RECONCILIATION"
FOOTBALL_PATHS: tuple[str, ...] = (
    "/timezone", "/countries", "/leagues", "/leagues/seasons",
    "/teams", "/teams/statistics", "/teams/seasons", "/teams/countries",
    "/venues", "/standings", "/fixtures/rounds", "/fixtures",
    "/fixtures/headtohead", "/fixtures/statistics", "/fixtures/events",
    "/fixtures/lineups", "/fixtures/players", "/injuries", "/coachs",
    "/players/seasons", "/players/profiles", "/players",
    "/players/squads", "/players/teams", "/players/topscorers",
    "/players/topassists", "/players/topyellowcards", "/players/topredcards",
    "/transfers", "/trophies", "/sidelined", "/predictions", "/odds",
    "/odds/mapping", "/odds/bookmakers", "/odds/bets", "/odds/live",
    "/odds/live/bets",
)
ACCOUNT_PATHS: tuple[str, ...] = ("/status",)
ALLOWED_PATHS = frozenset(FOOTBALL_PATHS + ACCOUNT_PATHS)


def documented_path(path: str) -> bool:
    return isinstance(path, str) and path in ALLOWED_PATHS
