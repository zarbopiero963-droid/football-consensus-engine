import asyncio
import json

import pytest
from mcp.server import MCPServer

from bridge.api_football import registry, tools
from conftest import FAKE_KEY, SAMPLES, WIRE_SAMPLES, Recorder, endpoint, envelope, json_response

CASES = [(e.tool_name, p) for e in registry.implemented() for p in e.params]


@pytest.fixture
def server(monkeypatch, make_client):
    recorder = Recorder(json_response(envelope()))
    monkeypatch.setattr(tools, "_client", make_client(recorder))
    server = MCPServer("contract-test")
    tools.register(server)
    server.recorder = recorder
    return server


def call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("tool_name,param", CASES, ids=[f"{t}:{p.wire}" for t, p in CASES])
def test_every_registry_parameter_reaches_the_provider(server, tool_name, param):
    result = call(server, tool_name, {param.name: SAMPLES[param.kind], "use_cache": False})
    assert result["ok"] is True, result
    request = server.recorder.requests[-1]
    assert request.method == "GET"
    assert request.url.host == "v3.football.api-sports.io"
    assert request.url.path == endpoint(tool_name).path
    assert dict(request.url.params) == {param.wire: WIRE_SAMPLES[param.kind]}
    assert request.headers["x-apisports-key"] == FAKE_KEY
    assert FAKE_KEY not in str(request.url)
    assert result["parameters"] == {param.wire: WIRE_SAMPLES[param.kind]}
    assert result["registry_id"] == endpoint(tool_name).id
    assert result["parameter_contract"] == "OWNER_VERIFIED_NAMES"
    assert result["client_validation"] == "PROVISIONAL_CLIENT_POLICY"


def test_tool_schemas_expose_exactly_registry_parameters(server):
    listed = {t.name: t for t in asyncio.run(server.list_tools())}
    for e in registry.implemented():
        properties = listed[e.tool_name].input_schema["properties"]
        assert set(properties) == {p.name for p in e.params} | {"use_cache"}
        # Required-ness is pending official verification: nothing is declared required.
        assert listed[e.tool_name].input_schema.get("required", []) == []
        assert listed[e.tool_name].annotations.read_only_hint is True
        assert e.path in listed[e.tool_name].description


@pytest.mark.parametrize("tool_name", [e.tool_name for e in registry.implemented()])
def test_empty_call_is_refused_without_network(server, tool_name):
    result = call(server, tool_name, {})
    assert result["status"] == "INVALID_ARGUMENT"
    assert server.recorder.requests == []


@pytest.mark.parametrize("arguments,message", [
    ({"id": "12"}, "must be an integer"),
    ({"id": -1}, "within"),
    ({"date": "2026-02-30"}, "valid YYYY-MM-DD"),
    ({"date": "24/10/2026"}, "valid YYYY-MM-DD"),
    ({"season": 1800}, "within"),
    ({"ids": "1-2-1"}, "duplicate"),
    ({"ids": "-".join(str(n) for n in range(1, 22))}, "at most 20"),
    ({"ids": "1,2"}, "positive ids"),
    ({"live": "everything"}, "positive ids"),
    ({"status": "ns"}, "unsupported characters"),
    ({"timezone": "Europe/Rome; DROP"}, "unsupported characters"),
    ({"round": "x" * 101}, "length"),
    ({"round": "Round&league=1"}, "unsupported characters"),
    ({"bogus": 1}, "unsupported parameters"),
])
def test_invalid_input_is_refused_before_any_request(make_client, arguments, message):
    recorder = Recorder(json_response(envelope()))
    result = asyncio.run(make_client(recorder).call(endpoint("apifootball_fixtures"), arguments))
    assert result["status"] == "INVALID_ARGUMENT"
    assert message in result["error"]["message"]
    assert recorder.requests == []


def test_none_is_not_sent_and_zero_is_rejected_not_dropped(make_client):
    recorder = Recorder(json_response(envelope()))
    client = make_client(recorder)
    result = asyncio.run(client.call(endpoint("apifootball_fixtures"), {"id": 7, "date": None}))
    assert dict(recorder.requests[-1].url.params) == {"id": "7"}
    assert result["parameters"] == {"id": "7"}
    zero = asyncio.run(client.call(endpoint("apifootball_fixtures"), {"id": 0}))
    assert zero["status"] == "INVALID_ARGUMENT"


def test_text_parameters_cannot_inject_query_syntax(make_client):
    recorder = Recorder(json_response(envelope()))
    client = make_client(recorder)
    for value in ["Shots&fixture=1", "a/../b", "x?y", "a#b", "Shots\nX", "50%", "a=b"]:
        result = asyncio.run(client.call(endpoint("apifootball_fixtures_statistics"), {"fixture": 1, "type": value}))
        assert result["status"] == "INVALID_ARGUMENT", value
    assert recorder.requests == []


def test_raw_envelope_is_preserved_without_normalization(make_client):
    body = envelope(response=[{"score": {"halftime": {"home": 0, "away": None}}, "note": "", "xg": None}],
                    paging={"current": 1, "total": 1})
    recorder = Recorder(json_response(body))
    result = asyncio.run(make_client(recorder).call(endpoint("apifootball_fixtures"), {"id": 1}))
    item = result["raw"]["response"][0]
    assert item["score"]["halftime"] == {"home": 0, "away": None}   # zero stays zero, null stays null
    assert item["note"] == "" and "missing_key" not in item
    assert result["raw"] == body
    assert len(result["raw_sha256"]) == 64 and result["bytes_utf8"] > 0
    assert result["pit_status"] == "NOT_PIT_CERTIFIED"
    assert result["acquired_at_utc"] and result["provider_timestamp_utc"] is None


def test_market_and_prediction_results_carry_provenance_warnings(make_client):
    recorder = Recorder(json_response(envelope()))
    client = make_client(recorder)
    odds = asyncio.run(client.call(endpoint("apifootball_odds"), {"fixture": 1}))
    prediction = asyncio.run(client.call(endpoint("apifootball_predictions"), {"fixture": 1}))
    assert odds["data_class"] == "MARKET_ODDS" and "not a model probability" in odds["warnings"][0]
    assert prediction["data_class"] == "PROVIDER_PREDICTION"
    assert "not point-in-time certified" in prediction["warnings"][0]
