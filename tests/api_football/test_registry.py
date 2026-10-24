import copy

import pytest

from bridge.api_football import registry

ISSUE_248_PATHS = {
    "/timezone", "/countries", "/leagues", "/leagues/seasons", "/teams", "/teams/statistics",
    "/teams/seasons", "/teams/countries", "/venues", "/standings",
    "/fixtures/rounds", "/fixtures", "/fixtures/headtohead", "/fixtures/statistics",
    "/fixtures/events", "/fixtures/lineups", "/fixtures/players",
    "/injuries", "/coachs", "/players/seasons", "/players/profiles", "/players", "/players/squads",
    "/players/teams", "/players/topscorers", "/players/topassists", "/players/topyellowcards",
    "/players/topredcards", "/transfers", "/trophies", "/sidelined",
    "/predictions", "/odds", "/odds/mapping", "/odds/bookmakers", "/odds/bets", "/odds/live",
    "/odds/live/bets",
    "/status",
}

# Owner mandate FCB-AF-V3-OFFICIAL-CONTRACTS-01 section 3 (names only).
OWNER_BASELINE = {
    "/fixtures": ["id", "ids", "live", "date", "league", "season", "team", "last", "next", "from", "to",
                  "round", "status", "venue", "timezone"],
    "/fixtures/statistics": ["fixture", "team", "type", "half"],
    "/predictions": ["fixture"],
    "/odds": ["fixture", "league", "season", "date", "timezone", "page", "bookmaker", "bet"],
    "/teams/statistics": ["league", "season", "team", "date"],
    "/odds/live": ["fixture", "league", "bet"],
}


@pytest.fixture
def raw():
    return copy.deepcopy(registry.load_raw())


def test_registry_is_consistent(raw):
    assert registry.check(raw) == []


def test_inventory_matches_issue_248_and_ids_are_stable(raw):
    endpoints = raw["endpoints"]["endpoints"]
    assert {e["path"] for e in endpoints} == ISSUE_248_PATHS
    assert [e["id"] for e in endpoints] == [f"AF-EP-{n:03d}" for n in range(1, 40)]
    assert all(e["method"] == "GET" for e in endpoints)


def test_only_owner_baseline_endpoints_have_parameters_and_tools(raw):
    params = raw["parameters"]["endpoints"]
    for endpoint in raw["endpoints"]["endpoints"]:
        entry = params[endpoint["id"]]
        if endpoint["path"] in OWNER_BASELINE:
            assert entry["status"] == "OWNER_VERIFIED_NAMES"
            assert [p["wire"] for p in entry["params"]] == OWNER_BASELINE[endpoint["path"]]
            assert endpoint["mcp_tool"] and endpoint["implementation_status"] == "IMPLEMENTED_PROVISIONAL"
        else:
            # No parameter reconstructed from memory: pending endpoints carry none.
            assert entry == {"path": endpoint["path"], "status": registry.PENDING, "source": None, "params": None}
            assert endpoint["mcp_tool"] is None
            assert endpoint["implementation_status"] == "NOT_IMPLEMENTED_CONTRACT_PENDING"


def test_nothing_is_claimed_as_officially_or_really_verified(raw):
    for endpoint in raw["endpoints"]["endpoints"]:
        assert endpoint["contract_status"] != "OFFICIAL_VERIFIED"
        assert endpoint["certification_status"] == "NOT_CERTIFIED"
        assert endpoint["real_test_status"] == "NOT_AUTHORIZED"
        assert endpoint["response_schema"] == registry.PENDING
        assert endpoint["documentation_version"] == registry.PENDING
    for entry in raw["parameters"]["endpoints"].values():
        for param in entry["params"] or []:
            assert set(param["official"].values()) == {registry.PENDING}
            assert param["client_validation"]["status"] == "PROVISIONAL_CLIENT_POLICY"
    assert all(s["status"] == registry.PENDING for s in raw["response_schemas"]["endpoints"].values())


def test_sources_record_unavailable_documentation_without_bypass(raw):
    sources = {s["id"]: s for s in raw["sources"]["sources"]}
    official = sources["SRC-OFFICIAL-DOC"]
    assert official["status"] == "DOCUMENTATION_UNAVAILABLE"
    assert official["bypass_attempted"] is False
    assert {a["http_status"] for a in official["acquisition_attempts"] if a["http_status"]} == {403}
    assert all(a["status"] == registry.PENDING for a in raw["sources"]["client_assumptions"])


@pytest.mark.parametrize("mutate,problem", [
    (lambda r: r["endpoints"]["endpoints"][0].update(mcp_tool="apifootball_timezone",
                                                     implementation_status="IMPLEMENTED_PROVISIONAL"),
     "tool without a verified parameter contract"),
    (lambda r: r["endpoints"]["endpoints"][1].update(path="/timezone"), "duplicate path"),
    (lambda r: r["endpoints"]["endpoints"][11].update(implementation_status="IMPLEMENTED"),
     "IMPLEMENTED requires an OFFICIAL_VERIFIED contract"),
    (lambda r: r["endpoints"]["endpoints"][11].update(real_test_status="REAL_VERIFIED"),
     "REAL_VERIFIED without last_verified_utc"),
    (lambda r: r["parameters"]["endpoints"]["AF-EP-012"]["params"][0]["client_validation"].update(kind="any"),
     "unknown validation kind"),
    (lambda r: r["parameters"]["endpoints"]["AF-EP-012"]["params"][1].update(wire="id"), "duplicate wire"),
    (lambda r: r["parameters"]["endpoints"]["AF-EP-012"].update(source="SRC-MEMORY"),
     "parameter source is not a registered source"),
    (lambda r: r["endpoints"]["endpoints"][11].update(mcp_tool="apifootball_matches"), "tool name must be"),
    (lambda r: r["endpoints"]["endpoints"][5].update(method="POST"), "unsupported method"),
    (lambda r: r["parameters"]["endpoints"].pop("AF-EP-020"), "parameters.json entry missing"),
])
def test_check_detects_contract_violations(raw, mutate, problem):
    mutate(raw)
    assert any(problem in item for item in registry.check(raw)), registry.check(raw)


def test_implemented_endpoints_are_built_from_registry():
    endpoints = registry.implemented()
    assert {e.path for e in endpoints} == set(OWNER_BASELINE)
    fixtures = next(e for e in endpoints if e.path == "/fixtures")
    assert {p.name: p.wire for p in fixtures.params}["from_date"] == "from"
    assert next(e for e in endpoints if e.path == "/odds").data_class == "MARKET_ODDS"
    assert "not a model probability" in next(e for e in endpoints if e.path == "/odds/live").warnings[0]
    assert next(e for e in endpoints if e.path == "/predictions").data_class == "PROVIDER_PREDICTION"


def test_registry_load_failure_is_explicit(tmp_path):
    with pytest.raises(registry.RegistryError):
        registry.load_raw(tmp_path)


def test_coverage_uses_explicit_denominators():
    dashboard = registry.coverage()["dashboard"]
    assert dashboard["endpoints_implemented_as_tools"] == {"numerator": 6, "denominator": 39}
    assert dashboard["official_contracts_verified"] == {"numerator": 0, "denominator": 39}
    assert dashboard["parameters_covered"]["numerator"] == dashboard["parameters_covered"]["denominator"] == 35
    assert "UNKNOWN" in dashboard["parameters_covered"]["denominator_note"]
    assert dashboard["real_tests_passed"]["numerator"] == 0


def test_committed_coverage_outputs_are_current():
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_api_football_contracts.py"
    spec = importlib.util.spec_from_file_location("af_contract_script", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for path, text in module.coverage_outputs().items():
        assert path.read_text(encoding="utf-8") == text, f"{path.name} stale: run report --write"
    assert module.tool_problems(registry.load_raw()) == []
    assert module.snapshot_problems() == []
