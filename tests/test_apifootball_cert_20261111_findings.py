"""Regressions from the real API-FOOTBALL certification of 2026-11-11 (MCP -> Render, 848c007).

F-A04  fixtures(venue=556, season=2026): the real provider refused the set with declared errors on
       `season` and `venue` and the bridge spent a call on a DOCS_UNVERIFIED label. The exact
       observed set is now PROVIDER_REJECTED_OBSERVED: refused locally, 0 provider calls. Nothing
       else is refused because of it.
F-WARN provider errors, scan-bound errors and partial manifests carried "Provider calls require
       separate owner authorization and key." although the call was authorized and made.
F-A10  player_profiles(page=2771) with a real total of 2770: results 0, paging {2771, 2771} (the
       provider echoes the requested page as total) was presented as LAST_PAGE.

No network: the provider is httpx.MockTransport (or a fake client) behind the real bridge code.
Fixtures: tests/fixtures/apifootball_cert_20261111 (built from the verbatim MCP outputs; each
file says what was trimmed or reconstructed).
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.contracts import (
    PROVIDER_REJECTED_OBSERVED, CombinationRejected, ParameterError, fixtures_combination, validate,
)
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import pagination_state, register_tools

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apifootball_cert_20261111"
KEY = "LOCAL_FAKE_KEY_CERT_20261111_ONLY"
AUTH_WARNING = "Provider calls require separate owner authorization and key."
DATA_WARNING = ("Provider data returned as received; coverage, availability and update timing vary by league "
                "and fixture.")


def envelope(name):
    return json.loads((FIXTURES / name).read_text())["envelope"]


class Quota:
    async def acquire(self, secret):
        return QuotaLease("l", "h")

    async def release(self, lease):
        return None

    async def mark_exhausted(self, secret, kind):
        return None


class Server:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def real_tools(body, seen):
    """The real resilience client and transport; the provider always answers `body`."""
    from bridge.api_football.transport import InjectedHTTPXTransport

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=body, headers={"x-ratelimit-requests-remaining": "7497",
                                                       "x-ratelimit-remaining": "297"})
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True, cache_enabled=False)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler))
    client = ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=KEY), quota=Quota(),
                                     retry=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0))
    server = Server()
    register_tools(server, settings, client=client)
    return server.tools


def run(tools, name, **args):
    return asyncio.run(tools[name](**args))


# ---- F-A04: a combination the real provider was observed to refuse ---------------------------

def test_a04_observed_rejection_is_refused_locally_with_zero_calls():
    seen = []
    tools = real_tools(envelope("a04_fixtures_venue556_season2026_declared_errors.json"), seen)
    out = run(tools, "apifootball_fixtures", venue=556, season=2026)
    assert seen == []                                  # no provider request, no quota spent
    assert out["status"] == "INVALID_INPUT" and out["network_calls_made"] == 0
    assert out["error"] == {"code": "FIXTURE_COMBINATION_PROVIDER_REJECTED"}
    assert out["combination"] == {
        "status": "PROVIDER_REJECTED_OBSERVED", "rule": "F-REJECTED-season+venue",
        "observed_on_utc": "2026-11-11", "evidence": "REAL_CERTIFICATION_2026-11-11_A04",
        "provider_error_keys": ["season", "venue"], "provider_error_category": "INVALID_PARAMETER"}
    assert "556" not in json.dumps(out)                # fixed evidence only, never the request values
    assert out["data"] is None and out["retrieved_at_utc"] is None


def test_a04_the_contract_records_the_observation_not_a_documentary_rule():
    with pytest.raises(CombinationRejected) as info:
        validate("apifootball_fixtures", {"venue": 556, "season": 2026})
    assert isinstance(info.value, ParameterError) and str(info.value) == "FIXTURE_COMBINATION_PROVIDER_REJECTED"
    assert info.value.combination["status"] == PROVIDER_REJECTED_OBSERVED
    # The key set is what the provider refused (declared errors on both keys), whatever the ids.
    with pytest.raises(CombinationRejected):
        fixtures_combination({"venue": 1, "season": 2025})


@pytest.mark.parametrize("args", [
    {"venue": 556, "season": 2026, "timezone": "Europe/Rome"},   # not the observed set: + timezone
    {"venue": 556, "season": 2026, "league": 39},
    {"venue": 556, "season": 2026, "status": "FT"},
    {"venue": 556, "next": 3}, {"venue": 556, "last": 3},
    {"venue": 556, "from": "2026-11-01", "to": "2026-11-30"},
    {"team": 33, "season": 2026},                               # A03: a real PASS, stays forwarded
])
def test_a04_is_not_generalised_to_other_sets(args):
    _, params = validate("apifootball_fixtures", args)
    out = fixtures_combination(params)
    assert out["status"] == "DOCS_UNVERIFIED" and out["rule"].startswith("F-COMBINATION-")


@pytest.mark.parametrize("args,code", [({"venue": 556}, "FIXTURE_FILTER_REQUIRED"),
                                       ({"season": 2026}, "FIXTURE_FILTER_REQUIRED"),
                                       ({"venue": 556, "season": 1989}, "INVALID_SEASON"),
                                       ({"venue": 0, "season": 2026}, "INVALID_INTEGER_PARAMETER")])
def test_a04_earlier_validation_codes_are_unchanged(args, code):
    with pytest.raises(ParameterError) as info:
        validate("apifootball_fixtures", args)
    assert str(info.value) == code and not isinstance(info.value, CombinationRejected)


def test_a04_a_neighbour_set_is_still_forwarded_and_the_provider_stays_the_authority():
    seen = []
    tools = real_tools(envelope("a04_fixtures_venue556_season2026_declared_errors.json"), seen)
    out = run(tools, "apifootball_fixtures", venue=556, season=2026, timezone="Europe/Rome")
    assert len(seen) == 1 and out["network_calls_made"] == 1
    assert out["status"] == "PROVIDER_ERROR"
    assert out["error"] == {"code": "PROVIDER_DECLARED_ERRORS", "keys": ["season", "venue"],
                            "category": "INVALID_PARAMETER"}
    assert "PLACEHOLDER_NOT_PROVIDER_TEXT" not in json.dumps(out)
    assert out["combination"]["status"] == "DOCS_UNVERIFIED"


# ---- F-WARN: every reply carries an accurate warning ------------------------------------------

def test_warn_declared_errors_reply_does_not_claim_a_missing_authorization():
    seen = []
    tools = real_tools(envelope("a04_fixtures_venue556_season2026_declared_errors.json"), seen)
    out = run(tools, "apifootball_fixtures", venue=556, next=3)
    assert out["status"] == "PROVIDER_ERROR" and out["network_calls_made"] == 1
    assert AUTH_WARNING not in out["warnings"] and out["warnings"]
    assert all("No provider data returned" in w for w in out["warnings"])
    assert out["quota_remaining"] == {"day": 7497, "minute": 297}


@pytest.mark.parametrize("status", [401, 429, 500])
def test_warn_http_errors_carry_the_error_warning(status):
    from bridge.api_football.transport import InjectedHTTPXTransport
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True, cache_enabled=False)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL,
                             transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    client = ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=KEY), quota=Quota(),
                                     retry=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0))
    server = Server()
    register_tools(server, settings, client=client)
    out = run(server.tools, "apifootball_countries")
    assert out["status"] == "PROVIDER_ERROR" and AUTH_WARNING not in out["warnings"]
    assert out["warnings"] and all("No provider data returned" in w for w in out["warnings"])


def test_warn_a_local_refusal_with_zero_calls_is_not_called_unauthorized():
    from bridge.api_football.quota import QuotaExceeded

    class Refusing:
        async def get(self, path, params=None):
            raise QuotaExceeded(window="minute", retry_after_seconds=5)
    server = Server()
    register_tools(server, Settings(enabled=True, api_key=KEY, live_authorized=True), client=Refusing())
    out = run(server.tools, "apifootball_countries")
    assert out["status"] == "PROVIDER_ERROR" and out["network_calls_made"] == 0
    assert out["error"]["code"] == "LOCAL_RATE_LIMITED"
    assert AUTH_WARNING not in out["warnings"] and out["warnings"]


def test_warn_scan_bound_error_carries_an_accurate_warning():
    from apifootball_continuation_kit import Provider, boot
    from bridge.api_football.stream_scan import ScanLimits
    leagues = [{"league": {"id": i, "name": "L" * 400}, "seasons": []} for i in range(1, 1500)]
    out = asyncio.run(boot(Provider(leagues=leagues), limits=ScanLimits(max_rows=100))["apifootball_leagues"]())
    assert out["status"] == "PROVIDER_ERROR"
    assert out["error"] == {"code": "PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN", "bound": "MAX_ROWS"}
    assert AUTH_WARNING not in out["warnings"]
    assert len(out["warnings"]) == 1 and "scan stopped at a limit" in out["warnings"][0]


def test_warn_scanned_partial_id_manifest_carries_the_partial_warning():
    from apifootball_continuation_kit import Provider, boot
    leagues = [{"league": {"id": i, "name": "L" * 400}, "seasons": []} for i in range(1, 1500)]
    out = asyncio.run(boot(Provider(leagues=leagues))["apifootball_leagues"]())
    assert out["status"] == "PARTIAL_ID_MANIFEST"
    assert AUTH_WARNING not in out["warnings"] and DATA_WARNING not in out["warnings"]
    assert len(out["warnings"]) == 1 and out["warnings"][0].startswith("Partial reply")


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return {"data": self.payload, "http_status": 200, "cache_status": "MISS",
                "quota_remaining_provider_day": 7494, "quota_remaining_provider_minute": 297}


def oversized_b06f():
    """B06f (fixture 1557464) with its first real bookmaker repeated under synthetic ids until the
    page is above the 256 KiB MCP cap: the provider shape is real, the repetition is not."""
    env = envelope("b06f_odds_fixture1557464_two_bookmakers.json")
    row = env["response"][0]
    template = row["bookmakers"][0]
    for i in range(40):
        book = copy.deepcopy(template)
        book["id"] = 9000 + i
        row["bookmakers"].append(book)
    assert len(json.dumps(env).encode()) > 262_144
    return env


def test_warn_partial_odds_manifest_carries_the_partial_warning():
    server = Server()
    register_tools(server, Settings(enabled=True, api_key=KEY, live_authorized=True),
                   client=FakeClient(oversized_b06f()))
    out = run(server.tools, "apifootball_odds", fixture=1557464)
    assert out["status"] == "PARTIAL_ODDS_MANIFEST" and out["network_calls_made"] == 1
    assert AUTH_WARNING not in out["warnings"] and DATA_WARNING not in out["warnings"]
    assert len(out["warnings"]) == 1 and "no prices" in out["warnings"][0]
    assert '"odd"' not in json.dumps(out["data"])


def test_warn_successful_real_reply_keeps_the_provider_data_warning():
    seen = []
    out = run(real_tools(envelope("a03_fixtures_team33_season2026.json"), seen),
              "apifootball_fixtures", team=33, season=2026)
    assert out["status"] == "REAL_API_RESPONSE" and out["warnings"] == [DATA_WARNING]


@pytest.mark.parametrize("settings,status", [
    (Settings(enabled=True, api_key=""), "KEY_NOT_CONFIGURED"),
    (Settings(enabled=True, api_key=KEY, live_authorized=False), "LIVE_NOT_AUTHORIZED"),
    (Settings(enabled=True, api_key=KEY, live_authorized=True), "PROVIDER_CLIENT_UNAVAILABLE"),
])
def test_warn_gated_replies_keep_the_authorization_warning(settings, status):
    server = Server()
    register_tools(server, settings)
    out = run(server.tools, "apifootball_countries")
    assert out["status"] == status and out["network_calls_made"] == 0
    assert AUTH_WARNING in out["warnings"]


# ---- F-A10: an empty page after page 1 is not a normal last page ------------------------------

def test_a10_empty_page_echoing_the_requested_page_as_total_is_flagged():
    seen = []
    body = envelope("a10_player_profiles_page2771_empty.json")
    out = run(real_tools(body, seen), "apifootball_player_profiles", page=2771)
    assert seen == [("/players/profiles", {"page": "2771"})]
    assert out["status"] == "REAL_API_RESPONSE"
    # Provider values stay raw and untouched.
    assert out["pagination"] == {"current": 2771, "total": 2771}
    assert out["data"]["paging"] == {"current": 2771, "total": 2771} and out["data"]["results"] == 0
    st = out["pagination_state"]
    assert st["state"] == "EMPTY_PAGE" and st["state"] != "LAST_PAGE"
    assert st["possible_out_of_range"] is True
    assert st["has_next"] is None and st["next_page"] is None and st["next_parameters"] is None
    assert st["requested_page"] == 2771 and st["current"] == 2771 and st["total"] == 2771
    assert "echo" in st["note"] and st["auto_fetch"] is False


def test_a10_a09_real_page_is_unchanged():
    seen = []
    out = run(real_tools(envelope("a09_player_profiles_page101.json"), seen), "apifootball_player_profiles", page=101)
    st = out["pagination_state"]
    assert st["state"] == "MORE_PAGES" and st["next_page"] == 102 and st["next_parameters"] == {"page": 102}
    assert "possible_out_of_range" not in st


@pytest.mark.parametrize("paging,params,rows,state", [
    ({"current": 2771, "total": 2771}, {"page": 2771}, 1, "LAST_PAGE"),     # rows present: a real last page
    ({"current": 1, "total": 1}, {}, 0, "LAST_PAGE"),                     # page 1 (default) empty: unchanged
    ({"current": 1, "total": 1}, {"page": 1}, 0, "LAST_PAGE"),
    ({"current": 2771, "total": 2771}, {"page": 2771}, None, "LAST_PAGE"),  # rows unknown: unchanged
    ({"current": 2772, "total": 2770}, {"page": 2772}, 0, "PAGE_OUT_OF_RANGE"),
    ({"current": 1, "total": 0}, {"page": 1}, 0, "NO_PAGES"),
    ({"current": 5, "total": 32}, {"page": 5}, 0, "EMPTY_PAGE"),          # empty inside the range too
    ({"current": 101, "total": 2770}, {"page": 101}, 250, "MORE_PAGES"),
])
def test_a10_pagination_state_edges(paging, params, rows, state):
    raw = dict(paging)
    st = pagination_state(paging, params, rows=rows)
    assert st["state"] == state and paging == raw
    assert ("possible_out_of_range" in st) == (state == "EMPTY_PAGE")


def test_a10_mock_path_applies_the_same_rule():
    from bridge.api_football.client import MockFootballClient
    mock = MockFootballClient({"/odds/mapping": {"get": "odds/mapping", "errors": [], "results": 0,
                                                 "paging": {"current": 33, "total": 33}, "response": []}})
    server = Server()
    register_tools(server, Settings(enabled=True, api_key="FAKE"), test_mock=mock)
    out = run(server.tools, "apifootball_odds_mapping", page=33)
    assert out["pagination_state"]["state"] == "EMPTY_PAGE"
