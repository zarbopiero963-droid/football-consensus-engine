"""Regressions found by the real MCP client run of 2026-10-31.

1. Real replies are wrapped ({"data": <provider envelope>, ...}); the top-level
   `pagination` read the wrapper and was therefore ALWAYS null for real calls.
2. /odds/mapping returned paging total=45 but the tool exposed no `page`,
   so pages 2..45 were unreachable from the MCP client.
3. Real replies carried the offline placeholder warning
   "Offline PR-03 tool: never claim real coverage ...".
"""
import asyncio

from bridge.api_football.config import Settings
from bridge.api_football.contracts import ENDPOINTS, ParameterError, validate
from bridge.api_football.tools import register_tools

import pytest


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


class _Client:
    def __init__(self, envelope):
        self.envelope = envelope
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return {"data": self.envelope, "http_status": 200, "cache_status": "MISS",
                "quota_remaining_provider_day": 7000, "quota_remaining_provider_minute": 290}


def _tools(envelope):
    registry = _Registry()
    client = _Client(envelope)
    register_tools(registry, Settings(enabled=True, api_key="fake-test-key", live_authorized=True), client=client)
    return registry.tools, client


def _mapping(page, total=45):
    return {"get": "odds/mapping", "parameters": {"page": str(page)} if page > 1 else [], "errors": [],
            "results": 1, "paging": {"current": page, "total": total},
            "response": [{"league": {"id": 253, "season": 2026}, "fixture": {"id": 1490556},
                          "update": "2026-10-24T18:00:38+00:00"}]}


def test_real_reply_surfaces_provider_pagination():
    tools, _ = _tools(_mapping(1))
    out = asyncio.run(tools["apifootball_odds_mapping"]())
    assert out["status"] == "REAL_API_RESPONSE"
    assert out["pagination"] == {"current": 1, "total": 45}


def test_odds_mapping_page_is_forwarded_and_reachable():
    tools, client = _tools(_mapping(2))
    out = asyncio.run(tools["apifootball_odds_mapping"](page=2))
    assert client.calls == [("/odds/mapping", {"page": 2})]
    assert out["pagination"] == {"current": 2, "total": 45}


def test_odds_mapping_page_bounds_and_no_other_filter():
    assert ENDPOINTS["apifootball_odds_mapping"].fields == ("page",)
    for bad in ({"page": 0}, {"page": 101}, {"page": "2"}, {"league": 39}, {"fixture": 1}):
        with pytest.raises(ParameterError):
            validate("apifootball_odds_mapping", bad)
    _, params = validate("apifootball_odds_mapping", {})
    assert params == {}


def test_real_reply_without_paging_keeps_null_pagination():
    envelope = {"get": "status", "errors": [], "results": 1, "response": {
        "subscription": {"plan": "Pro", "active": True, "end": "x"},
        "requests": {"current": 1, "limit_day": 7500}, "account": {"email": "private@example.invalid"}}}
    tools, _ = _tools(envelope)
    out = asyncio.run(tools["apifootball_status"]())
    assert out["pagination"] is None
    assert "private@example.invalid" not in repr(out)


def test_real_reply_has_no_offline_placeholder_warning():
    tools, _ = _tools(_mapping(1))
    out = asyncio.run(tools["apifootball_odds_mapping"]())
    assert not any("Offline" in w or "separate owner authorization" in w for w in out["warnings"])
    assert out["warnings"]


def test_gated_reply_keeps_offline_warning():
    registry = _Registry()
    register_tools(registry, Settings(enabled=True, api_key=""))
    out = asyncio.run(registry.tools["apifootball_odds_mapping"]())
    assert out["status"] == "KEY_NOT_CONFIGURED"
    assert any("Offline" in w for w in out["warnings"])
