"""Staging-only adapter for the public 365Scores web feed observed via Browser Use.

This module is deliberately NOT wired into production settlement. It only
normalizes fields that were actually observed in the public JSON response.
HT/corners remain unsupported until a stable public match-detail payload is
qualified and its use is approved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

BASE_CURRENT = "https://webws.365scores.com/web/games/current/"


def build_current_games_url(competition_id: int, timezone_name: str = "Europe/Rome") -> str:
    params = {
        "appTypeId": 5,
        "langId": 1,
        "timezoneName": timezone_name,
        "userCountryId": 3,
        "competitions": int(competition_id),
        "showOdds": "true",
        "includeTopBettingOpportunity": 1,
        "topBookmaker": 16,
    }
    return f"{BASE_CURRENT}?{urlencode(params)}"


def _utc_iso(value: str | None) -> str | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _score(comp: dict | None) -> int | None:
    if not comp:
        return None
    value = comp.get("score")
    if value is None:
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def normalize_game(game: dict) -> dict:
    home = game.get("homeCompetitor") or {}
    away = game.get("awayCompetitor") or {}
    home_goals = _score(home)
    away_goals = _score(away)
    status_text = str(game.get("statusText") or "")
    ended = game.get("statusGroup") == 4 or status_text.lower() == "ended"

    if ended and home_goals is not None and away_goals is not None:
        if home_goals > away_goals:
            result_1x2 = "1"
        elif home_goals < away_goals:
            result_1x2 = "2"
        else:
            result_1x2 = "X"
        status = "FINISHED"
    else:
        result_1x2 = None
        status = "UNRESOLVED"

    return {
        "provider": "365scores-web",
        "provider_match_id": str(game.get("id")) if game.get("id") is not None else None,
        "competition_id": game.get("competitionId"),
        "competition": game.get("competitionDisplayName"),
        "kickoff_utc": _utc_iso(game.get("startTime")),
        "home_team": home.get("name"),
        "away_team": away.get("name"),
        "status": status,
        "final_home_goals": home_goals if status == "FINISHED" else None,
        "final_away_goals": away_goals if status == "FINISHED" else None,
        "result_1x2": result_1x2,
        "ht_home_goals": None,
        "ht_away_goals": None,
        "corners_home": None,
        "corners_away": None,
        "ht_corners_home": None,
        "ht_corners_away": None,
        "capability_note": "FT from current feed verified; HT/corners require qualified detail adapter",
    }


def normalize_current_payload(payload: dict) -> list[dict]:
    return [normalize_game(game) for game in payload.get("games", []) if isinstance(game, dict)]
