"""Offline stand-in for API-FOOTBALL behind the REAL MCP tool code (tests only, no network).

`FakeTransport` replaces only the pinned HTTPX transport: the real ResilientFootballClient
(retries, circuit breaker, cache) and the real tools.py output shaping stay in the loop, so
the E2E suite is tested against the payloads production would produce. Every transport call is
one provider request; the fake decrements its own daily counter per request and reports it in
the quota headers, exactly like x-ratelimit-requests-remaining.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from bridge.api_football.config import Settings
from bridge.api_football.errors import ProviderDataError, ProviderHTTPError, ProviderUnavailable
from bridge.api_football.quota import QuotaExceeded, QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.transport import ProviderReply

KEY = "LOCAL_FAKE_APIFOOTBALL_KEY_E2E_ONLY"
NOW = 1_762_776_000  # 2025-11-10T12:00:00Z, the clock of the suite under test
DAY = 86_400
PRICE_SENTINEL = "7.777123"  # a decimal odd that must never appear in any report


def _fx(fid: int, ts: int, short: str, home: int, away: int, goals=(None, None), ht=(None, None)) -> dict:
    return {"fixture": {"id": fid, "timestamp": ts, "date": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                        "status": {"short": short, "elapsed": 90 if short == "FT" else None},
                        "venue": {"id": 556, "name": "Stadium"}},
            "league": {"id": 39, "season": 2025},
            "teams": {"home": {"id": home, "name": f"Team {home}"}, "away": {"id": away, "name": f"Team {away}"}},
            "goals": {"home": goals[0], "away": goals[1]},
            "score": {"halftime": {"home": ht[0], "away": ht[1]}, "fulltime": {"home": goals[0], "away": goals[1]}}}


# Window around NOW: two finished at the same, latest kickoff (tie -> lowest id 1002), one older;
# one NS too close to kickoff (< 2 h, excluded), two NS tied at the nearest valid kickoff (-> 2001).
WINDOW = [
    _fx(1001, NOW - 3 * DAY, "FT", 40, 50, (2, 1), (1, 0)),
    _fx(1003, NOW - 2 * DAY, "FT", 33, 34, (0, 0), (0, None)),
    _fx(1002, NOW - 2 * DAY, "AET", 42, 47, (3, 2), (1, 1)),
    _fx(2000, NOW + 3600, "NS", 35, 36),
    _fx(2002, NOW + DAY, "NS", 51, 52),
    _fx(2001, NOW + DAY, "NS", 49, 55),
]
LIVE = [_fx(9003, NOW - 3000, "2H", 61, 62, (1, 0)), _fx(9001, NOW - 2000, "1H", 63, 64, (0, 0))]


def _odds_item(fid: int, odd: str = "1.85") -> dict:
    return {"fixture": {"id": fid}, "league": {"id": 39},
            "bookmakers": [{"id": 8, "name": "Bet365", "bets": [{"id": 5, "name": "Goals Over/Under", "values": [
                {"value": "Over 2.5", "odd": odd}, {"value": "Under 2.5", "odd": "1.95"},
                {"value": "Over 3.5", "odd": "2.60"}]}]}]}


def response_for(path: str, params: dict[str, Any], *, huge_odds: bool = False) -> tuple[Any, dict]:
    """(response, paging) the provider would publish for a request (deterministic)."""
    one = {"current": 1, "total": 1}
    if path == "/status":
        return ({"account": {"firstname": "PRIVATE", "email": "private@example.invalid"},
                 "subscription": {"plan": "Free", "active": True, "end": "2026-12-31"},
                 "requests": {"current": 5, "limit_day": 100}}, one)
    if path == "/leagues" and params.get("current") == "true":
        return ([{"league": {"id": 39, "name": "Premier League"}, "seasons": [
            {"year": 2024, "current": False}, {"year": 2025, "current": True, "coverage": {
                "fixtures": {"events": True, "statistics_fixtures": True}, "standings": True, "odds": True}}]}], one)
    if path == "/fixtures":
        if params.get("live") == "all":
            return (LIVE, one)
        if "id" in params:
            return ([f for f in WINDOW if f["fixture"]["id"] == params["id"]], one)
        return (WINDOW, one)
    if path == "/fixtures/events":
        return ([{"time": {"elapsed": 10, "extra": None}, "team": {"id": 42}, "player": {"id": 7}, "type": "Goal",
                  "detail": "Normal Goal"},
                 {"time": {"elapsed": 30, "extra": None}, "team": {"id": 47}, "player": {"id": None}, "type": "Card",
                  "detail": "Yellow Card"}], one)
    if path == "/fixtures/statistics":
        return ([{"team": {"id": t}, "statistics": [{"type": "Shots on Goal", "value": 3},
                                                    {"type": "Ball Possession", "value": "55%"},
                                                    {"type": "expected_goals", "value": None}]} for t in (42, 47)], one)
    if path in ("/odds/bookmakers", "/odds/bets"):
        rows = ([{"id": 1, "name": "10Bet"}, {"id": 8, "name": "Bet365"}] if path == "/odds/bookmakers" else
                [{"id": 1, "name": "Match Winner"}, {"id": 5, "name": "Goals Over/Under"},
                 {"id": 6, "name": "Goals Over/Under First Half"}])
        return (rows, one)
    if path == "/odds":
        if "fixture" in params:
            return ([_odds_item(params["fixture"], PRICE_SENTINEL)], one)
        page = params.get("page", 1)
        if huge_odds and page == 1:
            item = _odds_item(2001)
            item["bookmakers"] = [dict(item["bookmakers"][0], id=i) for i in range(1, 30)]
            item["bookmakers"][0]["bets"] = [dict(item["bookmakers"][0]["bets"][0], values=[
                {"value": f"Over {i}.5", "odd": PRICE_SENTINEL} for i in range(9000)])]
            return ([item], {"current": 1, "total": 2})
        return ([_odds_item(2001 + page)], {"current": page, "total": 2})
    if path == "/odds/live":
        return ([{"fixture": {"id": params.get("fixture"), "status": {"elapsed": 60}}, "update": "x",
                  "odds": [{"id": 36, "name": "Over/Under Line", "values": [
                      {"value": "Over", "odd": "1.90", "handicap": "2.5", "suspended": False},
                      {"value": "Under", "odd": "1.90", "handicap": "2.5", "suspended": None}]}]}], one)
    if path == "/standings":
        return ([{"league": {"id": 39, "standings": [[{"rank": 1, "team": {"id": 42}, "points": 30},
                                                      {"rank": 2, "team": {"id": 47}, "points": 28}]]}}], one)
    if path == "/players":
        return ([{"player": {"id": 908}}, {"player": {"id": 276}}], {"current": 1, "total": 3})
    return ([{"id": 1, "name": "catalog row"}], one)


class FakeTransport:
    """Each `get` is ONE provider request. `plan[i]` (by request index) or `by_path[path]` can
    make a request fail like the real transport: an HTTP status, declared errors, a body above
    the read limit, or a reply without quota headers."""

    def __init__(self, *, day_remaining: int = 1000, by_path: dict[str, list[str]] | None = None,
                 huge_odds: bool = False, other_consumer: Callable[[int], int] | None = None):
        self.day = day_remaining
        self.requests: list[tuple[str, dict]] = []
        self.by_path = {k: list(v) for k, v in (by_path or {}).items()}
        self.huge_odds = huge_odds
        self.other_consumer = other_consumer

    async def get(self, path, params=None):
        params = dict(params or {})
        self.requests.append((path, params))
        self.day -= 1  # the provider counts every request that reaches it
        if self.other_consumer:
            self.day -= self.other_consumer(len(self.requests))
        queue = self.by_path.get(path) or []
        behaviour = queue.pop(0) if queue else "ok"
        if behaviour.startswith("http_"):
            raise ProviderHTTPError(int(behaviour[5:]))
        if behaviour == "timeout":
            raise ProviderUnavailable("PROVIDER_TIMEOUT")
        if behaviour == "declared_errors":
            raise ProviderDataError("PROVIDER_DECLARED_ERRORS")
        if behaviour == "too_large":
            raise ProviderDataError("PROVIDER_RESPONSE_TOO_LARGE")
        response, paging = response_for(path, params, huge_odds=self.huge_odds)
        results = len(response) if isinstance(response, list) else 1
        data = {"get": path.lstrip("/"), "parameters": {k: str(v) for k, v in params.items()}, "errors": [],
                "results": results, "paging": paging, "response": response}
        if behaviour == "echo_mismatch":
            data["parameters"] = {}
        if behaviour == "results_mismatch":
            data["results"] = results + 1
        day = None if behaviour == "no_headers" else max(self.day, 0)
        minute = 0 if behaviour == "minute_zero" else (None if behaviour == "no_headers" else 299)
        return ProviderReply(200, data, datetime.now(timezone.utc).isoformat(), 100,
                             quota_remaining_day=day, quota_remaining_minute=minute)


class FakeQuota:
    def __init__(self, error: BaseException | None = None):
        self.error = error
        self.leases = 0

    async def acquire(self, secret):
        if self.error:
            raise self.error
        self.leases += 1
        return QuotaLease(f"lease-{self.leases}", "fake-hash")

    async def release(self, lease):
        return None

    async def mark_exhausted(self, secret, kind):
        return None

    async def snapshot(self, secret):
        return {"remaining_day": 80, "remaining_minute": 8}


def build_client(transport: FakeTransport, quota: FakeQuota | None = None) -> ResilientFootballClient:
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True)
    return ResilientFootballClient(settings, transport, quota=quota or FakeQuota(),
                                   retry=RetryPolicy(base_delay=0.0, max_delay=0.0))


class _Server:
    def __init__(self):
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def tool_caller(transport: FakeTransport, quota: FakeQuota | None = None):
    """(call(tool, args) -> payload, tools): the real tools.py functions on one event loop."""
    from bridge.api_football.tools import register_tools
    server = _Server()
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True)
    client = build_client(transport, quota)
    register_tools(server, settings, client=client)
    loop = asyncio.new_event_loop()

    def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return loop.run_until_complete(server.tools[tool](**args))
    call.client = client  # type: ignore[attr-defined]
    return call, server.tools
