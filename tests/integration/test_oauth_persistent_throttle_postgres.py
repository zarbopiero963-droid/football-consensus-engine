"""OAuth persistent throttle on REAL PostgreSQL (CI: decision-postgres PG16 and postgres-18 PG18).

Each test gets its own schema with the bridge migrations applied. Covers: GCRA limits, restart
persistence (consent and registration), threads with a Barrier, processes with spawn, expiry,
app clock independence, DB unavailable / lock timeout, IPv4 and IPv6 /64, shared egress, many-key
flood (no rows for denied keys, bounded cardinality, bounded connections), bounded GC with
SKIP LOCKED, rotation, refund, no plain address in the table, runtime grants, migration 0009 on an
already-initialised database, readiness while OFF / SHADOW, and /oauth/token + /mcp continuity.
The HMAC secret is generated per test (CSPRNG), never a committed literal.
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402
from psycopg.sql import SQL, Identifier  # noqa: E402

from app import migrations  # noqa: E402
from bridge import oauth, oauth_persistent_throttle as pt, oauth_store, oauth_throttle  # noqa: E402
from bridge.schema_readiness import evaluate  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                                reason="requires dedicated TEST_DATABASE_URL; no production credentials")

ROOT = migrations.ROOT_MIGRATIONS.parents[1]
CLAUDE_REDIRECT = oauth.CLAUDE_CALLBACK
PASSWORD = "LOCAL_TEST_ONLY_PASSWORD_" + "x" * 8


def _connect_factory(dsn, schema, **extra):
    opts = f"-c search_path={schema} -c statement_timeout={pt.STATEMENT_TIMEOUT_MS} -c lock_timeout={pt.LOCK_TIMEOUT_MS}"

    def connect():
        return psycopg.connect(dsn, options=opts, connect_timeout=pt.CONNECT_TIMEOUT_SECONDS, **extra)
    return connect


@pytest.fixture
def db(monkeypatch):
    dsn = os.environ["TEST_DATABASE_URL"]
    schema = "thr_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
    connect = _connect_factory(dsn, schema)

    def connect_dict():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={schema}")

    migrations.apply_migrations(connect_dict)
    monkeypatch.setattr(pt, "_conn", connect)
    monkeypatch.setattr(oauth_store, "_conn", connect_dict)
    yield {"dsn": dsn, "schema": schema, "connect": connect, "dict": connect_dict}
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


@pytest.fixture
def key(monkeypatch):
    value = "t1:" + secrets.token_urlsafe(48)
    monkeypatch.setenv(pt.KEY_ENV, value)
    monkeypatch.delenv(pt.PREVIOUS_KEY_ENV, raising=False)
    return value


# Generated at runtime by a CSPRNG for this test process; never a committed literal.
_SECRET = secrets.token_bytes(32)


def _k(ip, secret=None):
    return pt.key_hash("t1", secret or _SECRET, ip)


def _rows(db, where="TRUE"):
    with db["dict"]() as conn:
        return conn.execute(f"SELECT scope, key_hash, tat, updated_at FROM oauth_throttle_buckets WHERE {where} "
                            "ORDER BY scope, key_hash").fetchall()


def _age(db, seconds):
    """Simulated passage of time for the GCRA state: shift every tat into the past."""
    with db["dict"]() as conn:
        conn.execute("UPDATE oauth_throttle_buckets SET tat = tat - make_interval(secs => %s)", (seconds,))


# ------------------------------------------------------------------ GCRA

def test_consent_limit_per_key_and_other_keys_unaffected(db):
    a = _k("203.0.113.1")
    results = [pt.acquire(pt.CONSENT, a) for _ in range(6)]
    assert [ok for ok, _ in results] == [True] * 5 + [False]
    assert 170 <= results[5][1] <= 181                         # 15 min / 5 = 180 s per event
    assert pt.acquire(pt.CONSENT, _k("203.0.113.2"))[0] is True


def test_global_limit_bounds_rotating_keys_and_denied_keys_leave_no_row(db):
    allowed = [pt.acquire(pt.CONSENT, _k(f"198.51.100.{i}"))[0] for i in range(60)]
    assert allowed == [True] * 50 + [False] * 10
    rows = _rows(db, "scope = 'consent'")
    assert len(rows) == 1 + 50                                  # the global row + conforming keys only
    for i in range(50, 60):
        assert not _rows(db, f"key_hash = '{_k(f'198.51.100.{i}')}'")


def test_registration_limits(db):
    a = _k("203.0.113.3")
    assert [pt.acquire(pt.REGISTRATION, a)[0] for _ in range(11)] == [True] * 10 + [False]
    # Scopes are independent.
    assert pt.acquire(pt.CONSENT, a)[0] is True


def test_expiry_and_partial_recovery(db):
    a = _k("203.0.113.4")
    for _ in range(5):
        pt.acquire(pt.CONSENT, a)
    assert pt.acquire(pt.CONSENT, a)[0] is False
    _age(db, 181)                                               # one emission interval later
    assert pt.acquire(pt.CONSENT, a)[0] is True and pt.acquire(pt.CONSENT, a)[0] is False
    _age(db, 901)                                               # whole window
    assert [pt.acquire(pt.CONSENT, a)[0] for _ in range(6)] == [True] * 5 + [False]


def test_refund_gives_back_one_event(db):
    a = _k("203.0.113.5")
    for _ in range(5):
        pt.acquire(pt.CONSENT, a)
    pt.refund(pt.CONSENT, a)
    assert pt.acquire(pt.CONSENT, a)[0] is True and pt.acquire(pt.CONSENT, a)[0] is False


def test_refund_never_moves_tat_below_now(db):
    a = _k("203.0.113.6")
    pt.acquire(pt.CONSENT, a)
    for _ in range(10):
        pt.refund(pt.CONSENT, a)
    assert [pt.acquire(pt.CONSENT, a)[0] for _ in range(6)] == [True] * 5 + [False]


def test_application_clock_is_irrelevant(db, monkeypatch):
    """Skewed app clock (far past and far future): verdicts depend on the database clock only."""
    a = _k("203.0.113.7")
    monkeypatch.setattr(time, "time", lambda: 0.0)
    assert [pt.acquire(pt.CONSENT, a)[0] for _ in range(3)] == [True] * 3
    monkeypatch.setattr(time, "time", lambda: 4.0e9)
    assert [pt.acquire(pt.CONSENT, a)[0] for _ in range(3)] == [True, True, False]


def test_database_clock_skew_far_future_rows_still_decay_by_db_time(db):
    """A row written under a DB clock that was ahead (tat far in the future) blocks only until
    the DB clock reaches it; the application never compares its own time."""
    a = _k("203.0.113.8")
    with db["dict"]() as conn:
        conn.execute("INSERT INTO oauth_throttle_buckets (scope, key_hash, tat) VALUES "
                     "('consent', %s, statement_timestamp() + interval '2 hours')", (a,))
    ok, retry = pt.acquire(pt.CONSENT, a)
    assert ok is False and retry > 3600
    _age(db, 7200)
    assert pt.acquire(pt.CONSENT, a)[0] is True


def test_rotation_previous_key_is_checked_not_written(db):
    old, new = secrets.token_bytes(32), secrets.token_bytes(32)
    prev, cur = pt.key_hash("t0", old, "203.0.113.9"), pt.key_hash("t1", new, "203.0.113.9")
    for _ in range(5):
        pt.acquire(pt.CONSENT, prev)
    before = _rows(db, f"key_hash = '{prev}'")
    assert pt.acquire(pt.CONSENT, cur, prev) == (False, pytest.approx(180, abs=2))
    assert _rows(db, f"key_hash = '{prev}'") == before and not _rows(db, f"key_hash = '{cur}'")
    _age(db, 901)
    assert pt.acquire(pt.CONSENT, cur, prev)[0] is True
    assert _rows(db, f"key_hash = '{cur}'") and _rows(db, f"key_hash = '{prev}'") == []  # GC removed the decayed old row


def test_no_plain_address_reaches_the_table(db, key, monkeypatch):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    from types import SimpleNamespace
    for ip in ("203.0.113.10", "2001:db8:5:6::7"):
        req = SimpleNamespace(client=SimpleNamespace(host="10.0.0.2"), headers={"cf-connecting-ip": ip})
        assert asyncio.run(pt.check(pt.CONSENT, req)).action == "allow"
    with db["dict"]() as conn:
        dump = json.dumps(conn.execute("SELECT * FROM oauth_throttle_buckets").fetchall(), default=str)
    for leak in ("203.0.113.10", "2001:db8:5:6", "v4:", "v6:", key.split(":", 1)[1]):
        assert leak not in dump


def test_ipv6_64_grouping_and_ipv4_on_the_real_table(db):
    s = secrets.token_bytes(32)
    same = [pt.key_hash("t1", s, f"2001:db8:aa:bb::{i:x}") for i in range(1, 7)]
    assert len(set(same)) == 1
    assert [pt.acquire(pt.CONSENT, k)[0] for k in same] == [True] * 5 + [False]
    # The neighbouring /64 and an IPv4 address are not blocked by it (no false block).
    assert pt.acquire(pt.CONSENT, pt.key_hash("t1", s, "2001:db8:aa:bc::1"))[0] is True
    assert pt.acquire(pt.CONSENT, pt.key_hash("t1", s, "203.0.113.11"))[0] is True


# ------------------------------------------------------------------ concurrency

def test_threads_with_a_barrier_never_exceed_the_limit(db):
    a = _k("203.0.113.12")
    barrier = threading.Barrier(20)

    def one():
        barrier.wait(10)
        return pt.acquire(pt.CONSENT, a)[0]

    with ThreadPoolExecutor(20) as ex:
        out = list(ex.map(lambda _: one(), range(20)))
    assert out.count(True) == 5


def test_threads_with_a_barrier_on_the_global_bucket(db):
    barrier = threading.Barrier(16)

    def burst(t):
        barrier.wait(10)
        return [pt.acquire(pt.CONSENT, _k(f"198.18.{t}.{i}"))[0] for i in range(5)]

    with ThreadPoolExecutor(16) as ex:
        out = [x for chunk in ex.map(burst, range(16)) for x in chunk]
    assert out.count(True) == 50
    assert len(_rows(db, "scope = 'consent'")) == 51


def _proc_acquire(dsn, schema, key_hash, n, start, q):
    """Runs in a spawned process: a fresh interpreter, as after a restart or on another worker."""
    from bridge import oauth_persistent_throttle as m
    connect = _connect_factory(dsn, schema)
    start.wait(30)
    q.put([m.acquire(m.CONSENT, key_hash, connect=connect)[0] for _ in range(n)])


def test_processes_with_spawn_share_the_limit(db):
    ctx = multiprocessing.get_context("spawn")
    start, q = ctx.Event(), ctx.Queue()
    a = _k("203.0.113.13")
    procs = [ctx.Process(target=_proc_acquire, args=(db["dsn"], db["schema"], a, 3, start, q)) for _ in range(4)]
    for p in procs:
        p.start()
    start.set()
    results = [x for _ in procs for x in q.get(timeout=120)]
    for p in procs:
        p.join(60)
    assert results.count(True) == 5 and len(results) == 12


# ------------------------------------------------------------------ anti-DoS

def test_many_key_flood_keeps_cardinality_bounded(db):
    for i in range(400):
        pt.acquire(pt.REGISTRATION, _k(f"100.{i // 200}.{i % 200}.1"))
    rows = _rows(db, "scope = 'registration'")
    assert len(rows) <= 1 + pt.REGISTRATION.global_


def test_gc_is_bounded_skips_locked_rows_and_never_deletes_the_global_row(db):
    with db["dict"]() as conn:
        conn.execute("INSERT INTO oauth_throttle_buckets (scope, key_hash, tat) "
                     "SELECT 'consent', 't1:' || lpad(to_hex(g), 64, '0'), statement_timestamp() - interval '1 day' "
                     "FROM generate_series(1, 200) g")
        conn.execute("INSERT INTO oauth_throttle_buckets (scope, key_hash, tat) VALUES "
                     "('consent', '*', statement_timestamp() - interval '1 day')")
    locked_key = "t1:" + format(1, "x").rjust(64, "0")
    holder = psycopg.connect(db["dsn"], options=f"-c search_path={db['schema']}")
    holder.execute("SELECT 1 FROM oauth_throttle_buckets WHERE key_hash = %s FOR UPDATE", (locked_key,))
    try:
        t0 = time.monotonic()
        assert pt.acquire(pt.REGISTRATION, _k("203.0.113.14"))[0] is True
        assert time.monotonic() - t0 < 2.0                        # never waits on the locked row
        remaining = _rows(db, "scope = 'consent' AND key_hash <> '*'")
        assert len(remaining) == 200 - pt.GC_BATCH
        assert _rows(db, "scope = 'consent' AND key_hash = '*'")
        assert any(r["key_hash"] == locked_key for r in remaining)
    finally:
        holder.rollback()
        holder.close()


def test_a_held_global_lock_fails_fast_with_lock_timeout(db):
    pt.acquire(pt.CONSENT, _k("203.0.113.15"))
    holder = psycopg.connect(db["dsn"], options=f"-c search_path={db['schema']}")
    holder.execute("SELECT 1 FROM oauth_throttle_buckets WHERE scope = 'consent' AND key_hash = '*' FOR UPDATE")
    try:
        t0 = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            pt.acquire(pt.CONSENT, _k("203.0.113.16"))
        assert time.monotonic() - t0 < 1.5
    finally:
        holder.rollback()
        holder.close()


def test_flood_never_holds_more_than_db_concurrency_connections(db, key, monkeypatch):
    """Counts real PostgreSQL connections opened by the throttle during a 120-key burst."""
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    real, active, peak, lock = db["connect"], [0], [0], threading.Lock()

    class Counted:
        def __init__(self):
            self.c = real()

        def __enter__(self):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            self.c.__enter__()
            time.sleep(0.05)
            return self.c

        def __exit__(self, *exc):
            try:
                return self.c.__exit__(*exc)
            finally:
                with lock:
                    active[0] -= 1

    monkeypatch.setattr(pt, "_conn", Counted)
    from types import SimpleNamespace

    async def scenario():
        reqs = [SimpleNamespace(client=SimpleNamespace(host="10.0.0.3"),
                                headers={"cf-connecting-ip": f"198.19.{i // 200}.{i % 200}"}) for i in range(120)]
        return await asyncio.gather(*(pt.check(pt.CONSENT, r) for r in reqs))

    decisions = asyncio.run(scenario())
    assert peak[0] <= pt.DB_CONCURRENCY
    assert {d.code for d in decisions} <= {"OK", "STORE_BUSY", "LIMITED"}


# ------------------------------------------------------------------ end to end through the endpoints

def _consent(password, ip="198.51.100.40"):
    from tests.test_oauth_hardening import _consent_form, _req
    form = _consent_form(password, client_id=DCR)
    return asyncio.run(oauth.authorize_post(_req("POST", "/oauth/authorize", form,
                                                 {"cf-connecting-ip": ip}, ("10.0.0.4", 1))))


DCR = None


@pytest.fixture
def dcr(db, monkeypatch):
    global DCR
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", PASSWORD)
    DCR = oauth_store.register_claude_dcr_client(CLAUDE_REDIRECT)
    return DCR


def test_restart_resets_layer_one_only_off_is_the_red_baseline(db, dcr, key, monkeypatch):
    """RED baseline (main, and mode off): a restart (layer-1 reset) grants a fresh window."""
    for _ in range(5):
        assert _consent("wrong").status_code == 401
    assert _consent(PASSWORD).status_code == 429
    oauth_throttle.reset_all()                                  # restart / spin-down
    assert _consent(PASSWORD).status_code == 302
    assert not _rows(db)                                        # off: the table was never touched


def test_restart_persistence_consent_in_enforce(db, dcr, key, monkeypatch):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    for _ in range(5):
        assert _consent("wrong").status_code == 401
    oauth_throttle.reset_all()                                  # restart / spin-down
    r = _consent(PASSWORD)                                      # even the RIGHT password
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert _consent(PASSWORD, ip="198.51.100.41").status_code == 302   # another source unaffected


def test_success_is_refunded_on_the_real_table(db, dcr, key, monkeypatch):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    for _ in range(4):
        assert _consent("wrong").status_code == 401
    for _ in range(3):
        assert _consent(PASSWORD).status_code == 302            # successes do not consume the 5th slot
    assert _consent("wrong").status_code == 401
    oauth_throttle.reset_all()
    assert _consent(PASSWORD).status_code == 429


def test_restart_persistence_registration_is_recorded_observe_only(db, key, monkeypatch, caplog):
    from tests.test_oauth_hardening import _register
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    for _ in range(10):
        assert _register(client=("203.0.113.30", 1)).status_code == 201
    oauth_throttle.reset_all()
    import logging
    with caplog.at_level(logging.INFO):
        assert _register(client=("203.0.113.30", 1)).status_code == 201   # observe-only (D03)
    assert "scope=registration mode=shadow outcome=would_deny code=LIMITED" in caplog.text
    assert len(_rows(db, "scope = 'registration'")) == 2


@pytest.mark.parametrize("password", [PASSWORD, "wrong"])
def test_db_unavailable_is_a_uniform_503_before_the_compare(db, dcr, key, monkeypatch, password):
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    monkeypatch.setattr(pt, "_conn", _connect_factory("postgresql://postgres@127.0.0.1:1/none", "public"))
    r = _consent(password)
    assert r.status_code == 503 and json.loads(r.body) == {"error": "temporarily_unavailable"}


def test_shadow_with_the_db_unavailable_never_blocks(db, dcr, key, monkeypatch):
    monkeypatch.setenv(pt.MODE_ENV, "shadow")
    monkeypatch.setattr(pt, "_conn", _connect_factory("postgresql://postgres@127.0.0.1:1/none", "public"))
    assert _consent(PASSWORD).status_code == 302


def test_token_refresh_and_bearer_continue_while_the_throttle_store_is_down(db, dcr, key, monkeypatch):
    """D12: a client's refresh after a wake-up and its /mcp calls never depend on the throttle."""
    from tests.test_oauth_hardening import CHALLENGE, VERIFIER, _req
    monkeypatch.setenv(pt.MODE_ENV, "enforce")
    oauth_store.save_code("code-1", client_id=dcr, redirect_uri=CLAUDE_REDIRECT, code_challenge=CHALLENGE,
                          resource=oauth.resource_url(), scope="browser:use offline_access")
    monkeypatch.setattr(pt, "_conn", _connect_factory("postgresql://postgres@127.0.0.1:1/none", "public"))
    monkeypatch.setenv(pt.MODE_ENV, "bogus")                   # even CONFIG_INVALID
    r = asyncio.run(oauth.token_endpoint(_req("POST", "/oauth/token", {
        "grant_type": "authorization_code", "client_id": dcr, "code": "code-1",
        "redirect_uri": CLAUDE_REDIRECT, "code_verifier": VERIFIER})))
    assert r.status_code == 200
    body = json.loads(r.body)
    refresh = body["refresh_token"]
    for _ in range(12):                                         # above every throttle limit
        r = asyncio.run(oauth.token_endpoint(_req("POST", "/oauth/token", {
            "grant_type": "refresh_token", "client_id": dcr, "refresh_token": refresh})))
        assert r.status_code == 200
        body = json.loads(r.body)
        refresh = body["refresh_token"]
        oauth.reset_bearer_cache()
        assert asyncio.run(oauth.bearer_valid(body["access_token"]))


# ------------------------------------------------------------------ grants

def test_runtime_grant_block_gives_exactly_s_i_u_d_on_the_buckets_and_nothing_on_the_audit(db):
    """The DO block shipped in both grant files, run against this schema with a throwaway role
    standing in for fcb_app; the role then runs acquire/refund/GC and cannot touch the audit."""
    role = "thr_app_" + uuid.uuid4().hex[:8]
    text = (ROOT / "sql" / "aiven" / "grant_bridge_runtime.sql").read_text()
    block = text[text.index("DO $throttle$"):text.index("END $throttle$;") + len("END $throttle$;")]
    assert block in (ROOT / "sql" / "aiven" / "finalize_bridge_privileges.sql").read_text()
    block = block.replace("public.oauth_throttle_buckets", f"{db['schema']}.oauth_throttle_buckets").replace("fcb_app", role)
    dsn, schema = db["dsn"], db["schema"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(role)))
        conn.execute(SQL("GRANT USAGE ON SCHEMA {} TO {}").format(Identifier(schema), Identifier(role)))
        conn.execute(block)
    try:
        def as_role():
            c = psycopg.connect(dsn, options=f"-c search_path={schema} -c lock_timeout=500")
            c.execute(SQL("SET ROLE {}").format(Identifier(role)))
            return c
        assert pt.acquire(pt.CONSENT, _k("203.0.113.50"), connect=as_role)[0] is True
        pt.refund(pt.CONSENT, _k("203.0.113.50"), connect=as_role)
        with as_role() as c:
            privs = c.execute("SELECT has_table_privilege(%s, 'oauth_throttle_buckets', 'SELECT,INSERT,UPDATE,DELETE'), "
                              "has_table_privilege(%s, 'oauth_throttle_buckets', 'TRUNCATE'), "
                              "has_table_privilege(%s, 'oauth_throttle_unlock_audit', 'SELECT,INSERT,UPDATE,DELETE')",
                              (role, role, role)).fetchone()
            assert privs == (True, False, False)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT 1 FROM oauth_throttle_unlock_audit")
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
            conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


def test_runtime_grant_block_is_a_noop_without_0009(db):
    dsn = db["dsn"]
    schema = "thr_pre_" + uuid.uuid4().hex[:8]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        try:
            text = (ROOT / "sql" / "aiven" / "grant_bridge_runtime.sql").read_text()
            block = text[text.index("DO $throttle$"):text.index("END $throttle$;") + len("END $throttle$;")]
            conn.execute(block.replace("public.oauth_throttle_buckets", f"{schema}.oauth_throttle_buckets"))
        finally:
            conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


# ------------------------------------------------------------------ migration 0009 and readiness

@pytest.fixture
def bare(monkeypatch):
    dsn = os.environ["TEST_DATABASE_URL"]
    schema = "thr_mig_" + uuid.uuid4().hex[:10]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={schema}")
    yield schema, connect
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


def _series_dir(tmp_path, upto):
    d = tmp_path / f"upto{upto}"
    d.mkdir()
    for p in sorted(migrations.BRIDGE.directory.glob("*.sql")):
        if int(p.name[:4]) <= upto:
            shutil.copyfile(p, d / p.name)
    return d


def _tracking(connect):
    with connect() as conn:
        return conn.execute("SELECT version, name, sha256, applied_at FROM schema_migrations ORDER BY version").fetchall()


def test_0009_on_an_already_initialised_database_keeps_history_and_oauth_rows(bare, tmp_path, monkeypatch):
    schema, connect = bare
    migrations.apply_migrations(connect, _series_dir(tmp_path, 8))
    monkeypatch.setattr(oauth_store, "_conn", connect)
    dcr_id = oauth_store.register_claude_dcr_client(CLAUDE_REDIRECT)
    oauth_store.save_code("c-pre", client_id=dcr_id, redirect_uri=CLAUDE_REDIRECT, code_challenge="CH",
                          resource="https://b/mcp", scope="browser:use")
    before = _tracking(connect)
    with connect() as conn:
        snapshot = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("oauth_claude_dcr_clients", "oauth_authorization_codes")}
    migrations.apply_migrations(connect)
    after = _tracking(connect)
    assert after[:8] == before                                  # names, checksums AND applied_at
    assert after[8]["version"] == 9 and after[8]["name"] == "0009_oauth_throttle_buckets.sql"
    assert after[8]["sha256"] == sha256((migrations.BRIDGE.directory / after[8]["name"]).read_bytes()).hexdigest()
    with connect() as conn:
        assert snapshot == {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in snapshot}
    assert oauth_store.valid_claude_dcr_client(dcr_id, CLAUDE_REDIRECT)
    for _ in range(3):
        migrations.apply_migrations(connect)
    assert _tracking(connect) == after                          # idempotent re-apply


def test_0009_concurrent_appliers_on_an_initialised_database(bare, tmp_path):
    schema, connect = bare
    migrations.apply_migrations(connect, _series_dir(tmp_path, 8))
    errors = []

    def run():
        try:
            migrations.apply_migrations(connect)
        except Exception as exc:  # pragma: no cover
            errors.append(type(exc).__name__)
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and [r["version"] for r in _tracking(connect)] == list(range(1, 10))


def test_0009_is_a_noop_where_the_tables_already_exist(bare, tmp_path):
    schema, connect = bare
    migrations.apply_migrations(connect, _series_dir(tmp_path, 8))
    sql = (migrations.BRIDGE.directory / "0009_oauth_throttle_buckets.sql").read_text()
    with connect() as conn:
        conn.execute(sql)
        conn.execute("INSERT INTO oauth_throttle_buckets (scope, key_hash, tat) VALUES ('consent', '*', now())")
    migrations.apply_migrations(connect)
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM oauth_throttle_buckets").fetchone()["n"] == 1


def test_table_constraints_refuse_plain_addresses_and_unknown_scopes(bare):
    schema, connect = bare
    migrations.apply_migrations(connect)
    for scope, key in (("consent", "203.0.113.1"), ("consent", "v4:203.0.113.1"), ("token", "*"),
                       ("consent", "t1:" + "A" * 64)):
        with pytest.raises(psycopg.errors.CheckViolation), connect() as conn:
            conn.execute("INSERT INTO oauth_throttle_buckets (scope, key_hash, tat) VALUES (%s, %s, now())", (scope, key))


def test_readiness_off_defers_0009_and_shadow_requires_it(bare, tmp_path, monkeypatch):
    schema, connect = bare
    migrations.apply_migrations(connect, _series_dir(tmp_path, 8))
    monkeypatch.delenv(pt.MODE_ENV, raising=False)
    assert evaluate(connect) == {"state": "READY", "missing_tables": [], "bridge_versions": 9,
                                 "deferred_versions": [9]}
    for m in ("shadow", "enforce", "bogus"):
        monkeypatch.setenv(pt.MODE_ENV, m)
        assert evaluate(connect)["state"] == "OUTDATED"
    migrations.apply_migrations(connect)
    for m in ("off", "shadow", "enforce"):
        monkeypatch.setenv(pt.MODE_ENV, m)
        assert evaluate(connect) == {"state": "READY", "missing_tables": [], "bridge_versions": 9,
                                     "deferred_versions": []}
    with connect() as conn:
        conn.execute("DROP TABLE oauth_throttle_buckets")
    monkeypatch.setenv(pt.MODE_ENV, "off")                      # recorded 0009 => its tables are required
    result = evaluate(connect)
    assert result["state"] == "MISSING_TABLES" and result["missing_tables"] == ["oauth_throttle_buckets"]
