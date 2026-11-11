"""FIX-09: /odds/live passes through untouched (null stays null); a DERIVED summary tells
active, suspended, null, empty, malformed and unavailable apart using only repo-evidenced
selection fields; the E2E runner grades an all-suspended / all-null / never-open reply PARTIAL,
never PASS. C08 stays NOT proven: these are sanitized repo-derived or ASSUMED shapes, not a
real capture."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from bridge.api_football.client import MockFootballClient
from bridge.api_football.config import Settings
from bridge.api_football.errors import ProviderHTTPError
from bridge.api_football.live_odds_state import classify
from bridge.api_football.tools import register_tools
from scripts import mcp_e2e_render as e2e

SHAPES = json.loads((Path(__file__).parent / "fixtures/apifootball_live_odds/shapes.json").read_text())["shapes"]
BY_NAME = {s["name"]: s for s in SHAPES}


def test_every_shape_names_its_evidence_and_no_shape_claims_a_real_capture():
    for s in SHAPES:
        assert s["evidence"].startswith(("REPO_FIXTURE ", "ASSUMED"))
        if s["evidence"].startswith("REPO_FIXTURE "):
            assert (Path(__file__).resolve().parents[1] / s["evidence"].split()[1]).exists()
    assert {s["expected_state"] for s in SHAPES} == {"ACTIVE", "ALL_SUSPENDED", "ALL_NULL_ODDS",
                                                     "NO_OPEN_PRICED_SELECTION", "EMPTY", "MALFORMED"}


@pytest.mark.parametrize("shape", SHAPES, ids=[s["name"] for s in SHAPES])
def test_bridge_state_per_shape(shape):
    before = copy.deepcopy(shape["payload"])
    out = classify(shape["payload"])
    assert out["state"] == shape["expected_state"] and out["label"] == "DERIVED"
    assert shape["payload"] == before  # never mutated


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


class _RealShaped:
    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error

    async def get(self, path, params=None):
        if self.error:
            raise self.error
        return {"data": copy.deepcopy(self.payload), "http_status": 200, "cache_status": "MISS"}


def _live_tool(client):
    reg = _Reg()
    register_tools(reg, Settings(enabled=True, api_key="fake", live_authorized=True), client=client)
    return reg.tools["apifootball_live_odds"]


@pytest.mark.parametrize("shape", [s for s in SHAPES if s["expected_state"] != "MALFORMED"],
                         ids=lambda s: s["name"])
def test_tool_passes_the_payload_through_and_adds_the_state_beside_it(shape):
    out = asyncio.run(_live_tool(_RealShaped(shape["payload"]))(fixture=1000001))
    assert out["status"] == "REAL_API_RESPONSE"
    assert out["data"] == shape["payload"]  # null stays null, nothing added inside the data
    assert out["live_odds_state"]["state"] == shape["expected_state"]


def test_null_odd_and_null_main_stay_null_in_the_tool_output():
    out = asyncio.run(_live_tool(_RealShaped(BY_NAME["suspended_null_odd"]["payload"]))(fixture=1000003))
    v = out["data"]["response"][0]["odds"][0]["values"][0]
    assert v["odd"] is None and v["main"] is None and v["suspended"] is True


def test_no_reply_is_unavailable_with_the_fixed_code():
    out = asyncio.run(_live_tool(_RealShaped(error=ProviderHTTPError(503)))(fixture=5))
    assert out["status"] == "PROVIDER_ERROR"
    assert out["live_odds_state"] == {"label": "DERIVED", "state": "UNAVAILABLE", "reason": "PROVIDER_SERVER_ERROR"}


def test_mock_path_also_labels_state():
    mock = MockFootballClient({"/odds/live": BY_NAME["active_open_priced"]["payload"]})
    reg = _Reg()
    register_tools(reg, Settings(enabled=True, api_key="fake"), test_mock=mock)
    out = asyncio.run(reg.tools["apifootball_live_odds"](fixture=1000001))
    assert out["status"] == "MOCK_VERIFIED" and out["live_odds_state"]["state"] == "ACTIVE"


def _c08_verdict(payload, fixture_id):
    suite = e2e.ApiFootballSuite(lambda tool, args: None, e2e.ProviderBudget(3), plan="apifootball_completion",
                                 sleep=lambda s: None, spacing=0)
    suite.ctx["fixture_live"] = fixture_id
    case = next(c for c in e2e._plan("apifootball_completion") if c.cid == "C08")
    p = {"status": "REAL_API_RESPONSE", "http_status": 200, "endpoint": "/odds/live", "cache_status": "MISS",
         "parameters": {"fixture": fixture_id}, "pagination": payload["paging"], "data": payload}
    return suite._evaluate(case, p, None)


@pytest.mark.parametrize("name,verdict", [
    ("active_open_priced", "PASS"),
    ("suspended_zero_odd", "PARTIAL"), ("suspended_null_odd", "PARTIAL"),
    ("all_null_odds_not_flagged", "PARTIAL"), ("no_suspension_field", "PARTIAL"),
    ("empty_response", "PARTIAL"), ("fixture_without_markets", "PARTIAL"),
    ("markets_key_missing", "FAIL"),
])
def test_e2e_runner_grades_live_states(name, verdict):
    payload = BY_NAME[name]["payload"]
    fid = payload["response"][0]["fixture"]["id"] if payload["response"] else 1000006
    payload = {**payload, "parameters": {"fixture": str(fid)}}
    rec = _c08_verdict(payload, fid)
    assert rec["verdict"] == verdict, rec
    if verdict == "PARTIAL" and payload["response"]:
        assert rec["facts"]["live_state"] in e2e.LIVE_PARTIAL_STATES


def test_c08_is_not_proven_by_these_fixtures():
    # Guard against reading this file as certification: no shape is a real capture.
    assert not any(s["evidence"].startswith("REAL") for s in SHAPES)


@pytest.mark.parametrize("bad", [
    {"value": "Over", "odd": "0", "suspended": False},   # zero on an open selection
    {"value": "Over", "odd": "", "suspended": False},
    {"value": "Over", "odd": "0.5", "suspended": False},
    {"value": "Over", "odd": "0.5", "suspended": True},
    {"value": "Over", "odd": 1.9, "suspended": False},   # a number, not the provider's string
    {"value": "Over", "odd": "nan", "suspended": False},
    {"value": "Over", "odd": "1e3", "suspended": False},
    {"value": "Over", "suspended": False},               # odd missing
    {"odd": "1.90", "suspended": False},                 # value missing
])
def test_a_bad_price_is_malformed_never_active(bad):
    payload = copy.deepcopy(BY_NAME["active_open_priced"]["payload"])
    payload["response"][0]["odds"][0]["values"].append(bad)
    assert classify(payload)["state"] == "MALFORMED"
    # The E2E runner agrees: the same selection is a defect there.
    page = {"status": "REAL_API_RESPONSE", "data": payload}
    assert e2e._check_odds(page, {"fixture_live": 1000001}, {}, live=True, fixture_key="fixture_live")


def test_suspended_zero_and_null_stay_valid():
    for good in ({"value": "Over", "odd": "0", "suspended": True}, {"value": "Over", "odd": None, "suspended": False}):
        payload = copy.deepcopy(BY_NAME["active_open_priced"]["payload"])
        payload["response"][0]["odds"][0]["values"].append(good)
        assert classify(payload)["state"] == "ACTIVE"
