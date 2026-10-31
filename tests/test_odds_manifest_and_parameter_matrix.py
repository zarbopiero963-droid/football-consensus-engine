from bridge.api_football.odds_manifest import odds_page_manifest
from bridge.api_football.contracts import ENDPOINTS
from scripts.print_api_football_parameter_matrix import matrix
import pytest

def sample(n=3):
    return {"data":{"get":"odds","paging":{"current":1,"total":2},
      "response":[{"fixture":{"id":100+i},"bookmakers":[{"id":4,"bets":[
        {"id":5,"values":[{"value":"Over 2.5","odd":"1.80"},
                          {"value":"Under 2.5","odd":"2.00"}]}]}]} for i in range(n)]}}

def test_manifest_accurate_counts_and_partial_flag():
    result=odds_page_manifest(sample())
    assert result["partial"] is True
    assert result["prices_included"] is False
    assert result["fixtures_in_page"]==3
    assert result["market_groups_in_page"]==3
    assert result["selections_in_page"]==6
    assert result["fixture_list_complete"] is True
    assert result["paging"]=={"current":1,"total":2}
    assert "1.80" not in str(result)

def test_manifest_does_not_pretend_complete_if_capped():
    result=odds_page_manifest(sample(),max_fixtures=2)
    assert result["fixture_list_complete"] is False
    assert len(result["fixture_ids"])==2

def test_malformed_fails_closed():
    with pytest.raises(ValueError):
        odds_page_manifest({"data":{"get":"odds","response":[{}],"paging":{}}})

def test_existing_official_fields_present_in_source():
    assert "ids" in ENDPOINTS["apifootball_fixtures"].fields
    assert "half" in ENDPOINTS["apifootball_fixture_statistics"].fields
    rows={entry["tool"]:entry for entry in matrix()}
    assert rows["apifootball_fixtures"]["connected_plugin_schema"]=="REQUIRES_INDEPENDENT_DISCOVERY"
    assert all(x["provider_real_coverage"]=="NOT_EXHAUSTIVELY_VERIFIED" for x in rows.values())


# --- Tool-level and transport-level regressions (PR #125 hardening) ---
import asyncio
import json

import httpx

from bridge.api_football.config import Settings
from bridge.api_football.tools import register_tools
from bridge.api_football.transport import InjectedHTTPXTransport, OFFICIAL_BASE_URL
from bridge.api_football.errors import ProviderDataError


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


def _big_page(n=400, *, drop=None):
    def book(b):
        return {"id": b, "name": "B%d" % b, "bets": [
            {"id": k, "name": "M%d" % k, "values": [{"value": "Home", "odd": "1.95"}] * 6}
            for k in range(10)]}
    response = [{"fixture": {"id": 1000 + i}, "bookmakers": [book(b) for b in range(5)]} for i in range(n)]
    # Corrupt only the first fixture so the page stays oversized.
    if drop == "bets":
        del response[0]["bookmakers"][0]["bets"]
    if drop == "values":
        del response[0]["bookmakers"][0]["bets"][0]["values"]
    if drop == "bookmakers":
        del response[0]["bookmakers"]
    return {"get": "odds", "errors": [], "results": n,
            "paging": {"current": 1, "total": 3}, "response": response}


class _RealShapedClient:
    """Shape of ResilientFootballClient.get output; no network."""

    def __init__(self, payload):
        self.payload = payload

    async def get(self, path, params=None):
        return {"data": self.payload, "http_status": 200, "cache_status": "MISS",
                "quota_remaining_provider_day": 123, "quota_remaining_provider_minute": 9}


def _odds_tool(payload):
    registry = _Registry()
    register_tools(registry, Settings(enabled=True, api_key="fake-test-key", live_authorized=True),
                   client=_RealShapedClient(payload))
    return registry.tools["apifootball_odds"]


def test_oversized_real_page_returns_bounded_manifest_with_quota_and_provenance():
    payload = _big_page()
    assert len(json.dumps(payload).encode()) > 262_144
    result = asyncio.run(_odds_tool(payload)(league=39, season=2026))
    assert result["status"] == "PARTIAL_ODDS_MANIFEST"
    assert result["source_status"] == "REAL_API_RESPONSE"
    assert result["quota_remaining"] == {"day": 123, "minute": 9}
    assert result["http_status"] == 200
    assert result["network_calls_made"] == 1
    assert result["data"]["prices_included"] is False
    assert result["data"]["fixtures_in_page"] == 400
    assert result["data"]["fixture_list_complete"] is False
    assert result["data"]["selections_in_page"] == 400 * 5 * 10 * 6
    assert result["pagination"] == {"current": 1, "total": 3}
    assert "1.95" not in json.dumps(result)
    assert len(json.dumps(result).encode()) <= 262_144


def test_small_real_page_is_returned_losslessly_not_as_manifest():
    payload = _big_page(2)
    result = asyncio.run(_odds_tool(payload)(fixture=1000))
    assert result["status"] == "REAL_API_RESPONSE"
    assert result["data"] == payload


@pytest.mark.parametrize("drop", ["bets", "values", "bookmakers"])
def test_missing_lists_fail_closed_explicitly_never_zero(drop):
    with pytest.raises(ValueError):
        odds_page_manifest(_big_page(2, drop=drop))
    result = asyncio.run(_odds_tool(_big_page(400, drop=drop))(league=39, season=2026))
    assert result["status"] == "PAYLOAD_TOO_LARGE"
    assert result["error"] == {"code": "PROVIDER_RESPONSE_TOO_LARGE"}
    assert result["manifest_error"]["code"].startswith("INVALID_ODDS_")
    assert result["data"] is None


def test_non_string_get_is_rejected_as_value_error():
    with pytest.raises(ValueError):
        odds_page_manifest({"get": None, "response": [], "paging": {}})


def test_mock_manifest_keeps_mock_provenance():
    class _Mock:
        async def get(self, path, params=None):
            return _big_page()
    registry = _Registry()
    register_tools(registry, Settings(enabled=True), test_mock=_Mock())
    result = asyncio.run(registry.tools["apifootball_odds"](league=39, season=2026))
    assert result["status"] == "PARTIAL_ODDS_MANIFEST"
    assert result["source_status"] == "MOCK_VERIFIED"
    assert result["network_calls_made"] == 0
    assert result["quota_remaining"] is None


def _transport_get(path, size):
    body = json.dumps({"get": path.strip("/"), "errors": [], "response": [{"x": "a" * size}],
                       "paging": {"current": 1, "total": 1}}).encode()

    async def run():
        client = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL,
                                   transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
        async with client:
            return await InjectedHTTPXTransport(client, api_key="fake-test-key").get(path, {"fixture": 1})
    return asyncio.run(run())


def test_transport_cap_is_2mib_only_for_odds_and_live_odds():
    assert _transport_get("/odds", 1_000_000).bytes_received > 524_288
    assert _transport_get("/odds/live", 1_000_000).bytes_received > 524_288
    for path, size in (("/odds", 2_200_000), ("/odds/live", 2_200_000), ("/fixtures", 600_000)):
        with pytest.raises(ProviderDataError) as info:
            _transport_get(path, size)
        assert info.value.code == "PROVIDER_RESPONSE_TOO_LARGE"


def _live_page(fixtures=1, markets=100, values=40, drop=None):
    response = [{"fixture": {"id": 7 + f, "status": {"long": "First Half", "elapsed": 36}},
                 "update": "2026-10-31T22:51:31+00:00",
                 "odds": [{"id": k, "name": "M%d" % k, "values": [
                     {"value": "Over %d.5" % v, "odd": "1.95", "handicap": str(v), "main": None,
                      "suspended": v % 4 == 0} for v in range(values)]} for k in range(markets)]}
                for f in range(fixtures)]
    if drop == "odds":
        del response[0]["odds"]
    if drop == "values":
        del response[0]["odds"][0]["values"]
    if drop == "suspended":
        del response[0]["odds"][0]["values"][0]["suspended"]
    return {"get": "odds/live", "errors": [], "results": fixtures,
            "paging": {"current": 1, "total": 1}, "response": response}


def _live_tool(payload):
    registry = _Registry()
    register_tools(registry, Settings(enabled=True, api_key="fake-test-key", live_authorized=True),
                   client=_RealShapedClient(payload))
    return registry.tools["apifootball_live_odds"]


def test_oversized_live_odds_return_navigable_price_free_manifest():
    live = _live_page()
    assert len(json.dumps(live, separators=(",", ":"), ensure_ascii=False).encode()) > 262_144
    result = asyncio.run(_live_tool(live)(fixture=7))
    assert result["status"] == "PARTIAL_ODDS_MANIFEST"
    assert result["source_status"] == "REAL_API_RESPONSE"
    data = result["data"]
    assert data["phase"] == "LIVE" and data["prices_included"] is False and data["partial"] is True
    assert data["fixtures_in_page"] == 1 and data["markets_in_page"] == 100
    assert data["selections_in_page"] == 4000 and data["suspended_in_page"] == 1000
    fx = data["fixtures"][0]
    assert fx["fixture_id"] == 7 and fx["update"] == "2026-10-31T22:51:31+00:00" and fx["elapsed"] == 36
    assert [m["bet_id"] for m in fx["markets"]] == list(range(100))  # every market retrievable via bet=
    assert data["market_list_complete"] is True and data["fixture_list_complete"] is True
    assert "1.95" not in json.dumps(result)
    assert result["quota_remaining"] == {"day": 123, "minute": 9}
    assert len(json.dumps(result).encode()) <= 262_144


def test_small_live_odds_returned_losslessly():
    live = _live_page(markets=2, values=3)
    result = asyncio.run(_live_tool(live)(fixture=7))
    assert result["status"] == "REAL_API_RESPONSE" and result["data"] == live


def test_live_manifest_flags_capped_market_list():
    from bridge.api_football.odds_manifest import live_odds_manifest
    out = live_odds_manifest(_live_page(markets=5, values=1), max_markets_per_fixture=3)
    assert out["market_list_complete"] is False and len(out["fixtures"][0]["markets"]) == 3
    assert out["markets_in_page"] == 5  # totals never truncated


def test_live_manifest_missing_suspended_is_unknown_not_false():
    from bridge.api_football.odds_manifest import live_odds_manifest
    out = live_odds_manifest(_live_page(markets=1, values=4, drop="suspended"))
    m = out["fixtures"][0]["markets"][0]
    assert m["suspension_unknown"] == 1 and m["suspended"] == 0  # v=0 suspended flag removed


@pytest.mark.parametrize("drop", ["odds", "values"])
def test_oversized_malformed_live_odds_fail_closed(drop):
    result = asyncio.run(_live_tool(_live_page(fixtures=2, drop=drop))(league=128))
    assert result["status"] == "PAYLOAD_TOO_LARGE"
    assert result["error"] == {"code": "PROVIDER_RESPONSE_TOO_LARGE"}
    assert result["manifest_error"]["code"].startswith("INVALID_LIVE_ODDS_")
    assert result["data"] is None and "1.95" not in json.dumps(result)


def test_live_manifest_rejects_prematch_envelope():
    from bridge.api_football.odds_manifest import live_odds_manifest
    with pytest.raises(ValueError):
        live_odds_manifest({"get": "odds", "response": [], "paging": {}})
