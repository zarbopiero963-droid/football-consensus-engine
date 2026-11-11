"""Shared synthetic provider for the FIX-01 continuation and streaming tests (no network)."""
from __future__ import annotations

import asyncio
import json

import httpx

from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.continuation import provider_arguments
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import register_tools
from bridge.api_football.transport import InjectedHTTPXTransport

KEY = "LOCAL_FAKE_KEY_CONTINUATION_ONLY"
PAD = "Stadio-" + "v" * 420  # a listing row of ~600 bytes
CHUNK = 16_384


def fixture_row(fid, day="2026-11-10", league=39, season=2026, rnd="Regular Season - 1", pad=PAD):
    return {"fixture": {"id": fid, "date": day + "T15:00:00+00:00", "timestamp": 1_794_000_000 + fid,
                        "venue": {"id": None, "name": pad}, "status": {"short": "NS", "elapsed": None}},
            "league": {"id": league, "season": season, "round": rnd},
            "teams": {"home": {"id": 1}, "away": {"id": 2}}, "goals": {"home": None, "away": None}}


class Provider:
    """A fake API-FOOTBALL. `fixtures` is a list of fixture rows in provider order."""

    def __init__(self, fixtures=(), leagues=(), reverse_follow_ups=False, extra_chunks=None):
        self.fixtures = list(fixtures)
        self.leagues = list(leagues)
        self.reverse = reverse_follow_ups
        self.requests = []
        self.bodies = []

    def rows_for(self, path, params):
        if path == "/leagues":
            rows = self.leagues
            if "id" in params:
                rows = [r for r in rows if str(r["league"]["id"]) == params["id"]]
            return rows, False
        rows = self.fixtures
        follow = False
        if "ids" in params or "id" in params:
            wanted = {int(x) for x in (params.get("ids") or params["id"]).split("-")}
            rows, follow = [r for r in rows if r["fixture"]["id"] in wanted], True
        if "date" in params:
            rows = [r for r in rows if r["fixture"]["date"].startswith(params["date"])]
        if "league" in params:
            rows, follow = [r for r in rows if str(r["league"]["id"]) == params["league"]], "date" in params
        if "season" in params:
            rows = [r for r in rows if str(r["league"]["season"]) == params["season"]]
        if "round" in params:
            rows, follow = [r for r in rows if r["league"]["round"] == params["round"]], True
        if follow and self.reverse:
            rows = list(reversed(rows))
        return rows, follow

    def body(self, request):
        params = dict(request.url.params)
        path = request.url.path
        self.requests.append((path, params))
        rows, _ = self.rows_for(path, params)
        body = json.dumps({"get": path.strip("/"), "parameters": params, "errors": [], "results": len(rows),
                           "paging": {"current": 1, "total": 1}, "response": rows}).encode()
        self.bodies.append(body)
        return body

    def handler(self, request):
        body = self.body(request)

        async def stream():
            for i in range(0, len(body), CHUNK):
                yield body[i:i + CHUNK]
        return httpx.Response(200, content=stream())


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


def boot(provider, *, limits=None, clock=None):
    """A fresh bridge process: new client, new cache, no state shared with an earlier boot."""
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True, cache_enabled=False, timeout_seconds=30)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(provider.handler))
    transport = InjectedHTTPXTransport(http, api_key=KEY)
    if limits is not None:
        transport.scan_limits = limits
    if clock is not None:
        transport.scan_clock = clock
    client = ResilientFootballClient(settings, transport, quota=Quota(),
                                     retry=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0))
    server = Server()
    register_tools(server, settings, client=client)
    return server.tools


def call(tools, name, mcp_args):
    return asyncio.run(tools[name](**mcp_args))


def fixture_ids(out):
    return [r["fixture"]["id"] for r in out["data"]["response"]]


def follow_all(tools, manifest, id_of=fixture_ids):
    """The client side: run every follow-up and compare id SETS and counts (order not asserted)."""
    rows, problems = [], []
    for c in manifest["follow_up"]["calls"]:
        out = call(tools, manifest["follow_up"]["tool"], c["arguments"])
        assert out["status"] == "REAL_API_RESPONSE", (out.get("status"), out.get("error"))
        got = id_of(out)
        if sorted(got) != sorted(c["expect_ids"]) or len(got) != c["expect_rows"]:
            problems.append({"expected": c["expect_ids"], "got": got})
        rows.extend(got)
    return rows, problems


def validate_follow_up(tool, mcp_args):
    from bridge.api_football.contracts import validate
    return validate(tool, provider_arguments(mcp_args))
