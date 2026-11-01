"""Real PostgreSQL: the SX Bet rate coordinator is shared, exact and fail-closed (governance §2.4, §5)."""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import threading
import time

import httpx
import pytest

from app import migrations
from bridge.sxbet import ratelimit as rl
from bridge.sxbet.client import SXBetError, SXBetReadClient, Settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires dedicated TEST_DATABASE_URL; no production credentials",
)


def _connect(**kwargs):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(os.environ["TEST_DATABASE_URL"], row_factory=dict_row, **kwargs)


def _fake_key() -> str:
    return "FAKE_SX_ITEST_KEY_" + secrets.token_hex(16)


def _cleanup(*patterns: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        for table in ("sxbet_rate_attempts", "sxbet_rate_cooldowns"):
            for pattern in patterns:
                cur.execute(f"DELETE FROM {table} WHERE bucket_key LIKE %s", (pattern,))
        conn.commit()


@pytest.fixture
def key():
    value = _fake_key()
    digest = hashlib.sha256(value.encode()).hexdigest()
    rl.PostgresRateCoordinator(connect=_connect)._ensure_schema()
    # testnet ip buckets are shared by design: isolate them for this test DB.
    _cleanup(f"%{digest}%", "sxbet:v1:testnet:ip:%")
    yield value
    _cleanup(f"%{digest}%", "sxbet:v1:testnet:ip:%")


def _client(key, coordinator, handler=None, *, rpm=60):
    calls = []

    def wrapped(request):
        calls.append(request)
        return (handler or (lambda r: httpx.Response(200, json={"status": "success", "data": []})))(request)

    settings = Settings(enabled=True, network="testnet", api_key=key, max_rpm=rpm, rate_coordinator="postgres")
    return SXBetReadClient(settings, transport=httpx.MockTransport(wrapped), limiter=coordinator), calls


async def _get(client, path, *, auth):
    try:
        await client.get(path, {}, auth=auth)
        return "OK", None
    except SXBetError as exc:
        return exc.code, exc.retry_after


def test_threads_on_independent_coordinators_never_exceed_the_shared_budget(key):
    plan = rl.build_plan(network="testnet", api_key=key, max_rpm=7, path="/user/privacy-v3", auth=True)
    coordinators = [rl.PostgresRateCoordinator(connect=_connect) for _ in range(4)]  # 4 "processes"
    admitted, refused, errors = [], [], []
    barrier = threading.Barrier(16)

    def worker(i):
        barrier.wait()
        try:
            asyncio.run(coordinators[i % 4].acquire(plan))
            admitted.append(i)
        except rl.RateLimited as exc:
            refused.append(exc.reason)
        except Exception as exc:  # pragma: no cover - must not happen
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(admitted) == 7 and len(refused) == 9 and set(refused) == {"WINDOW_FULL"}


def test_documented_per_route_limit_is_enforced_below_sxbet_max_rpm(key):
    client, calls = _client(key, rl.PostgresRateCoordinator(connect=_connect), rpm=600)

    async def scenario():
        results = [await _get(client, "/user/fees-v3", auth=True) for _ in range(121)]
        other = await _get(client, "/user/balance-v3", auth=True)
        return results, other

    results, other = asyncio.run(scenario())
    assert [r[0] for r in results[:120]] == ["OK"] * 120  # documented 120/60s
    assert results[120][0] == "SXBET_LOCAL_RATE_LIMITED" and 1 <= results[120][1] <= 61
    assert len(calls) == 121 and other == ("OK", None)  # 120 fees + 1 balance, never the refused one


def test_public_family_budget_is_shared_across_keys_and_instances(key):
    other_key = _fake_key()
    digest = hashlib.sha256(other_key.encode()).hexdigest()
    try:
        with _connect() as conn, conn.cursor() as cur:
            bucket = rl.build_plan(network="testnet", api_key=key, max_rpm=60, path="/sports",
                                   auth=False).cooldown_key
            assert bucket == "sxbet:v1:testnet:ip:route:markets_and_sports_data"
            # 499 earlier requests from "another instance" (documented family budget 500/60s).
            cur.execute("INSERT INTO sxbet_rate_attempts (attempt_id, bucket_key) "
                        "SELECT 'itest-' || g::text || '-' || %s, %s FROM generate_series(1, 499) g",
                        (secrets.token_hex(6), bucket))
            conn.commit()
        a, calls_a = _client(key, rl.PostgresRateCoordinator(connect=_connect))
        b, calls_b = _client(other_key, rl.PostgresRateCoordinator(connect=_connect))
        assert asyncio.run(_get(a, "/markets/active", auth=False)) == ("OK", None)
        code, _ = asyncio.run(_get(b, "/teams", auth=False))
        assert code == "SXBET_LOCAL_RATE_LIMITED" and len(calls_a) == 1 and calls_b == []
        assert asyncio.run(_get(b, "/metadata/obv3", auth=False)) == ("OK", None)  # its own bucket
    finally:
        _cleanup(f"%{digest}%")


def test_429_cooldown_is_shared_across_instances_without_network(key):
    a, calls_a = _client(key, rl.PostgresRateCoordinator(connect=_connect),
                         lambda r: httpx.Response(429, headers={"retry-after": "30"}))
    b, calls_b = _client(key, rl.PostgresRateCoordinator(connect=_connect))
    assert asyncio.run(_get(a, "/user/balance-v3", auth=True)) == ("SXBET_RATE_LIMITED", 30)
    code, retry = asyncio.run(_get(b, "/user/balance-v3", auth=True))
    assert code == "SXBET_LOCAL_RATE_LIMITED" and 28 <= retry <= 30 and calls_b == [] and len(calls_a) == 1
    assert asyncio.run(_get(b, "/user/fees-v3", auth=True)) == ("OK", None)  # other bucket unaffected
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT bucket_key, cooldown_until > clock_timestamp() + INTERVAL '25 seconds' AS active "
                    "FROM sxbet_rate_cooldowns WHERE bucket_key LIKE %s",
                    (f"%{hashlib.sha256(key.encode()).hexdigest()}%",))
        rows = cur.fetchall()
    assert [r["bucket_key"].rsplit(":", 1)[-1] for r in rows] == ["GET /user/balance-v3"] and rows[0]["active"]
    # A shorter later Retry-After never shortens the stored cooldown.
    a2, _ = _client(key, rl.PostgresRateCoordinator(connect=_connect))
    asyncio.run(a2._limiter.record_rate_limited(
        rl.build_plan(network="testnet", api_key=key, max_rpm=60, path="/user/balance-v3", auth=True), 1))
    assert asyncio.run(_get(b, "/user/balance-v3", auth=True))[1] >= 25


def test_coordinator_down_fails_closed_with_zero_network_calls(key):
    import psycopg

    def down():
        return psycopg.connect("postgresql://postgres:SECRETPW@127.0.0.1:1/none?connect_timeout=1")
    client, calls = _client(key, rl.PostgresRateCoordinator(connect=down))
    assert asyncio.run(_get(client, "/sports", auth=False)) == ("SXBET_RATE_COORDINATOR_UNAVAILABLE", None)
    assert calls == []


def test_held_rate_lock_times_out_fail_closed(key):
    import bridge.sxbet.client as sx_client
    holder = _connect()
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (rl._RATE_LOCK_ID,))
        client, calls = _client(key, rl.PostgresRateCoordinator(connect=_connect))
        started = time.monotonic()
        assert asyncio.run(_get(client, "/sports", auth=False))[0] == "SXBET_RATE_COORDINATOR_UNAVAILABLE"
        assert time.monotonic() - started < sx_client._COORDINATOR_TIMEOUT_SECONDS + 1.5 and calls == []
    finally:
        holder.rollback()
        holder.close()
    with _connect() as conn, conn.cursor() as cur:  # the timed-out reservation was rolled back
        cur.execute("SELECT count(*) AS n FROM sxbet_rate_attempts WHERE bucket_key LIKE %s",
                    (f"%{hashlib.sha256(key.encode()).hexdigest()}%",))
        assert cur.fetchone()["n"] == 0


def test_api_key_is_never_stored_in_plaintext(key):
    client, _ = _client(key, rl.PostgresRateCoordinator(connect=_connect),
                        lambda r: httpx.Response(429, headers={"retry-after": "2"}))
    asyncio.run(_get(client, "/user/balance-v3", auth=True))
    client2, _ = _client(key, rl.PostgresRateCoordinator(connect=_connect))
    asyncio.run(_get(client2, "/sports", auth=False))
    digest = hashlib.sha256(key.encode()).hexdigest()
    with _connect() as conn, conn.cursor() as cur:
        for table in ("sxbet_rate_attempts", "sxbet_rate_cooldowns"):
            cur.execute(f"SELECT count(*) AS n FROM {table} t WHERE t::text LIKE %s", (f"%{key}%",))
            assert cur.fetchone()["n"] == 0
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE bucket_key LIKE %s", (f"%{digest}%",))
            assert cur.fetchone()["n"] >= 1


def test_schema_ensure_and_migration_0007_on_already_initialized_db(key):
    # Migrations 0001..0007 applied (twice: idempotent tracking), then a fresh process ensures the schema.
    migrations.apply_migrations(_connect)
    migrations.apply_migrations(_connect)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations WHERE version=7")
        assert cur.fetchone()["name"] == "0007_sxbet_rate_limits.sql"
        cur.execute("INSERT INTO sxbet_rate_attempts (attempt_id, bucket_key) VALUES (%s, %s)",
                    ("itest-keep-" + secrets.token_hex(4), f"sxbet:v1:testnet:key:{hashlib.sha256(key.encode()).hexdigest()}:global"))
        conn.commit()
    fresh = rl.PostgresRateCoordinator(connect=_connect)  # _schema_ready False, tables exist
    client, _ = _client(key, fresh)
    assert asyncio.run(_get(client, "/user/privacy-v3", auth=True)) == ("OK", None)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM sxbet_rate_attempts WHERE attempt_id LIKE 'itest-keep-%'")
        assert cur.fetchone()["n"] == 1  # existing rows preserved


def test_runtime_first_then_migration_0007_in_isolated_schema():
    schema = "sx_itest_" + secrets.token_hex(4)
    with _connect(autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    try:
        connect = lambda: _connect(options=f"-c search_path={schema}")
        rl.PostgresRateCoordinator(connect=connect)._ensure_schema()  # runtime first
        m7 = [m for m in migrations.load_migrations() if m.version == 7][0]
        with connect() as conn, conn.cursor() as cur:  # then the versioned migration on the same schema
            cur.execute(migrations.TRACKING_SQL)
            cur.execute(migrations.migration_block(m7))
            cur.execute(migrations.migration_block(m7))  # re-run: tracked, checksum verified
            conn.commit()
            cur.execute("SELECT count(*) AS n FROM schema_migrations WHERE version=7")
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname=%s AND tablename LIKE 'sxbet_rate_%%'",
                        (schema,))
            assert cur.fetchone()["n"] == 2
            with pytest.raises(Exception):  # format check: no free-form (e.g. plaintext key) bucket keys
                cur.execute("INSERT INTO sxbet_rate_attempts (attempt_id, bucket_key) VALUES ('x', 'RAW_KEY')")
    finally:
        with _connect(autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")  # throwaway test schema only


def test_fresh_process_with_slow_connections_fits_the_budgets(key):
    def slow():
        time.sleep(0.6)  # ~Neon/Render per-connection latency
        return _connect()
    client, calls = _client(key, rl.PostgresRateCoordinator(connect=slow))
    assert asyncio.run(_get(client, "/user/privacy-v3", auth=True)) == ("OK", None) and len(calls) == 1
