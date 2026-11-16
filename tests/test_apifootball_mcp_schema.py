"""WP6 (E-G1, E-G3): the pinned apifootball_* MCP surface the live E2E compares against.

- docs/certification/apifootball/mcp_schema_v1.json is exactly what the server's own registry
  produces now (scripts/apifootball_mcp_schema.py --check): one source of truth, no second copy;
- its 42 names are the contracts plus the two local tools, and its hashes recompute;
- every pinned zero-call negative, sent through the REAL MCPServer and tools.py with live requests
  authorized and a provider transport that counts every request, comes back as its pinned local
  refusal with no provider request and no quota lease;
- unknown arguments are NOT a zero-call negative: the MCP SDK drops them and the call is real;
- the runner's guard lets a provider tool through without a budget reservation only for the one
  open pinned probe, with exactly its pinned arguments.
Offline only: no network, no database, no secret.
"""
from __future__ import annotations

import asyncio
import copy
import json

import pydantic
import pytest

import _apifootball_fake_provider as fake
from bridge.api_football.contracts import ENDPOINTS
from scripts import apifootball_mcp_schema as gen
from scripts import mcp_e2e_render as e2e


def _pinned():
    return e2e.load_expected_mcp_schema()


def test_the_committed_surface_is_what_the_registry_produces_now():
    assert gen.main(["--check"]) == 0


def test_the_pinned_names_are_the_contracts_plus_the_two_local_tools_and_the_hashes_recompute():
    pinned = _pinned()
    assert pinned["format"] == e2e.MCP_SCHEMA_FORMAT and pinned["tool_count"] == 42
    assert set(pinned["tools"]) == set(ENDPOINTS) | {"apifootball_health", "apifootball_quota_report"}
    assert set(e2e.APIFOOTBALL_PROVIDER_TOOLS) <= set(pinned["tools"])
    for name, t in pinned["tools"].items():
        assert t["projection_sha256"] == e2e._sha256(t["projection"]), name
        contract = ENDPOINTS.get(name)
        if contract is not None:  # the schema exposes exactly the contract parameters (from/to renamed)
            exposed = {"from" if p == "from_date" else "to" if p == "to_date" else p for p in t["projection"]["properties"]}
            assert exposed == set(contract.fields), name
            assert {"from" if p == "from_date" else p for p in t["projection"]["required"]} == set(contract.required), name
    assert pinned["schema_set_sha256"] == e2e._sha256({n: t["projection"] for n, t in pinned["tools"].items()})


def test_a_tampered_or_missing_pinned_file_is_a_fixed_failure(tmp_path):
    pinned = _pinned()
    bad = copy.deepcopy(pinned)
    bad["tools"]["apifootball_odds"]["projection"]["properties"].pop("page")  # hash no longer matches
    for name, data in (("hash.json", bad), ("count.json", {**pinned, "tool_count": 41}),
                       ("format.json", {**pinned, "format": "other"})):
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(e2e.Fail, match="expected_mcp_schema_invalid"):
            e2e.load_expected_mcp_schema(str(path))
    with pytest.raises(e2e.Fail, match="expected_mcp_schema_unavailable"):
        e2e.load_expected_mcp_schema(str(tmp_path / "absent.json"))


def test_the_projection_keeps_types_defaults_required_and_strictness_and_drops_documentation():
    a = {"type": "object", "title": "A", "required": ["b", "a"],
         "properties": {"a": {"type": "integer", "title": "A", "description": "x"},
                        "b": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}}}
    b = json.loads(json.dumps(a))
    b["title"], b["properties"]["a"]["description"], b["required"] = "B", "y", ["a", "b"]
    assert e2e.schema_projection(a) == e2e.schema_projection(b)
    assert e2e.schema_projection(a)["properties"] == {"a": {"types": ["integer"]},
                                                      "b": {"types": ["null", "string"], "default": None}}
    for change in (lambda s: s["properties"]["a"].update(type="string"),
                   lambda s: s["properties"]["b"].pop("default"),
                   lambda s: s["required"].pop(),
                   lambda s: s.update(additionalProperties=False),
                   lambda s: s["properties"].update(c={"type": "integer"})):
        c = json.loads(json.dumps(a))
        change(c)
        assert e2e._sha256(e2e.schema_projection(c)) != e2e._sha256(e2e.schema_projection(a))


def _live_server():
    """The REAL MCPServer + tools.py, live requests authorized, over a counting fake provider."""
    from mcp.server import MCPServer
    from bridge.api_football.config import Settings
    from bridge.api_football.tools import register_tools
    transport, quota = fake.FakeTransport(), fake.FakeQuota()
    server = MCPServer("wp6-offline")
    register_tools(server, Settings(enabled=True, api_key=fake.KEY, live_authorized=True),
                   client=fake.build_client(transport, quota))
    return server, transport, quota


async def _call(server, tool, args):
    try:
        res = await server.call_tool(tool, args)
    except Exception as exc:  # ToolError; isError=true on the wire
        return {"schema_rejected": isinstance(exc.__cause__, pydantic.ValidationError)}
    return res.model_dump(by_alias=True, mode="json")


def test_every_pinned_negative_is_refused_locally_with_no_provider_request_and_no_quota_lease():
    pinned = _pinned()
    server, transport, quota = _live_server()
    cases = pinned["zero_call_negatives"]
    assert len(cases) >= 40 and {c["expect"] for c in cases} == {"INVALID_INPUT", "MCP_SCHEMA_REJECTED"}
    with_params = {n for n in ENDPOINTS if pinned["tools"][n]["projection"]["properties"]}
    assert {c["tool"] for c in cases if c["expect"] == "INVALID_INPUT"} == with_params  # one per tool
    required = {n for n in ENDPOINTS if ENDPOINTS[n].required}
    assert {c["tool"] for c in cases if c["class"] == "MISSING_REQUIRED"} == required

    async def run():
        for c in cases:
            # Only parameters the schema declares: an unknown one would be dropped, not refused.
            assert set(c["arguments"]) <= set(pinned["tools"][c["tool"]]["projection"]["properties"]), c["id"]
            out = await _call(server, c["tool"], c["arguments"])
            if c["expect"] == "MCP_SCHEMA_REJECTED":
                assert out == {"schema_rejected": True}, c["id"]
            else:
                p = out["structuredContent"]
                assert p["status"] == "INVALID_INPUT" and p["error"]["code"] == c["code"], c["id"]
                assert p["network_calls_made"] == 0 and c["code"] and e2e._code(c["code"]) == c["code"]
    asyncio.run(run())
    assert transport.requests == [] and quota.leases == 0


def test_unknown_arguments_and_numeric_strings_are_not_zero_call_negatives():
    """Why the pinned set has no unknown-parameter case and no parameterless tool: the SDK drops
    an unknown argument and coerces "5" to 5, so both calls reach the provider."""
    server, transport, quota = _live_server()

    async def run():
        a = await _call(server, "apifootball_predictions", {"fixture": 5, "not_a_parameter": 1})
        b = await _call(server, "apifootball_predictions", {"fixture": "6"})
        c = await _call(server, "apifootball_status", {"not_a_parameter": 1})
        return a, b, c
    a, b, c = asyncio.run(run())
    assert a["structuredContent"]["status"] == b["structuredContent"]["status"] == "REAL_API_RESPONSE"
    assert c["structuredContent"]["status"] == "REAL_API_RESPONSE"
    assert [p for p, _ in transport.requests] == ["/predictions", "/predictions", "/status"]
    pinned = _pinned()
    assert set(pinned["zero_call_not_testable"]) == {n for n in ENDPOINTS if not ENDPOINTS[n].fields}
    assert set(pinned["zero_call_not_testable"].values()) == {e2e.NO_LOCAL_REJECTION_REASON}


class _Recorder:
    def __init__(self):
        self.posts = []

    def post(self, url, headers=None, json=None, data=None):
        self.posts.append(json)
        return e2e.Response(200, {"content-type": "application/json"}, b'{"result": {}}')


def test_the_guard_admits_a_provider_tool_without_reservation_only_for_the_open_pinned_probe():
    run = e2e.Run(client=_Recorder(), base=e2e.PINNED_BASE, password="p", expected_commit="a" * 40,
                  mask=lambda v: None)
    case = _pinned()["zero_call_negatives"][0]
    with pytest.raises(e2e.Fail, match="tool_not_allowlisted"):
        run.rpc("tools/call", {"name": case["tool"], "arguments": case["arguments"]}, "t")
    run._negative_open = (case["tool"], e2e._canonical(case["arguments"]))
    for args in ({}, {"id": 1}, {**case["arguments"], "x": 1}):  # any other argument set stays refused
        with pytest.raises(e2e.Fail):
            run.rpc("tools/call", {"name": case["tool"], "arguments": args}, "t")
    with pytest.raises(e2e.Fail):
        run.rpc("tools/call", {"name": "apifootball_odds", "arguments": case["arguments"]}, "t")
    run.rpc("tools/call", {"name": case["tool"], "arguments": dict(case["arguments"])}, "t")
    assert len(run.c.posts) == 1 and run.tools_called == [case["tool"]]
    # The probe closes after one call; the budget model of the opt-in suites is unchanged.
    run._negative_open = None
    run.negative_records, run.negatives_state = [], "RUN"
    assert run._negative_probe(case, "t")["charged"] in (0, e2e.APIFOOTBALL_WORST_CASE_PER_CALL)
    assert run._negative_open is None
    assert e2e.APIFOOTBALL_BUDGET_CEILING == 45 and e2e.APIFOOTBALL_WORST_CASE_PER_CALL == 3
    assert e2e.SUITE_BUDGET_CEILING == {"apifootball": 20, "apifootball_full": 20, "apifootball_completion": 45}


def test_an_unexpected_reply_to_a_probe_is_charged_the_worst_case():
    class _Reply(_Recorder):
        def __init__(self, body):
            super().__init__()
            self.body = body

        def post(self, url, headers=None, json=None, data=None):
            return e2e.Response(200, {"content-type": "application/json"}, self.body)
    case = next(c for c in _pinned()["zero_call_negatives"] if c["expect"] == "INVALID_INPUT")
    real = json.dumps({"result": {"isError": False, "structuredContent": {
        "status": "REAL_API_RESPONSE", "network_calls_made": 1}}}).encode()
    other_code = json.dumps({"result": {"isError": False, "structuredContent": {
        "status": "INVALID_INPUT", "error": {"code": "SOMETHING_ELSE"}, "network_calls_made": 0}}}).encode()
    schema = json.dumps({"result": {"isError": True, "content": [{"type": "text", "text": "x"}]}}).encode()
    for body, verdict, charged in ((real, "FAIL", 3), (b"not json", "FAIL", 3), (other_code, "FAIL", 0),
                                   (schema, "FAIL", 0)):
        run = e2e.Run(client=_Reply(body), base=e2e.PINNED_BASE, password="p", expected_commit="a" * 40,
                      mask=lambda v: None)
        out = run._negative_probe(case, "t")
        assert (out["verdict"], out["charged"]) == (verdict, charged), body
