"""PATH A2 variant R1 (owner decision D2) on a REAL PostgreSQL server (18 in CI and locally).

fcb_cert_runner holds NO privilege on the three shared quota tables; its only access to the shared
coordinator is the four SECURITY DEFINER functions of schema fcb_cert_quota
(sql/aiven/apifootball_cert_path_a_quota_functions.sql), installed by the real step 1a/1b code of
scripts/apifootball_cert_path_a.py. The runner logs in with SCRAM (require_auth=scram-sha-256); the
"bridge" is the real PostgresQuota as the tables' owner (the Aiven-like administrator of this module).
Proves: same results as PostgresQuota on the same sequence; no direct SQL on counters/leases/attempts;
no caller-controlled limit; only named `cert-` leases released; malformed input refused; identity by
SESSION_USER; search_path / pg_temp shadowing without effect; no CREATE/ALTER/REPLACE; concurrency of
runner processes AND bridge processes never above 8/min, 80/day, 2 leases; crash recovery; exact-state
drift refused by the step-1b assert and by the runner before any provider request. No network.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.sql import SQL, Identifier, Literal  # noqa: E402

SU_DSN = os.environ.get("PATH_A_TEST_SUPERUSER_DSN", "")
pytestmark = pytest.mark.skipif(not SU_DSN, reason="requires a throwaway PATH_A_TEST_SUPERUSER_DSN")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
from apifootball_campaign_kit import Provider, load, settings  # noqa: E402

batch = load("apifootball_cert_batch1_r1", "apifootball_cert_batch1.py")
path_a = load("apifootball_cert_path_a_r1", "apifootball_cert_path_a.py")
ADMIN = "fcbr1_avnadmin"
OTHER = "fcbr1_other"
RUNNER = "fcb_cert_runner"
OWNER = "fcb_cert_owner"
TABLES = ("api_football_quota_counters", "api_football_quota_leases", "api_football_quota_attempts")
FUNCTIONS = ("reserve(text)", "release(text, text)", "snapshot(text)", "mark_exhausted(text, text)")


def _dsn(user, password, dbname, require=True):
    params = conninfo_to_dict(SU_DSN)
    params.update(user=user, password=password, dbname=dbname)
    if require:
        params["require_auth"] = "scram-sha-256"
    return make_conninfo(**params)


def _h(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from app import migrations
    from fcb_db_roles import scram_verifier
    admin_pw, runner_pw, other_pw = ("adm-" + uuid.uuid4().hex, "run-" + uuid.uuid4().hex + uuid.uuid4().hex,
                                     "oth-" + uuid.uuid4().hex)
    with psycopg.connect(SU_DSN, autocommit=True) as su:
        if su.execute("SELECT 1 FROM pg_roles WHERE rolname = ANY(%s) UNION ALL SELECT 1 FROM pg_database "
                      "WHERE datname = 'defaultdb'", ([ADMIN, OTHER, OWNER, RUNNER],)).fetchone():
            pytest.skip("PATH A names already exist on this server; not touching them")
        su.execute(SQL("CREATE ROLE {} LOGIN CREATEROLE CREATEDB BYPASSRLS PASSWORD {}").format(Identifier(ADMIN), Literal(admin_pw)))
        su.execute(SQL("ALTER ROLE {} SET createrole_self_grant = 'inherit,set'").format(Identifier(ADMIN)))
        su.execute(SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(Identifier(OTHER), Literal(other_pw)))
        su.execute(SQL("CREATE DATABASE defaultdb OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(Identifier(ADMIN)))
    ns = SimpleNamespace(
        admin=lambda **kw: psycopg.connect(_dsn(ADMIN, admin_pw, "defaultdb"), row_factory=dict_row, **kw),
        runner_dsn=_dsn(RUNNER, runner_pw, "defaultdb"),
        runner=lambda **kw: psycopg.connect(_dsn(RUNNER, runner_pw, "defaultdb"), row_factory=dict_row, **kw),
        other=lambda **kw: psycopg.connect(_dsn(OTHER, other_pw, "defaultdb"), row_factory=dict_row, **kw),
        su=lambda: psycopg.connect(make_conninfo(SU_DSN, dbname="defaultdb"), row_factory=dict_row, autocommit=True),
        admin_dsn=_dsn(ADMIN, admin_pw, "defaultdb"))
    try:
        upto8 = tmp_path_factory.mktemp("r1upto8")
        for m in migrations.load_migrations()[:8]:
            (upto8 / m.name).write_bytes((migrations.BRIDGE.directory / m.name).read_bytes())
        migrations.apply_migrations(ns.admin, upto8)  # the administrator owns bridge 0001-0008
        path_a.install_roles(ns.admin, scram_verifier(runner_pw))
        path_a.install_quota_schema(ns.admin)
        path_a.install_quota_functions(ns.admin)  # the tables' owner (pre-WP-009 shape of this module)
        yield ns
    finally:
        with psycopg.connect(SU_DSN, autocommit=True) as su:
            su.execute("DROP DATABASE IF EXISTS defaultdb WITH (FORCE)")
            for role in (RUNNER, OWNER, OTHER, ADMIN):
                su.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))


def _rows(ns, key_hash):
    with ns.su() as su:
        return {t: su.execute(f"SELECT count(*) AS n FROM public.{t} WHERE api_key_hash = %s", (key_hash,)).fetchone()["n"]
                for t in TABLES}


def _counters(ns, key_hash):
    with ns.su() as su:
        return sorted((r["window_kind"], r["used"]) for r in su.execute(
            "SELECT window_kind, used FROM public.api_football_quota_counters WHERE api_key_hash = %s", (key_hash,)))


def _state(ns):
    with ns.su() as su:
        return su.execute(batch.quota_state_sql()).fetchone()["state"]


def _bridge(ns):
    from bridge.api_football.quota import PostgresQuota, QuotaLimits
    return PostgresQuota(QuotaLimits(8, 80, 2), connect=ns.admin)  # the bridge defaults, as the tables' owner


def _runner_q(ns):
    return batch.CertQuotaCoordinator(connect=ns.runner)


# ------------------------------------------------------------------ install and exact state

def test_install_is_exact_and_idempotent(env):
    assert _state(env) == "EXACT"
    path_a.install_quota_schema(env.admin)
    path_a.install_quota_functions(env.admin)  # second run: no change
    path_a.assert_quota_functions(env.admin)
    assert _state(env) == "EXACT"
    with env.su() as su:
        assert su.execute("SELECT count(*) AS n FROM pg_class c, LATERAL aclexplode(c.relacl) x "
                          "WHERE x.grantee = %s::regrole", (RUNNER,)).fetchone()["n"] == 0
        assert su.execute("SELECT count(*) AS n FROM pg_attribute a, LATERAL aclexplode(a.attacl) x "
                          "WHERE x.grantee = %s::regrole", (RUNNER,)).fetchone()["n"] == 0


def test_step_1b_refuses_a_caller_that_is_not_the_tables_owner(env):
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PATH_A_QUOTA_GUARD_FAILED"):
        path_a.install_quota_functions(env.other)
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PATH_A_QUOTA_GUARD_FAILED"):
        path_a.install_quota_functions(env.runner)


# ------------------------------------------------------------------ differential vs PostgresQuota

def _outcome(coro):
    from bridge.api_football.quota import QuotaConcurrencyBusy, QuotaExceeded
    try:
        return ("ok", asyncio.run(coro))
    except QuotaExceeded as exc:
        return ("exceeded", exc.window, str(exc))
    except QuotaConcurrencyBusy as exc:
        return ("busy", str(exc))


def test_differential_same_sequence_same_results_as_postgres_quota(env):
    """The same operation sequence, runner (functions) on key A and bridge (PostgresQuota 8/80/2) on a
    twin key B: identical outcomes (type, window, message), snapshots, counters and row counts."""
    runner, bridge = _runner_q(env), _bridge(env)
    sa, sb = "R1-DIFF-A-" + uuid.uuid4().hex, "R1-DIFF-B-" + uuid.uuid4().hex
    trace = {}
    for name, q, secret in (("runner", runner, sa), ("bridge", bridge, sb)):
        out, leases = [], []

        def step(kind, *args, q=q, secret=secret, out=out, leases=leases):
            if kind == "acquire":
                r = _outcome(q.acquire(secret))
                if r[0] == "ok":
                    leases.append(r[1])
                    r = ("ok",)
            elif kind == "release":
                asyncio.run(q.release(leases.pop(0)))
                r = ("released",)
            elif kind == "snapshot":
                r = ("snapshot", asyncio.run(q.snapshot(secret)))
            else:
                asyncio.run(q.mark_exhausted(secret, args[0]))
                r = ("marked", args[0])
            out.append(r)
        step("snapshot")
        step("acquire"); step("acquire"); step("acquire")  # third: 2 leases active -> busy
        step("snapshot")
        step("release"); step("release")
        for _ in range(6):  # 8 attempts in the rolling minute
            step("acquire"); step("release")
        step("acquire")  # 9th: rolling window exhausted
        step("snapshot")
        step("mark", "minute"); step("acquire"); step("snapshot")
        step("mark", "day"); step("snapshot")
        trace[name] = (out, _counters(env, _h(secret)), _rows(env, _h(secret)))
    assert trace["runner"][0] == trace["bridge"][0]
    assert trace["runner"][1] == trace["bridge"][1]
    assert trace["runner"][2] == trace["bridge"][2]
    kinds = [o[0] for o in trace["runner"][0]]
    assert "busy" in kinds and kinds.count("exceeded") == 2 and trace["runner"][0][-1][1]["remaining_day"] == 0


def test_runner_leases_and_attempts_are_the_shared_rows_the_bridge_counts(env):
    """Same key: what the runner reserves, the bridge sees (and vice versa): one shared budget."""
    secret = "R1-SHARED-" + uuid.uuid4().hex
    runner, bridge = _runner_q(env), _bridge(env)
    l1 = asyncio.run(runner.acquire(secret))
    l2 = asyncio.run(bridge.acquire(secret))
    assert l1.lease_id.startswith("cert-") and not l2.lease_id.startswith("cert-")
    from bridge.api_football.quota import QuotaConcurrencyBusy
    with pytest.raises(QuotaConcurrencyBusy):
        asyncio.run(bridge.acquire(secret))
    with pytest.raises(QuotaConcurrencyBusy):
        asyncio.run(runner.acquire(secret))
    assert asyncio.run(bridge.snapshot(secret)) == asyncio.run(runner.snapshot(secret))
    asyncio.run(runner.release(l1)); asyncio.run(bridge.release(l2))
    assert _rows(env, _h(secret))["api_football_quota_leases"] == 0


# ------------------------------------------------------------------ no direct SQL, no caller limits

def test_runner_has_no_direct_sql_on_counters_leases_or_attempts(env):
    secret = "R1-DIRECT-" + uuid.uuid4().hex
    bridge_lease = asyncio.run(_bridge(env).acquire(secret))
    key = _h(secret)
    before = (_counters(env, key), _rows(env, key))
    statements = [f"SELECT * FROM public.{t}" for t in TABLES] + [
        f"INSERT INTO public.api_football_quota_counters VALUES ('{key}', 'day', 1, 0)",
        "INSERT INTO public.api_football_quota_leases VALUES ('x', 'y', now())",
        "INSERT INTO public.api_football_quota_attempts (attempt_id, api_key_hash) VALUES ('x', 'y')",
        "UPDATE public.api_football_quota_counters SET used = 0",
        f"UPDATE public.api_football_quota_counters SET used = 0 WHERE api_key_hash = '{key}'",
        f"DELETE FROM public.api_football_quota_leases WHERE lease_id = '{bridge_lease.lease_id}'",
        "DELETE FROM public.api_football_quota_attempts",
        "DELETE FROM public.api_football_quota_counters",
        "TRUNCATE public.api_football_quota_counters", "TRUNCATE public.api_football_quota_leases",
        "TRUNCATE public.api_football_quota_attempts",
        "COPY public.api_football_quota_counters TO STDOUT",
        "ALTER TABLE public.api_football_quota_counters DISABLE TRIGGER ALL",
        "LOCK TABLE public.api_football_quota_leases IN ACCESS EXCLUSIVE MODE"]
    for statement in statements:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with env.runner() as conn:
                conn.execute(statement)
    assert (_counters(env, key), _rows(env, key)) == before
    asyncio.run(_bridge(env).release(bridge_lease))


def test_limits_are_not_caller_controlled(env):
    with env.su() as su:
        args = {r["f"]: r["n"] for r in su.execute(
            "SELECT p.proname AS f, p.pronargs AS n FROM pg_proc p WHERE p.pronamespace = 'fcb_cert_quota'::regnamespace")}
    assert args == {"reserve": 1, "release": 2, "snapshot": 1, "mark_exhausted": 2}  # key hash (+ lease/kind) only
    key = _h("R1-LIMITS-" + uuid.uuid4().hex)
    for statement, params in (("SELECT fcb_cert_quota.reserve(%s, 1000)", (key,)),
                              ("SELECT fcb_cert_quota.reserve(%s, 1000, 100000, 32)", (key,)),
                              ("SELECT fcb_cert_quota.reserve(p_key_hash => %s, per_minute => 1000)", (key,))):
        with pytest.raises(psycopg.errors.UndefinedFunction):
            with env.runner() as conn:
                conn.execute(statement, params)
    # GUCs a caller can set do not change the pinned limits
    with env.runner() as conn:
        conn.execute("SET fcb.per_minute = '1000'")
        conn.execute("SET search_path = pg_temp")
        leases = [conn.execute("SELECT fcb_cert_quota.reserve(%s) AS l", (key,)).fetchone()["l"] for _ in range(2)]
        with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_QUOTA_BUSY"):
            conn.execute("SELECT fcb_cert_quota.reserve(%s)", (key,))
    assert len(leases) == 2


def test_release_deletes_only_the_named_cert_lease_of_the_named_key(env):
    secret, other_secret = "R1-REL-" + uuid.uuid4().hex, "R1-REL2-" + uuid.uuid4().hex
    bridge_lease = asyncio.run(_bridge(env).acquire(secret))
    mine = asyncio.run(_runner_q(env).acquire(other_secret))
    key, other = _h(secret), _h(other_secret)
    with env.runner() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_QUOTA_BAD_LEASE_ID"):
            conn.execute("SELECT fcb_cert_quota.release(%s, %s)", (key, bridge_lease.lease_id))  # a bridge lease
    with env.runner() as conn:
        conn.execute("SELECT fcb_cert_quota.release(%s, %s)", (key, mine.lease_id))  # right lease, wrong key
        conn.execute("SELECT fcb_cert_quota.release(%s, %s)", (other, "cert-" + "A" * 32))  # a lease that does not exist
    for bad in ("%", "cert-%", "cert-" + "_" * 32 + "%", "' OR true --"):
        with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_QUOTA_BAD_LEASE_ID"):
            with env.runner() as conn:
                conn.execute("SELECT fcb_cert_quota.release(%s, %s)", (other, bad))
    assert _rows(env, key)["api_football_quota_leases"] == 1 and _rows(env, other)["api_football_quota_leases"] == 1
    asyncio.run(_runner_q(env).release(mine))
    assert _rows(env, other)["api_football_quota_leases"] == 0  # the named one, only
    assert _rows(env, key)["api_football_quota_leases"] == 1
    asyncio.run(_bridge(env).release(bridge_lease))


BAD_KEYS = [None, "", "A" * 64, "0" * 63, "0" * 65, "g" * 64, "0" * 63 + "\n", " " + "0" * 63, "0" * 64 + "\n",
            "é" * 64, "0" * 32 + "%" + "0" * 31]


@pytest.mark.parametrize("bad", BAD_KEYS)
def test_malformed_key_hash_is_refused_by_every_function_and_changes_nothing(env, bad):
    for statement, extra in (("SELECT fcb_cert_quota.reserve(%s)", ()), ("SELECT * FROM fcb_cert_quota.snapshot(%s)", ()),
                             ("SELECT fcb_cert_quota.release(%s, %s)", ("cert-" + "A" * 32,)),
                             ("SELECT fcb_cert_quota.mark_exhausted(%s, %s)", ("minute",))):
        with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_QUOTA_BAD_KEY_HASH"):
            with env.runner() as conn:
                conn.execute(statement, (bad, *extra))


@pytest.mark.parametrize("bad", [None, "", "hour", "MINUTE", "minute ", "day\n"])
def test_unknown_window_is_refused(env, bad):
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_QUOTA_BAD_WINDOW"):
        with env.runner() as conn:
            conn.execute("SELECT fcb_cert_quota.mark_exhausted(%s, %s)", ("0" * 64, bad))


@pytest.mark.parametrize("bad", [None, "", "cert-" + "A" * 31, "cert-" + "A" * 33, "CERT-" + "A" * 32, "cert-" + "é" * 32,
                                 "cert-" + "A" * 31 + "\n", "x" * 32, "provider-zero-abc-1"])
def test_malformed_lease_id_is_refused(env, bad):
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_QUOTA_BAD_LEASE_ID"):
        with env.runner() as conn:
            conn.execute("SELECT fcb_cert_quota.release(%s, %s)", ("0" * 64, bad))


# ------------------------------------------------------------------ identity

def test_only_the_session_identity_fcb_cert_runner_may_call(env):
    key = _h("R1-ID-" + uuid.uuid4().hex)
    # the tables' owner (EXECUTE as owner) is refused by the identity check
    for connect in (env.admin,):
        for statement in ("SELECT fcb_cert_quota.reserve(%s)", "SELECT * FROM fcb_cert_quota.snapshot(%s)"):
            with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_QUOTA_IDENTITY_REFUSED"):
                with connect() as conn:
                    conn.execute(statement, (key,))
    # another login role: no EXECUTE at all
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with env.other() as conn:
            conn.execute("SELECT fcb_cert_quota.reserve(%s)", (key,))
    # a role that can SET ROLE fcb_cert_runner (deliberate superuser act): CURRENT_USER is the runner,
    # SESSION_USER is not -> refused
    with env.su() as su:
        su.execute(f"GRANT {RUNNER} TO {OTHER} WITH INHERIT FALSE, SET TRUE")
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_QUOTA_IDENTITY_REFUSED"):
            with env.other() as conn:
                conn.execute(f"SET ROLE {RUNNER}")
                conn.execute("SELECT fcb_cert_quota.reserve(%s)", (key,))
        assert _state(env) == "DRIFT"  # and the exact state no longer holds: the runner refuses to run
    finally:
        with env.su() as su:
            su.execute(f"REVOKE {RUNNER} FROM {OTHER}")
    assert _state(env) == "EXACT" and _rows(env, key) == {t: 0 for t in TABLES}


# ------------------------------------------------------------------ no DDL, no shadowing

def test_runner_cannot_create_alter_replace_or_drop_anything(env):
    statements = [
        "CREATE FUNCTION fcb_cert_quota.x() RETURNS int LANGUAGE sql AS 'select 1'",
        "CREATE TABLE fcb_cert_quota.t (a int)", "CREATE TABLE public.t (a int)", "CREATE SCHEMA fcbr1_x",
        "CREATE OR REPLACE FUNCTION fcb_cert_quota.reserve(p_key_hash text) RETURNS text LANGUAGE sql AS $$select 'x'$$",
        "ALTER FUNCTION fcb_cert_quota.reserve(text) SECURITY INVOKER",
        "ALTER FUNCTION fcb_cert_quota.reserve(text) RESET search_path",
        "ALTER FUNCTION fcb_cert_quota.reserve(text) SET search_path = pg_temp, public",
        f"ALTER FUNCTION fcb_cert_quota.reserve(text) OWNER TO {RUNNER}",
        "ALTER FUNCTION fcb_cert_quota.reserve(text) RENAME TO r2",
        "DROP FUNCTION fcb_cert_quota.reserve(text)", "DROP SCHEMA fcb_cert_quota CASCADE",
        f"ALTER SCHEMA fcb_cert_quota OWNER TO {RUNNER}",
        f"GRANT CREATE ON SCHEMA fcb_cert_quota TO {RUNNER}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA fcb_cert_quota GRANT EXECUTE ON FUNCTIONS TO PUBLIC",
        "COMMENT ON FUNCTION fcb_cert_quota.reserve(text) IS 'x'",
        "SECURITY LABEL ON FUNCTION fcb_cert_quota.reserve(text) IS 'x'",
    ]
    for statement in statements:
        with pytest.raises(psycopg.Error) as exc:
            with env.runner() as conn:
                conn.execute(statement)
        assert exc.value.sqlstate in ("42501", "22023", "0LP01", "42704", "22P02"), (statement, exc.value.sqlstate)
    assert _state(env) == "EXACT"


def test_search_path_and_pg_temp_shadows_have_no_effect(env):
    """Temp tables named like the coordinator tables, a temp function and a hostile search_path: the
    functions still write the REAL public rows (pinned search_path + qualified names)."""
    secret = "R1-SHADOW-" + uuid.uuid4().hex
    key = _h(secret)
    with env.runner() as conn:
        for t in TABLES:
            conn.execute(f"CREATE TEMP TABLE {t} (api_key_hash text, window_kind text, bucket bigint, used int, "
                         "lease_id text, expires_at timestamptz, attempt_id text, created_at timestamptz)")
        conn.execute("CREATE FUNCTION pg_temp.clock_timestamp() RETURNS timestamptz LANGUAGE sql "
                     "AS $$ SELECT '2000-01-01'::timestamptz $$")
        conn.execute("CREATE FUNCTION pg_temp.gen_random_uuid() RETURNS uuid LANGUAGE sql "
                     "AS $$ SELECT '00000000-0000-0000-0000-000000000000'::uuid $$")
        conn.execute("SET search_path = pg_temp, fcb_cert_quota, public")
        lease = conn.execute("SELECT fcb_cert_quota.reserve(%s) AS l", (key,)).fetchone()["l"]
        snap = conn.execute("SELECT * FROM fcb_cert_quota.snapshot(%s)", (key,)).fetchone()
        temp = [conn.execute(f"SELECT count(*) AS n FROM pg_temp.{t}").fetchone()["n"] for t in TABLES]
        conn.commit()
    assert temp == [0, 0, 0] and lease != "cert-" + "A" * 32 and "AAAAAAAAAAAAAAAAAAAAA" not in lease
    assert _rows(env, key) == {"api_football_quota_counters": 2, "api_football_quota_leases": 1, "api_football_quota_attempts": 1}
    assert snap == {"remaining_minute": 7, "remaining_day": 79, "active_leases": 1}
    asyncio.run(_runner_q(env).release(type("L", (), {"lease_id": lease, "key_hash": key})()))


# ------------------------------------------------------------------ concurrency

def _runner_worker(dsn, secret, rounds, out):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from apifootball_campaign_kit import load as _load
    mod = _load("apifootball_cert_batch1_r1_worker", "apifootball_cert_batch1.py")
    q = mod.CertQuotaCoordinator(connect=lambda: psycopg.connect(dsn, row_factory=dict_row))
    _loop(q, secret, rounds, out)


def _bridge_worker(dsn, secret, rounds, out):
    sys.path.insert(0, str(ROOT))
    from bridge.api_football.quota import PostgresQuota, QuotaLimits
    _loop(PostgresQuota(QuotaLimits(8, 80, 2), connect=lambda: psycopg.connect(dsn, row_factory=dict_row)), secret, rounds, out)


def _loop(q, secret, rounds, out):
    from bridge.api_football.quota import QuotaConcurrencyBusy, QuotaExceeded
    held = []
    for _ in range(rounds):
        try:
            lease = asyncio.run(q.acquire(secret))
        except (QuotaExceeded, QuotaConcurrencyBusy) as exc:
            held.append(["refused", type(exc).__name__])
            time.sleep(0.02)
            continue
        start = time.time()
        time.sleep(0.15)
        end = time.time()
        asyncio.run(q.release(lease))
        held.append(["ok", start, end])
    Path(out).write_text(json.dumps(held), encoding="utf-8")


def _run_mixed(env, tmp_path, secret, runners=9, bridges=3, rounds=6):
    ctx = multiprocessing.get_context("spawn")
    procs = []
    for i in range(runners + bridges):
        target = _runner_worker if i < runners else _bridge_worker
        dsn = env.runner_dsn if i < runners else env.admin_dsn
        procs.append(ctx.Process(target=target, args=(dsn, secret, rounds, str(tmp_path / f"w{i}.json"))))
    for p in procs:
        p.start()
    for p in procs:
        p.join(180)
        assert p.exitcode == 0
    results = [json.loads((tmp_path / f"w{i}.json").read_text()) for i in range(runners + bridges)]
    ok = [r for w in results for r in w if r[0] == "ok"]
    events = sorted([(s, 1) for _, s, _ in ok] + [(e, -1) for _, _, e in ok], key=lambda x: (x[0], x[1]))
    level = peak = 0
    for _, d in events:
        level += d
        peak = max(peak, level)
    return results, ok, peak


def test_concurrency_runner_and_bridge_processes_never_exceed_minute_or_leases(env, tmp_path):
    secret = "R1-CONC-" + uuid.uuid4().hex
    results, ok, peak = _run_mixed(env, tmp_path, secret)
    # 12 processes x 6 attempts within a few seconds: at most 8 grants in the rolling minute, at most
    # 2 leases at any instant (hold windows measured by the workers), and runners AND bridge both refused
    assert 1 <= len(ok) <= 8 and peak <= 2, (len(ok), peak)
    counters = dict(_counters(env, _h(secret)))
    assert counters["minute"] <= 8 and counters["day"] == len(ok)
    assert _rows(env, _h(secret))["api_football_quota_attempts"] == len(ok)
    assert _rows(env, _h(secret))["api_football_quota_leases"] == 0


def test_concurrency_never_exceeds_the_day(env, tmp_path):
    secret = "R1-DAY-" + uuid.uuid4().hex
    key = _h(secret)
    with env.su() as su:  # the day already holds 78 of 80
        su.execute("INSERT INTO public.api_football_quota_counters VALUES (%s, 'day', "
                   "(EXTRACT(EPOCH FROM clock_timestamp())::bigint / 86400), 78)", (key,))
    results, ok, peak = _run_mixed(env, tmp_path, secret, runners=6, bridges=3, rounds=3)
    assert len(ok) <= 2 and peak <= 2
    assert dict(_counters(env, key))["day"] <= 80


# ------------------------------------------------------------------ crash recovery

def _reserve_then_die(dsn, key, mode):
    conn = psycopg.connect(dsn, row_factory=dict_row)
    if mode == "after_commit":
        conn.execute("SELECT fcb_cert_quota.reserve(%s)", (key,))
        conn.commit()
        os._exit(17)  # killed after the durable reservation, before release
    conn.execute("SELECT fcb_cert_quota.reserve(%s)", (key,))
    os._exit(9)  # killed inside the transaction: nothing may be committed


def test_killed_mid_transaction_commits_nothing(env):
    key = _h("R1-CRASH-TX-" + uuid.uuid4().hex)
    p = multiprocessing.get_context("spawn").Process(target=_reserve_then_die, args=(env.runner_dsn, key, "in_tx"))
    p.start(); p.join(60)
    assert p.exitcode == 9
    assert _rows(env, key) == {t: 0 for t in TABLES}


def test_killed_after_reserve_leaves_a_lease_that_expires_and_is_pruned(env):
    secret = "R1-CRASH-" + uuid.uuid4().hex
    key = _h(secret)
    for _ in range(2):
        p = multiprocessing.get_context("spawn").Process(target=_reserve_then_die, args=(env.runner_dsn, key, "after_commit"))
        p.start(); p.join(60)
        assert p.exitcode == 17
    assert _rows(env, key)["api_football_quota_leases"] == 2  # durable: both orphans hold the 2 slots
    from bridge.api_football.quota import QuotaConcurrencyBusy
    with pytest.raises(QuotaConcurrencyBusy):
        asyncio.run(_runner_q(env).acquire(secret))
    with pytest.raises(QuotaConcurrencyBusy):
        asyncio.run(_bridge(env).acquire(secret))
    with env.su() as su:  # simulate the 180 s lease TTL elapsing (clock), not a privilege the runner has
        su.execute("UPDATE public.api_football_quota_leases SET expires_at = clock_timestamp() - interval '1 second' "
                   "WHERE api_key_hash = %s", (key,))
    lease = asyncio.run(_runner_q(env).acquire(secret))  # the next reserve prunes both expired leases
    assert _rows(env, key)["api_football_quota_leases"] == 1
    asyncio.run(_runner_q(env).release(lease))
    assert _rows(env, key)["api_football_quota_leases"] == 0


def test_the_lease_ttl_is_pinned_to_180_seconds(env):
    secret = "R1-TTL-" + uuid.uuid4().hex
    lease = asyncio.run(_runner_q(env).acquire(secret))
    with env.su() as su:
        ttl = su.execute("SELECT EXTRACT(EPOCH FROM expires_at - a.created_at)::int AS s FROM public.api_football_quota_leases l "
                         "JOIN public.api_football_quota_attempts a ON a.attempt_id = l.lease_id WHERE l.lease_id = %s",
                         (lease.lease_id,)).fetchone()["s"]
    assert ttl == 180
    asyncio.run(_runner_q(env).release(lease))


# ------------------------------------------------------------------ exact state: tamper -> refused

TAMPER = {
    "extra_function": ["CREATE FUNCTION fcb_cert_quota.extra() RETURNS int LANGUAGE sql AS 'select 1'"],
    "changed_body": ["CREATE OR REPLACE FUNCTION fcb_cert_quota.release(p_key_hash text, p_lease_id text) RETURNS void "
                     "LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$BEGIN END$$"],
    "security_invoker": ["ALTER FUNCTION fcb_cert_quota.reserve(text) SECURITY INVOKER"],
    "search_path_reset": ["ALTER FUNCTION fcb_cert_quota.reserve(text) RESET search_path"],
    "search_path_changed": ["ALTER FUNCTION fcb_cert_quota.reserve(text) SET search_path = public, pg_temp"],
    "execute_public": ["GRANT EXECUTE ON FUNCTION fcb_cert_quota.snapshot(text) TO PUBLIC"],
    "execute_other_role": [f"GRANT EXECUTE ON FUNCTION fcb_cert_quota.reserve(text) TO {OTHER}"],
    "execute_grant_option": [f"GRANT EXECUTE ON FUNCTION fcb_cert_quota.reserve(text) TO {RUNNER} WITH GRANT OPTION"],
    "table_privilege": [f"GRANT SELECT ON public.api_football_quota_counters TO {RUNNER}"],
    "table_privilege_public": ["GRANT SELECT ON public.api_football_quota_leases TO PUBLIC"],
    "column_privilege": [f"GRANT UPDATE (used) ON public.api_football_quota_counters TO {RUNNER}"],
    "function_owner": [f"ALTER FUNCTION fcb_cert_quota.reserve(text) OWNER TO {OTHER}"],
    "schema_owner": [f"ALTER SCHEMA fcb_cert_quota OWNER TO {OTHER}"],
    "schema_create_privilege": [f"GRANT CREATE ON SCHEMA fcb_cert_quota TO {RUNNER}"],
    "schema_usage_public": ["GRANT USAGE ON SCHEMA fcb_cert_quota TO PUBLIC"],
    "extra_table": ["CREATE TABLE fcb_cert_quota.t (a int)"],
    "extra_type": ["CREATE TYPE fcb_cert_quota.e AS ENUM ('a')"],
    "default_acl": [f"ALTER DEFAULT PRIVILEGES FOR ROLE {ADMIN} IN SCHEMA fcb_cert_quota GRANT EXECUTE ON FUNCTIONS TO {OTHER}"],
    "runner_not_plain": [f"ALTER ROLE {RUNNER} CREATEDB"],
    "runner_member": [f"GRANT {OTHER} TO {RUNNER}"],
    "runner_public_create": [f"GRANT CREATE ON SCHEMA public TO {RUNNER}"],
    "strict": ["ALTER FUNCTION fcb_cert_quota.snapshot(text) STRICT"],
}


@pytest.mark.parametrize("tamper", sorted(TAMPER))
def test_any_drift_of_the_exact_state_is_refused_by_the_assert(env, tamper):
    with env.su() as su:
        su.autocommit = False
        try:
            for statement in TAMPER[tamper]:
                su.execute(statement)
            assert su.execute(batch.quota_state_sql()).fetchone()["state"] == "DRIFT", tamper
            with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PATH_A_QUOTA_STATE_UNEXPECTED"):
                su.execute(path_a._sql("apifootball_cert_path_a_quota_assert.sql"))
        finally:
            su.rollback()
    assert _state(env) == "EXACT"


def test_a_committed_drift_stops_the_runner_before_any_provider_request(env, monkeypatch):
    """The runner's coordinator refuses a drifted variant in prepare(): QUOTA_COORDINATOR_UNAVAILABLE,
    NOTHING sent, NOTHING reserved in the ledger (fake ledger), whatever the drift."""
    monkeypatch.setenv(batch.AUTH_VAR, "true")
    from apifootball_campaign_kit import FakeLedger, Store
    import httpx
    sent = []

    def handler(request):
        sent.append(request.url.path)
        return Provider().default(request)
    with env.admin() as conn:
        conn.execute("ALTER FUNCTION fcb_cert_quota.reserve(text) SECURITY INVOKER")
    try:
        assert _state(env) == "DRIFT"
        store = Store()
        plan = json.loads(batch.render_plan(batch.build_plan()))
        plan["calls"] = plan["calls"][:2]
        run = asyncio.run(batch.execute(plan, campaign_id="camp-r1-drift-" + uuid.uuid4().hex[:6], settings=settings(),
                                        ledger=FakeLedger(store), inner_transport=httpx.MockTransport(handler),
                                        quota=_runner_q(env), plan_text=json.dumps(plan), source_sha="x"))
    finally:
        path_a.install_quota_functions(env.admin)  # the owner's step 1b restores the exact state
    assert sent == [] and store.attempts == []
    assert all(c["outcome"] == "REFUSED_BEFORE_SEND" for c in run["calls"]), run["calls"]
    assert {c.get("refusal_code") for c in run["calls"]} <= {"QUOTA_COORDINATOR_UNAVAILABLE", "CIRCUIT_OPEN"}
    assert _state(env) == "EXACT"


def test_runner_main_preflight_refuses_a_drifted_variant(env, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(batch.AUTH_VAR, "true")
    monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    monkeypatch.setenv("APIFOOTBALL_API_KEY", "R1-MAIN-" + uuid.uuid4().hex)
    monkeypatch.setenv("APIFOOTBALL_LIVE_AUTHORIZED", "true")
    monkeypatch.setenv("DATABASE_URL", env.runner_dsn)
    with env.admin() as conn:
        conn.execute(f"GRANT EXECUTE ON FUNCTION fcb_cert_quota.reserve(text) TO {OTHER}")
    try:
        rc = batch.main(["--execute", "--authorize-live-campaign", "--campaign-id", "camp-r1-main-0001", "--out", str(tmp_path)])
    finally:
        with env.admin() as conn:
            conn.execute(f"REVOKE EXECUTE ON FUNCTION fcb_cert_quota.reserve(text) FROM {OTHER}")
    assert rc == 2 and "CAMPAIGN_REFUSED COORDINATOR_FUNCTIONS_NOT_EXACT" in capsys.readouterr().out
    assert _state(env) == "EXACT"
