"""Every query parameter of the official API-FOOTBALL v3.9.3 documentation is
exposed by the matching contract, with the documented required set.

Source: docs/api_football/official_parameter_matrix_v3.9.3.json, extracted
from the rendered official documentation page (see its `retrieval` field).
"""
import json
from pathlib import Path

from bridge.api_football.contracts import ENDPOINTS, ParameterError, validate

MATRIX = json.loads((Path(__file__).resolve().parents[1]
                     / "docs/api_football/official_parameter_matrix_v3.9.3.json").read_text(encoding="utf-8"))
# Tools that intentionally narrow a shared path (documented in contracts.py).
NARROW = {"apifootball_coverage_report"}


def _primary(path):
    tools = [n for n, e in ENDPOINTS.items() if e.path == path and n not in NARROW]
    assert len(tools) == 1, (path, tools)
    return tools[0], ENDPOINTS[tools[0]]


def test_matrix_covers_the_38_official_paths():
    assert MATRIX["documentation_version"] == "3.9.3" and MATRIX["endpoint_count"] == 38
    paths = {e["path"] for e in MATRIX["endpoints"]}
    assert len(paths) == 38
    assert paths == {e.path for e in ENDPOINTS.values()} - {"/status"}


def test_every_documented_parameter_is_exposed_and_none_invented():
    gaps = {}
    for ep in MATRIX["endpoints"]:
        name, contract = _primary(ep["path"])
        documented = {p["name"] for p in ep["query_parameters"]}
        if documented != set(contract.fields):
            gaps[name] = {"missing": sorted(documented - set(contract.fields)),
                          "not_documented": sorted(set(contract.fields) - documented)}
    assert not gaps, gaps


def test_required_and_pagination_follow_the_documentation():
    for ep in MATRIX["endpoints"]:
        name, contract = _primary(ep["path"])
        documented_required = {p["name"] for p in ep["query_parameters"] if p["required"]}
        assert set(contract.required) == documented_required, (name, contract.required, documented_required)
        assert contract.pagination == (ep["pagination_results_per_page"] is not None
                                       or "page" in {p["name"] for p in ep["query_parameters"]}), name


def test_documented_contracts_are_no_longer_pending():
    pending = [n for n, e in ENDPOINTS.items() if e.confidence != "DOCUMENTED_OFFICIAL"]
    assert pending == []


def _rejects(name, values, code):
    try:
        validate(name, values)
    except ParameterError as exc:
        assert str(exc) == code, (name, values, str(exc))
    else:
        raise AssertionError((name, values, "accepted"))


def test_documented_constraints_and_combinations():
    # /players: search >= 4 chars and requires league or team; season requires id, league or team
    validate("apifootball_players", {"search": "Mess", "league": 39})
    _rejects("apifootball_players", {"search": "Mes", "league": 39}, "SEARCH_TOO_SHORT")
    _rejects("apifootball_players", {"search": "Messi"}, "PLAYER_FILTER_REQUIRED")
    validate("apifootball_players", {"league": 39, "season": 2026})
    # /players/profiles: search >= 4 chars; no filter is valid (pagination)
    validate("apifootball_player_profiles", {})
    validate("apifootball_player_profiles", {"search": "Ronaldo", "page": 2})
    _rejects("apifootball_player_profiles", {"search": "Ron"}, "SEARCH_TOO_SHORT")
    # /injuries: season required with league, team and player
    validate("apifootball_injuries", {"player": 276, "season": 2026})
    validate("apifootball_injuries", {"date": "2026-11-01"})
    _rejects("apifootball_injuries", {"team": 33}, "INJURY_FILTER_REQUIRED")
    _rejects("apifootball_injuries", {"player": 276}, "INJURY_FILTER_REQUIRED")
    # /leagues enums and bounds
    validate("apifootball_leagues", {"type": "cup", "current": True, "code": "GB-ENG", "last": 10})
    _rejects("apifootball_leagues", {"type": "friendly"}, "INVALID_LEAGUE_TYPE")
    _rejects("apifootball_leagues", {"last": 100}, "PAGE_OR_BATCH_TOO_LARGE")
    _rejects("apifootball_leagues", {"search": "ab"}, "SEARCH_TOO_SHORT")
    # /standings: only season is required by the docs, but league or team must bound it
    validate("apifootball_standings", {"team": 33, "season": 2026})
    _rejects("apifootball_standings", {"season": 2026}, "STANDINGS_FILTER_REQUIRED")
    # /trophies, /sidelined: up to 20 ids
    validate("apifootball_trophies", {"players": "276-277"})
    validate("apifootball_sidelined", {"coachs": "1-2-3"})
    _rejects("apifootball_trophies", {"players": "-".join(["1"] * 21)}, "INVALID_ID_LIST")
    # events/lineups filters
    validate("apifootball_fixture_events", {"fixture": 1, "team": 33, "player": 276, "type": "Goal"})
    validate("apifootball_fixture_headtohead", {"h2h": "33-34", "league": 39, "season": 2026, "from": "2026-01-01", "to": "2026-06-30"})
