"""SX Bet rate admission (governance §2.4): documented buckets, cooldowns, fail-closed coordinator.

Offline: fake clock, fake coordinators and a fake psycopg connection; no network, no database.
The real PostgreSQL behaviour is in tests/integration/test_sxbet_rate_coordinator_postgres.py.
"""
import asyncio
import json
import logging
import re
from pathlib import Path

import httpx
import pytest

from bridge.sxbet import client as sx_client
from bridge.sxbet import ratelimit as rl
from bridge.sxbet.client import NO_NETWORK_CODES, SXBetReadClient, Settings, default_limiter
from bridge.sxbet.tools import register_sxbet_tools

KEY = "FAKE_SX_RATE_TEST_KEY_DO_NOT_STORE"
H = "0x" + "ab" * 32
ADDR = "0x" + "12" * 20
MIGRATION = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "0007_sxbet_rate_limits.sql"


class _Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def attach(fn):
            self.tools[fn.__name__] = fn
            return fn
        return attach


def _tools(limiter, handler=None, *, rpm=60, key=KEY):
    calls = []

    def wrapped(request):
        calls.append(request)
        return (handler or (lambda r: httpx.Response(200, json={"status": "success", "data": []})))(request)

    settings = Settings(enabled=True, api_key=key, max_rpm=rpm)
    client = SXBetReadClient(settings, transport=httpx.MockTransport(wrapped), limiter=limiter)
    registry = _Registry()
    register_sxbet_tools(registry, settings, client=client)
    return registry.tools, calls


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------------- plan: documented limits, bucket identity ----------------

DOC_TABLE = {  # https://docs.sx.bet/developers/rate-limits (GET rows), fetched 2026-11-01
    "/orders-v3": (1200, 60, "key"), "/orders-v3/" + H: (1200, 60, "key"),
    "/orders-v3/client/abc_1": (1200, 60, "key"),
    "/orderbook-v3/snapshot/market": (27500, 300, "key"),
    "/fills-v3": (300, 60, "key"), "/positions-v3": (300, 60, "key"), "/trades-v3": (300, 60, "key"),
    "/trades-v3/t-1": (300, 60, "key"), "/positions-v3/settled/pnl": (120, 60, "key"),
    "/user/balance-v3": (300, 60, "key"), "/user/fees-v3": (120, 60, "key"),
    "/user/realtime-token-v3/api-key": (30, 60, "key"),
    "/user/proxy": (120, 60, "key"), "/user/pending-deploy-proxy": (120, 60, "key"),
    "/user/transfer-to-proxy/pending": (120, 60, "key"), "/user/transfer-to-proxy/status": (120, 60, "key"),
    f"/user/multisig/proxy/{ADDR}": (60, 60, "key"), f"/user/multisig/{ADDR}/info": (60, 60, "key"),
    "/orders-v3/odds/best": (5500, 60, "ip"), "/metadata/obv3": (500, 60, "ip"),
    "/summary-v3/x": (500, 60, "ip"),
    "/trades-v3/public": (200, 60, "ip"), "/trades-v3/public/count": (200, 60, "ip"),
}
SHARED_FAMILY = ["/markets/active", "/markets/find", "/markets/popular", "/fixture/active", "/fixture/status",
                 "/leagues", "/leagues/active", "/sports", "/teams", "/live-scores", "/search"]
UNDOCUMENTED = ["/orders-v3/public", "/orders-v3/public/user", "/positions-v3/public", "/ledger-v3",
                "/user/privacy-v3", "/orderbook-v3/snapshot/event", "/unknown-route"]


@pytest.mark.parametrize("path,expected", sorted(DOC_TABLE.items()))
def test_documented_routes_use_documented_limits(path, expected):
    route = rl.documented_route(path)
    assert route is not None and route[1:] == expected
    plan = rl.build_plan(network="mainnet", api_key=KEY, max_rpm=60, path=path, auth=True)
    route_bucket = [b for b in plan.buckets if b.kind == "route"]
    assert len(route_bucket) == 1 and (route_bucket[0].limit, route_bucket[0].window_seconds) == expected[:2]
    assert plan.cooldown_key == route_bucket[0].key
    assert (":ip:route:" in route_bucket[0].key) == (expected[2] == "ip")


def test_markets_and_sports_family_shares_one_bucket_across_routes_and_keys():
    keys = {b.key for p in SHARED_FAMILY for k in (KEY, "OTHER_KEY")
            for b in rl.build_plan(network="mainnet", api_key=k, max_rpm=60, path=p, auth=False).buckets
            if b.kind == "route"}
    assert len(keys) == 1 and next(iter(keys)).endswith(":ip:route:markets_and_sports_data")
    assert rl.documented_route("/markets/active")[1:3] == (500, 60)


@pytest.mark.parametrize("path", UNDOCUMENTED)
def test_undocumented_routes_use_only_the_global_budget(path):
    plan = rl.build_plan(network="mainnet", api_key=KEY, max_rpm=60, path=path, auth=True)
    assert [b.kind for b in plan.buckets] == ["global"] and plan.cooldown_key == plan.buckets[0].key


def test_unauthenticated_requests_also_draw_from_the_documented_edge_bucket():
    plan = rl.build_plan(network="mainnet", api_key=KEY, max_rpm=60, path="/sports", auth=False)
    edge = [b for b in plan.buckets if b.kind == "edge"]
    assert len(edge) == 1 and (edge[0].limit, edge[0].window_seconds) == (35000, 600)
    assert not [b for b in rl.build_plan(network="mainnet", api_key=KEY, max_rpm=60, path="/sports",
                                         auth=True).buckets if b.kind == "edge"]


@pytest.mark.parametrize("rpm", [1, 60, 600])
def test_global_bucket_is_sxbet_max_rpm_and_bounds_are_kept(rpm):
    plan = rl.build_plan(network="mainnet", api_key=KEY, max_rpm=rpm, path="/sports", auth=False)
    assert (plan.buckets[0].kind, plan.buckets[0].limit, plan.buckets[0].window_seconds) == ("global", rpm, 60)
    for bad in (0, 601):
        with pytest.raises(ValueError):
            rl.build_plan(network="mainnet", api_key=KEY, max_rpm=bad, path="/sports", auth=False)
    with pytest.raises(ValueError):
        Settings.from_env({"SXBET_MAX_RPM": "601"})


def test_bucket_keys_never_contain_the_api_key_only_its_sha256():
    import hashlib
    digest = hashlib.sha256(KEY.encode()).hexdigest()
    for path in list(DOC_TABLE) + SHARED_FAMILY + UNDOCUMENTED:
        for auth in (True, False):
            plan = rl.build_plan(network="testnet", api_key=KEY, max_rpm=60, path=path, auth=auth)
            for b in plan.buckets:
                assert KEY not in b.key and b.key.startswith("sxbet:v1:testnet:") and len(b.key) <= 256
            assert plan.buckets[0].key == f"sxbet:v1:testnet:key:{digest}:global"
    assert rl.build_plan(network="mainnet", api_key="", max_rpm=60, path="/sports",
                         auth=False).buckets[0].key == "sxbet:v1:mainnet:key:none:global"


# ---------------- local limiter semantics (same as the shared coordinator) ----------------

def test_effective_limit_is_min_of_local_and_documented():
    clock = Clock()
    limiter = rl.LocalRateLimiter(600, clock=clock)
    tools, calls = _tools(limiter, rpm=600)
    for _ in range(120):  # documented /user/fees-v3 = 120 per 60 s, lower than SXBET_MAX_RPM=600
        assert run(tools["sxbet_fees"]())["status"] == "REAL_API_RESPONSE"
    out = run(tools["sxbet_fees"]())
    assert out["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED" and out["network_calls_made"] == 0
    assert 1 <= out["error"]["retry_after_seconds"] <= 61 and len(calls) == 120
    # A different documented route still has capacity (global 600 not exhausted).
    assert run(tools["sxbet_balance"]())["status"] == "REAL_API_RESPONSE"
    clock.t += 61
    assert run(tools["sxbet_fees"]())["status"] == "REAL_API_RESPONSE"


def test_local_global_budget_is_lower_than_documented_route():
    clock = Clock()
    tools, calls = _tools(rl.LocalRateLimiter(3, clock=clock), rpm=3)
    for _ in range(3):
        assert run(tools["sxbet_balance"]())["status"] == "REAL_API_RESPONSE"
    assert run(tools["sxbet_sports"]())["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED"
    assert len(calls) == 3


def test_shared_public_family_budget_is_drawn_by_any_member():
    clock = Clock()
    limiter = rl.LocalRateLimiter(600, clock=clock)
    tools, calls = _tools(limiter, rpm=600)
    for i in range(500):
        name = ("sxbet_sports", "sxbet_markets_popular")[i % 2]
        assert run(tools[name]())["status"] == "REAL_API_RESPONSE"
    clock.t += 30  # global window (60 s) still has 500 entries, global limit 600 not reached
    out = run(tools["sxbet_search"](query="milan"))
    assert out["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED" and len(calls) == 500
    assert run(tools["sxbet_metadata"]())["status"] == "REAL_API_RESPONSE"  # separate documented bucket


def test_429_cooldown_is_shared_and_blocks_without_network():
    clock = Clock()
    shared = rl.LocalRateLimiter(60, clock=clock)  # same store, two clients ("instances")
    state = {"status": 429}
    handler = lambda r: (httpx.Response(429, headers={"retry-after": "7"}) if state["status"] == 429
                         else httpx.Response(200, json={"status": "success", "data": []}))
    a, calls_a = _tools(shared, handler)
    b, calls_b = _tools(shared, handler)
    out = run(a["sxbet_balance"]())
    assert out["error"]["code"] == "SXBET_RATE_LIMITED" and out["error"]["retry_after_seconds"] == 7
    state["status"] = 200
    out = run(b["sxbet_balance"]())
    assert out["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED" and out["network_calls_made"] == 0
    assert 1 <= out["error"]["retry_after_seconds"] <= 7 and calls_b == [] and len(calls_a) == 1
    # Only the throttled bucket cools down.
    assert run(b["sxbet_fees"]())["status"] == "REAL_API_RESPONSE"
    clock.t += 7.5
    assert run(b["sxbet_balance"]())["status"] == "REAL_API_RESPONSE"


def test_429_without_retry_after_uses_default_and_huge_values_are_capped():
    assert rl.cooldown_seconds(None) == rl.DEFAULT_COOLDOWN_SECONDS == 60
    assert rl.cooldown_seconds(0) == 60 and rl.cooldown_seconds(10**8) == rl.MAX_COOLDOWN_SECONDS == 3600
    clock = Clock()
    tools, calls = _tools(rl.LocalRateLimiter(60, clock=clock), lambda r: httpx.Response(429))
    run(tools["sxbet_sports"]())
    clock.t += 59
    assert run(tools["sxbet_sports"]())["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED" and len(calls) == 1


def test_undocumented_route_429_cools_down_the_global_budget():
    clock = Clock()
    tools, calls = _tools(rl.LocalRateLimiter(60, clock=clock), lambda r: httpx.Response(429, headers={"retry-after": "5"}))
    run(tools["sxbet_privacy"]())
    assert run(tools["sxbet_sports"]())["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED" and len(calls) == 1


def test_failed_calls_still_consume_their_slot():
    clock = Clock()
    tools, calls = _tools(rl.LocalRateLimiter(2, clock=clock), lambda r: httpx.Response(500), rpm=2)
    run(tools["sxbet_sports"]())
    run(tools["sxbet_sports"]())
    assert run(tools["sxbet_sports"]())["error"]["code"] == "SXBET_LOCAL_RATE_LIMITED" and len(calls) == 2


# ---------------- fail closed ----------------

class Broken:
    kind = "POSTGRES_SHARED"

    def __init__(self, *, fail_prepare=False, slow=0.0, error=RuntimeError("postgresql://u:pw@db.example/x " + KEY)):
        self.fail_prepare, self.slow, self.error = fail_prepare, slow, error
        self.recorded = []

    async def prepare(self):
        if self.fail_prepare:
            raise self.error

    async def acquire(self, plan):
        if self.slow:
            await asyncio.sleep(self.slow)
            return
        raise self.error

    async def record_rate_limited(self, plan, retry_after):
        self.recorded.append(retry_after)


@pytest.mark.parametrize("limiter", [Broken(), Broken(fail_prepare=True), Broken(error=OSError("boom")),
                                     Broken(slow=5.0)])
def test_coordinator_failure_fails_closed_with_zero_network_calls(limiter, monkeypatch, caplog):
    monkeypatch.setattr(sx_client, "_COORDINATOR_TIMEOUT_SECONDS", 0.05)
    tools, calls = _tools(limiter)
    with caplog.at_level(logging.WARNING):
        for name in ("sxbet_sports", "sxbet_balance"):
            out = run(tools[name]())
            assert out["status"] == "PROVIDER_ERROR" and out["network_calls_made"] == 0
            assert out["error"] == {"code": "SXBET_RATE_COORDINATOR_UNAVAILABLE"}
    assert calls == []
    text = json.dumps(out) + caplog.text
    assert KEY not in text and "pw@" not in text and "db.example" not in text
    assert "SXBET_RATE_COORDINATOR_UNAVAILABLE" in caplog.text


def test_slow_prepare_is_bounded(monkeypatch):
    monkeypatch.setattr(sx_client, "_COORDINATOR_PREPARE_SECONDS", 0.05)

    class SlowPrepare(Broken):
        async def prepare(self):
            await asyncio.sleep(5)
    tools, calls = _tools(SlowPrepare())
    assert run(tools["sxbet_sports"]())["error"]["code"] == "SXBET_RATE_COORDINATOR_UNAVAILABLE" and calls == []


def test_simulation_counts_no_network_call_when_coordinator_is_down():
    tools, calls = _tools(Broken())
    out = run(tools["sxbet_simulate_order"](market_hash=H, betting_outcome_one=True, stake_base_units=1_000_000,
                                            percentage_odds="50000000000000000000", time_in_force="GTC",
                                            expiry=2_000_000_000, idempotency_key="rate-test-1"))
    assert out["error"]["code"] == "SXBET_RATE_COORDINATOR_UNAVAILABLE" and out["network_calls_made"] == 0
    assert calls == [] and "SXBET_RATE_COORDINATOR_UNAVAILABLE" in NO_NETWORK_CODES


def test_unreachable_postgres_fails_closed_without_fallback_or_network():
    import psycopg
    dsn = "postgresql://postgres:SECRETPW@127.0.0.1:1/none?connect_timeout=1"
    coordinator = rl.PostgresRateCoordinator(connect=lambda: psycopg.connect(dsn))
    tools, calls = _tools(coordinator)
    out = run(tools["sxbet_sports"]())
    assert out["error"] == {"code": "SXBET_RATE_COORDINATOR_UNAVAILABLE"} and out["network_calls_made"] == 0
    assert calls == [] and "SECRETPW" not in json.dumps(out)


def test_429_cooldown_store_failure_still_returns_the_provider_429():
    class CooldownDown(rl.LocalRateLimiter):
        async def record_rate_limited(self, plan, retry_after):
            raise RuntimeError("down")
    tools, calls = _tools(CooldownDown(60), lambda r: httpx.Response(429, headers={"retry-after": "3"}))
    out = run(tools["sxbet_sports"]())
    assert out["error"]["code"] == "SXBET_RATE_LIMITED" and out["network_calls_made"] == 1 and len(calls) == 1


# ---------------- selection ----------------

def test_selection_postgres_when_database_url_set_local_otherwise():
    assert Settings.from_env({}).rate_coordinator == "local"
    assert Settings.from_env({"DATABASE_URL": "  "}).rate_coordinator == "local"
    pg = Settings.from_env({"DATABASE_URL": "postgresql://example.invalid/db"})
    assert pg.rate_coordinator == "postgres" and "example.invalid" not in repr(pg)
    assert isinstance(default_limiter(pg), rl.PostgresRateCoordinator)  # constructed lazily: no connection
    assert isinstance(default_limiter(Settings()), rl.LocalRateLimiter)
    with pytest.raises(ValueError):
        default_limiter(Settings(rate_coordinator="none"))
    registry = _Registry()
    register_sxbet_tools(registry, Settings(enabled=True, api_key=KEY, rate_coordinator="postgres"))
    assert run(registry.tools["sxbet_health"]())["rate_coordinator"] == "POSTGRES_SHARED"


# ---------------- PostgreSQL coordinator SQL shape (fake connection) ----------------

class FakeConn:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        self.log.append(("BEGIN",))
        return self

    def __exit__(self, typ, exc, tb):
        self.log.append(("ROLLBACK",) if typ else ("CLOSE",))
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0)

    def commit(self):
        self.log.append(("COMMIT",))


def test_postgres_reservation_is_serialized_and_stores_only_hashes():
    log = []
    rows = [{"wait": None}, {"n": 0, "wait": None}, {"n": 3, "wait": None}, {"n": 0, "wait": None}]
    coordinator = rl.PostgresRateCoordinator(connect=lambda: FakeConn(log, rows))
    plan = rl.build_plan(network="mainnet", api_key=KEY, max_rpm=60, path="/sports", auth=False)
    run(coordinator.acquire(plan))
    statements = [entry[0] for entry in log]
    # Schema DDL is its own transaction, under its own lock, before the reservation lock.
    first_commit = statements.index("COMMIT")
    assert any("CREATE TABLE IF NOT EXISTS sxbet_rate_attempts" in s for s in statements[:first_commit])
    assert not any("pg_advisory_xact_lock" in s and entry[1] == (rl._RATE_LOCK_ID,)
                   for s, entry in zip(statements[:first_commit], log[:first_commit]))
    reservation = log[first_commit + 1:]
    lock_at = next(i for i, e in enumerate(reservation) if len(e) > 1 and e[1] == (rl._RATE_LOCK_ID,))
    first_read = next(i for i, e in enumerate(reservation) if e[0].startswith(("SELECT", "DELETE", "INSERT"))
                      and "advisory" not in e[0])
    assert lock_at < first_read
    inserts = [e for e in reservation if e[0].startswith("INSERT INTO sxbet_rate_attempts")]
    assert len(inserts) == len(plan.buckets) == 3
    assert KEY not in json.dumps([e[1] for e in log if len(e) > 1], default=str)
    assert reservation[-2:] == [("COMMIT",), ("CLOSE",)]


def test_postgres_full_window_or_cooldown_rolls_back_without_insert():
    for rows, reason in (([{"wait": 4}], "COOLDOWN"), ([{"wait": None}, {"n": 60, "wait": 12}], "WINDOW_FULL")):
        log = []
        coordinator = rl.PostgresRateCoordinator(connect=lambda: FakeConn(log, list(rows)))
        coordinator._schema_ready = True
        plan = rl.build_plan(network="mainnet", api_key=KEY, max_rpm=60, path="/user/balance-v3", auth=True)
        with pytest.raises(rl.RateLimited) as caught:
            run(coordinator.acquire(plan))
        assert caught.value.reason == reason and caught.value.retry_after in (4, 12)
        assert not [e for e in log if e[0].startswith("INSERT")] and log[-1] == ("ROLLBACK",)
        assert ("COMMIT",) not in log


# ---------------- schema: migration is the source of truth ----------------

def _statements(sql):
    body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    return [" ".join(s.split()) for s in body.split(";") if s.strip()]


def test_runtime_schema_matches_migration_0007_and_is_additive():
    sql = MIGRATION.read_text()
    assert _statements(sql) == [" ".join(s.split()) for s in rl._SCHEMA]
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|ALTER|UPDATE|RENAME)\b", sql, re.I)
    assert all("IF NOT EXISTS" in s for s in _statements(sql))
