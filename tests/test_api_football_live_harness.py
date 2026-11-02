"""FCB-WP-003 — live API-FOOTBALL scripts reach the provider only through the coordinator.

RED on main fbd8cd2: the three live scripts opened ``httpx.AsyncClient`` directly with
the API key and never touched the shared PostgreSQL quota/concurrency coordinator or a
circuit breaker (CLAUDE.md §2.4).
"""
from __future__ import annotations

import ast
import asyncio
import dataclasses
from pathlib import Path

import httpx
import pytest

from bridge.api_football.circuit import CircuitBreaker
from bridge.api_football.config import Settings
from bridge.api_football.live_harness import (COORDINATOR_REQUIRED, CoordinatorRefused,
                                              coordinated_client)
from scripts.api_football_39_route_inventory import inventory
from scripts.api_football_capability_probe import run as probe_run
from scripts.api_football_live_smoke import MAX_CALLS, SAMPLE, run_live
from tests.apifootball_fake_coordinator import FakeCoordinator

ROOT = Path(__file__).resolve().parents[1]
LIVE_SCRIPTS = ("api_football_39_route_inventory.py", "api_football_capability_probe.py",
                "api_football_live_smoke.py")
KEY = "UNIT_TEST_KEY_NOT_REAL"
ENV = {"APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "true",
       "APIFOOTBALL_API_KEY": KEY, "APIFOOTBALL_MAX_DAILY_REQUESTS": "60",
       "APIFOOTBALL_MAX_RPM": "8", "APIFOOTBALL_MAX_CONCURRENCY": "1"}
OK_BODY = {"response": [], "results": 0, "errors": {}, "parameters": []}


async def _no_wait(_):
    return None


def _counting(status=200, headers=None, body=None):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json=body if body is not None else OK_BODY, headers=headers or {})
    return calls, httpx.MockTransport(handler)


# ---------------------------------------------------------------- static guard

def _provider_scripts():
    for path in sorted((ROOT / "scripts").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "OFFICIAL_BASE_URL" in text or "x-apisports-key" in text or "api-sports.io" in text:
            yield path, text


def test_every_provider_script_is_known_and_uses_the_harness():
    found = {p.name for p, _ in _provider_scripts()}
    assert found == set(LIVE_SCRIPTS), "a new script reaches API-FOOTBALL: route it through live_harness"
    for path, text in _provider_scripts():
        assert "coordinated_client" in text, path.name


@pytest.mark.parametrize("name", LIVE_SCRIPTS)
def test_live_scripts_never_build_their_own_http_client(name):
    """No direct httpx client/request in a live script: the harness is the only door."""
    tree = ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
    banned = {"AsyncClient", "Client", "get", "post", "request", "stream", "AsyncHTTPTransport"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "httpx" and node.attr in banned:
            offenders.append(f"httpx.{node.attr}:{node.lineno}")
        if isinstance(node, ast.ImportFrom) and node.module == "httpx":
            offenders.extend(f"from httpx import {a.name}:{node.lineno}" for a in node.names)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) else [])
            offenders.extend(f"import {m}:{node.lineno}" for m in mods
                             if m and m.split(".")[0] in {"requests", "aiohttp", "urllib3"})
    assert offenders == []


def test_only_trusted_modules_build_a_provider_http_client():
    """Inside the bridge only the runtime wiring and the live harness construct the client."""
    owners = set()
    for path in (ROOT / "bridge" / "api_football").glob("*.py"):
        if "httpx.AsyncClient(" in path.read_text(encoding="utf-8"):
            owners.add(path.name)
    assert owners == {"wiring.py", "live_harness.py"}


# ---------------------------------------------------------------- fail closed

@pytest.mark.parametrize("runner", [
    lambda env, t: run_live(env, transport=t),
    lambda env, t: inventory(env, transport=t, sleep=_no_wait),
    lambda env, t: probe_run(env, transport=t, sleep=_no_wait),
])
def test_scripts_refuse_without_coordinator_database(runner, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    calls, transport = _counting()
    with pytest.raises(ValueError, match=COORDINATOR_REQUIRED):
        asyncio.run(runner(dict(ENV), transport))
    assert calls == []  # nothing reached the provider


def test_blank_database_url_is_not_a_coordinator():
    calls, transport = _counting()
    with pytest.raises(ValueError, match=COORDINATOR_REQUIRED):
        asyncio.run(run_live({**ENV, "DATABASE_URL": "  "}, transport=transport))
    assert calls == []


def test_harness_rejects_untrusted_origin():
    settings = dataclasses.replace(Settings.from_env(ENV), base_url="https://example.invalid")
    calls, transport = _counting()

    async def go():
        async with coordinated_client(settings, ENV, transport=transport, quota=FakeCoordinator()):
            pass
    with pytest.raises(ValueError, match="UNTRUSTED_API_ORIGIN"):
        asyncio.run(go())
    assert calls == []


# ---------------------------------------------------------------- lease accounting

def test_each_request_takes_and_returns_exactly_one_lease():
    coord = FakeCoordinator()
    calls, transport = _counting(headers={"x-ratelimit-requests-remaining": "70", "x-ratelimit-remaining": "5"})
    result = asyncio.run(run_live(ENV, transport=transport, coordinator=coord))
    assert result["complete"]
    assert len(calls) == MAX_CALLS == len(SAMPLE)
    assert coord.used == len(calls)          # one quota unit per real attempt
    assert len(coord.released) == coord.used  # every lease released
    assert coord.active == set()
    assert coord.prepared >= 1
    assert coord.keys == {KEY}


def test_coordinator_budget_stops_the_script_before_sending():
    coord = FakeCoordinator(budget=2)
    calls, transport = _counting()
    result = asyncio.run(run_live(ENV, transport=transport, coordinator=coord))
    assert len(calls) == 2
    assert result["results"][-1] == {"path": SAMPLE[2], "outcome": "BLOCKED_COORDINATOR", "code": "QUOTA_EXCEEDED"}
    assert result["requests_attempted"] == 2  # the refused request was never sent
    assert not result["complete"]


def test_inventory_records_coordinator_refusal_and_stops():
    coord = FakeCoordinator(budget=1)
    calls, transport = _counting(body={"response": [{"league": {"id": 39}}], "results": 1, "errors": {}})
    report = asyncio.run(inventory(ENV | {"APIFOOTBALL_MAX_DAILY_REQUESTS": "39"}, transport=transport,
                                   sleep=_no_wait, coordinator=coord))
    assert len(calls) == 1
    blocked = [r for r in report["records"] if r.get("outcome") == "BLOCKED_COORDINATOR"]
    assert blocked and blocked[0]["coordinator_code"] == "QUOTA_EXCEEDED"
    assert report["coordinator_leases"] == 1


def test_probe_records_coordinator_refusal_and_stops():
    coord = FakeCoordinator(budget=1)
    calls, transport = _counting()
    evidence = asyncio.run(probe_run(ENV, transport=transport, sleep=_no_wait, coordinator=coord))
    assert len(calls) == 1
    assert any(r.get("status") == "BLOCKED_COORDINATOR" and r.get("code") == "QUOTA_EXCEEDED" for r in evidence)
    assert [r["coordinator_leases"] for r in evidence if "coordinator_leases" in r] == [1]


@pytest.mark.parametrize("header,kind", [("x-ratelimit-requests-remaining", "day"),
                                         ("x-ratelimit-remaining", "minute")])
def test_provider_zero_remaining_is_written_back_to_the_coordinator(header, kind):
    coord = FakeCoordinator()
    calls, transport = _counting(headers={header: "0"})
    asyncio.run(run_live(ENV, transport=transport, coordinator=coord))
    assert coord.exhausted == [kind]
    assert len(calls) == 1


def test_lease_released_when_transport_fails():
    coord = FakeCoordinator()

    def handler(request):
        raise httpx.ConnectError("SENSITIVE_DETAIL", request=request)
    result = asyncio.run(run_live(ENV, transport=httpx.MockTransport(handler), coordinator=coord))
    assert result["results"][0]["outcome"] == "TRANSPORT_ERROR"
    assert coord.used == 1 and coord.active == set()
    assert "SENSITIVE_DETAIL" not in str(result)


class _BrokenCoordinator(FakeCoordinator):
    async def acquire(self, secret):
        raise RuntimeError("postgresql://user:SECRET_PASSWORD@db.internal/x")


def test_coordinator_failure_is_fixed_code_and_sends_nothing():
    calls, transport = _counting()
    result = asyncio.run(run_live(ENV, transport=transport, coordinator=_BrokenCoordinator()))
    assert calls == []
    assert result["results"] == [{"path": SAMPLE[0], "outcome": "BLOCKED_COORDINATOR",
                                  "code": "COORDINATOR_UNAVAILABLE"}]
    assert "SECRET_PASSWORD" not in str(result)


class _HangingCoordinator(FakeCoordinator):
    async def acquire(self, secret):
        await asyncio.sleep(3600)


def test_coordinator_timeout_fails_closed(monkeypatch):
    from bridge.api_football import live_harness
    monkeypatch.setattr(live_harness, "_LEASE_TIMEOUT_SECONDS", 0.05)
    calls, transport = _counting()
    result = asyncio.run(run_live(ENV, transport=transport, coordinator=_HangingCoordinator()))
    assert calls == []
    assert result["results"][0]["code"] == "COORDINATOR_UNAVAILABLE"


# ---------------------------------------------------------------- circuit breaker

def test_breaker_opens_after_provider_failures_and_refuses_without_sending():
    settings = Settings.from_env(ENV)
    coord = FakeCoordinator()
    calls, transport = _counting(status=503)

    async def go():
        async with coordinated_client(settings, ENV, transport=transport, quota=coord,
                                      breaker=CircuitBreaker(threshold=2, cooldown=30)) as client:
            for _ in range(2):
                assert (await client.get("/status")).status_code == 503
            with pytest.raises(CoordinatorRefused) as refused:
                await client.get("/status")
            return refused.value.code
    assert asyncio.run(go()) == "CIRCUIT_OPEN"
    assert len(calls) == 2
    assert coord.used == 2 and coord.active == set()  # refused call took no lease


def test_breaker_counts_429_and_transport_errors_as_failures():
    settings = Settings.from_env(ENV)
    seq = iter([429])

    def handler(request):
        code = next(seq, None)
        if code is None:
            raise httpx.ConnectError("x", request=request)
        return httpx.Response(code, json=OK_BODY)

    async def go():
        async with coordinated_client(settings, ENV, transport=httpx.MockTransport(handler),
                                      quota=FakeCoordinator(), breaker=CircuitBreaker(threshold=2, cooldown=30)) as c:
            await c.get("/status")
            with pytest.raises(httpx.ConnectError):
                await c.get("/status")
            with pytest.raises(CoordinatorRefused, match="CIRCUIT_OPEN"):
                await c.get("/status")
    asyncio.run(go())


def test_stream_is_get_only():
    settings = Settings.from_env(ENV)

    async def go():
        async with coordinated_client(settings, ENV, transport=_counting()[1], quota=FakeCoordinator()) as c:
            async with c.stream("POST", "/status"):
                pass
    with pytest.raises(ValueError, match="GET_ONLY"):
        asyncio.run(go())


WORKFLOWS = ("api-football-39-route-inventory.yml", "api-football-capability-inventory.yml",
             "api-football-live-smoke.yml")


@pytest.mark.parametrize("name", WORKFLOWS)
def test_live_workflows_require_the_coordinator_before_any_live_step(name):
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))
    assert set(on) == {"workflow_dispatch"}  # manual only
    (job,) = wf["jobs"].values()
    assert job["environment"] == "api-football-live"
    assert job["env"]["DATABASE_URL"] == "${{ secrets.APIFOOTBALL_COORDINATOR_DATABASE_URL }}"
    steps = job["steps"]
    pre = next(i for i, s in enumerate(steps) if "COORDINATOR_DATABASE_REQUIRED" in s.get("run", ""))
    live = next(i for i, s in enumerate(steps) if "scripts/api_football_" in s.get("run", "")
                and "evidence_log_transport" not in s.get("run", ""))
    assert pre < live
    assert "echo \"$DATABASE_URL" not in steps[pre]["run"] and "set -x" not in steps[pre]["run"]
