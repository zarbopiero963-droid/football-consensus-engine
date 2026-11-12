"""PATH A (owner mandate FCB-OWNER-APIFOOTBALL-PATH-A-20261112) on a REAL PostgreSQL 18 server.

An Aiven-like cluster is built here from a throwaway SUPERUSER DSN (PATH_A_TEST_SUPERUSER_DSN, TCP,
SCRAM; never a production server): a NON-superuser administrator (CREATEROLE, CREATEDB, BYPASSRLS,
member of a group, createrole_self_grant = 'inherit,set' like Aiven) that owns `defaultdb` with bridge
0001-0008 and data. Then PATH A is installed AS that administrator (scripts/apifootball_cert_path_a.py)
and every check below uses REAL SCRAM logins (require_auth=scram-sha-256): the runner is
fcb_cert_runner, the administrator stands for the Render runtime identity (it uses the admin DSN).
Covers INV-1 (approved immutable plans), INV-2 (atomic per-plan budget: 9 processes at RC / RR /
SERIALIZABLE / runner path, the original stale-snapshot defect, kills and rollbacks, counter ==
committed), INV-3 (append-only evidence, RESERVED_UNKNOWN, fail-closed), the runner's least privilege
(no OAuth / SX Bet / other table), the coordinator as the runner, the bridge unchanged with the admin
DSN, and what the administrator must do DELIBERATELY to bypass (and what the runner then detects).
Provider = httpx.MockTransport; no network.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import sys
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
from apifootball_campaign_kit import KEY, Provider, Quota, load, settings  # noqa: E402

batch = load("apifootball_cert_batch1_path_a", "apifootball_cert_batch1.py")
path_a = load("apifootball_cert_path_a_mod", "apifootball_cert_path_a.py")
ADMIN = "fcbpa_avnadmin"
GROUP = "fcbpa_avnadmin_group"
OWNER, RUNNER = "fcb_cert_owner", "fcb_cert_runner"
LEDGER_TABLES = ("api_football_cert_plans", "api_football_cert_campaigns", "api_football_cert_campaign_attempts",
                 "api_football_cert_campaign_outcomes")
WRITABLE = {"api_football_cert_plans": (), "api_football_cert_campaigns": ("campaign_id", "plan_sha256"),
            "api_football_cert_campaign_attempts": ("campaign_id", "call_id"),
            "api_football_cert_campaign_outcomes": ("campaign_id", "seq", "outcome", "http_status")}
COORDINATOR = {"api_football_quota_counters", "api_football_quota_leases", "api_football_quota_attempts"}


def _dsn(user, password, dbname, require=True):
    params = conninfo_to_dict(SU_DSN)
    params.update(user=user, password=password, dbname=dbname)
    if require:
        params["require_auth"] = "scram-sha-256"
    return make_conninfo(**params)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from app import migrations
    from fcb_db_roles import scram_verifier
    admin_pw, runner_pw = "adm-" + uuid.uuid4().hex, "run-" + uuid.uuid4().hex + uuid.uuid4().hex
    with psycopg.connect(SU_DSN, autocommit=True) as su:
        if su.execute("SELECT 1 FROM pg_roles WHERE rolname = ANY(%s) UNION ALL SELECT 1 FROM pg_database "
                      "WHERE datname IN ('defaultdb', 'fcb_cert')", ([ADMIN, GROUP, OWNER, RUNNER],)).fetchone():
            pytest.skip("PATH A names already exist on this server; not touching them")
        su.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(GROUP)))
        su.execute(SQL("CREATE ROLE {} LOGIN CREATEROLE CREATEDB BYPASSRLS PASSWORD {}").format(
            Identifier(ADMIN), Literal(admin_pw)))
        su.execute(SQL("GRANT {} TO {}").format(Identifier(GROUP), Identifier(ADMIN)))
        su.execute(SQL("ALTER ROLE {} SET createrole_self_grant = 'inherit,set'").format(Identifier(ADMIN)))
        su.execute(SQL("CREATE DATABASE defaultdb OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(Identifier(ADMIN)))
    ns = SimpleNamespace(
        admin=lambda db="defaultdb", **kw: psycopg.connect(_dsn(ADMIN, admin_pw, db), row_factory=dict_row, **kw),
        runner_dsn=lambda db="fcb_cert": _dsn(RUNNER, runner_pw, db),
        runner=lambda db="fcb_cert": psycopg.connect(_dsn(RUNNER, runner_pw, db), row_factory=dict_row,
                                                     options="-c lock_timeout=5000"),
        su=lambda db: psycopg.connect(make_conninfo(SU_DSN, dbname=db), row_factory=dict_row, autocommit=True),
        admin_pw=admin_pw, runner_pw=runner_pw)
    try:
        upto8 = tmp_path_factory.mktemp("upto8")
        for m in migrations.load_migrations()[:8]:
            (upto8 / m.name).write_bytes((migrations.BRIDGE.directory / m.name).read_bytes())
        migrations.apply_migrations(ns.admin, upto8)  # the administrator owns bridge 0001-0008
        with ns.admin() as conn:
            conn.execute("INSERT INTO api_football_quota_counters VALUES ('seed', 'day', 1, 7)")
            conn.execute("INSERT INTO sxbet_rate_cooldowns SELECT * FROM sxbet_rate_cooldowns LIMIT 0")
        ns.before = _defaultdb_fingerprint(ns)
        path_a.install_roles(ns.admin, scram_verifier(runner_pw))
        ns.db_state = path_a.create_database(lambda: ns.admin(autocommit=True))
        path_a.install_ledger(lambda: ns.admin("fcb_cert"))
        yield ns
    finally:
        with psycopg.connect(SU_DSN, autocommit=True) as su:
            su.execute("DROP DATABASE IF EXISTS fcb_cert WITH (FORCE)")
            su.execute("DROP DATABASE IF EXISTS defaultdb WITH (FORCE)")
            for role in (RUNNER, OWNER, ADMIN, GROUP):
                su.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))


def _defaultdb_fingerprint(ns):
    with ns.admin() as conn:
        return conn.execute(
            "SELECT (SELECT string_agg(c.relname || ':' || c.relowner::regrole::text, ',' ORDER BY c.relname) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r') AS owners, "
            "(SELECT string_agg(version || name || sha256 || applied_at, ',' ORDER BY version) FROM schema_migrations) AS rows, "
            "(SELECT used FROM api_football_quota_counters WHERE api_key_hash = 'seed') AS seed").fetchone()


def _approve(ns, budget, tag, kind="BATCH1", sha=None, run_id="4242"):
    sha = sha or hashlib.sha256(f"path-a-{budget}-{tag}-{uuid.uuid4().hex}".encode()).hexdigest()
    path_a.approve_plan(lambda: ns.admin("fcb_cert"), sha, kind, budget, "owner-test", run_id)
    return sha


def _counter_matches(ns, plan, expected=None):
    """counter == committed attempts of the plan, seqs exactly 1..used, never above the budget
    (read with the superuser: neither the runner nor the administrator can read the counter)."""
    with ns.su("fcb_cert") as conn:
        used = conn.execute("SELECT used FROM api_football_cert_plan_usage WHERE plan_sha256 = %s", (plan,)).fetchone()["used"]
        budget = conn.execute("SELECT budget FROM api_football_cert_plans WHERE plan_sha256 = %s", (plan,)).fetchone()["budget"]
        seqs = [r["seq"] for r in conn.execute(
            "SELECT a.seq FROM api_football_cert_campaign_attempts a JOIN api_football_cert_campaigns c USING (campaign_id) "
            "WHERE c.plan_sha256 = %s ORDER BY a.seq", (plan,))]
    assert used == len(seqs) and seqs == list(range(1, used + 1)) and used <= budget, (used, seqs, budget)
    if expected is not None:
        assert used == expected
    return used


def _ledger(ns):
    return batch.PostgresCampaignLedger(ns.runner, schema="public", owner_role=OWNER)


@pytest.fixture(autouse=True)
def owner_authorized(monkeypatch):
    monkeypatch.setenv(batch.AUTH_VAR, "true")


# ------------------------------------------------------------------ install and end state

def test_install_is_idempotent_and_changes_no_existing_object(env):
    from fcb_db_roles import scram_verifier
    assert env.db_state == "CREATED"
    after = _defaultdb_fingerprint(env)
    assert after == env.before  # 33 tables still owned by the administrator, 0001-0008 rows, data
    assert after["owners"].count(":" + ADMIN) == 33 and after["rows"].count(".sql") == 8 and after["seed"] == 7
    with env.su("fcb_cert") as conn:
        catalog = conn.execute("SELECT string_agg(c.relname || c.relacl::text, ',' ORDER BY c.relname) AS a "
                               "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace").fetchone()
    path_a.install_roles(env.admin, scram_verifier(env.runner_pw))  # second run: no-op
    assert path_a.create_database(lambda: env.admin(autocommit=True)) == "ALREADY_PRESENT"
    path_a.install_ledger(lambda: env.admin("fcb_cert"))
    with env.su("fcb_cert") as conn:
        assert conn.execute("SELECT string_agg(c.relname || c.relacl::text, ',' ORDER BY c.relname) AS a "
                            "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace").fetchone() == catalog
        assert [tuple(r.values()) for r in conn.execute(
            "SELECT version, name, sha256 FROM apifootball_cert_schema_migrations")] == [
            (10, "0010_apifootball_cert_campaign_ledger.sql", path_a.LEDGER_MIGRATION_SHA256)]
        # the administrator keeps ONLY the automatic ADMIN-only grant on the two roles it created
        rows = conn.execute("SELECT r.rolname AS role, m.rolname AS member, a.admin_option, a.inherit_option, a.set_option "
                            "FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member "
                            "WHERE r.rolname IN (%s, %s) ORDER BY 1", (OWNER, RUNNER)).fetchall()
    assert rows == [{"role": OWNER, "member": ADMIN, "admin_option": True, "inherit_option": False, "set_option": False},
                    {"role": RUNNER, "member": ADMIN, "admin_option": True, "inherit_option": False, "set_option": False}]
    with env.admin() as conn:  # the bridge series of defaultdb is untouched: no 0009, no 0010
        assert [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")] == list(range(1, 9))


def test_installer_refuses_wrong_database_and_bad_verifier(env):
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PATH_A_ROLES_GUARD_FAILED"):
        path_a.install_roles(env.admin, "not-a-verifier")
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PATH_A_LEDGER_GUARD_FAILED"):
        with env.admin("defaultdb") as conn:  # the ledger part never runs outside fcb_cert
            conn.execute((ROOT / "sql/aiven/apifootball_cert_path_a_ledger_pre.sql").read_text())


def test_logins_are_scram_and_the_owner_cannot_log_in(env):
    with env.runner() as conn:
        assert conn.info.user == RUNNER and conn.pgconn.used_password
        assert conn.execute("SELECT current_user = session_user AS ok").fetchone()["ok"]
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_dsn(RUNNER, "wrong-" + uuid.uuid4().hex, "fcb_cert")).close()
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_dsn(OWNER, env.runner_pw, "fcb_cert", require=False)).close()
    with pytest.raises(psycopg.OperationalError):  # PUBLIC has no CONNECT on fcb_cert
        with env.su("postgres") as su:
            su.execute("CREATE ROLE fcbpa_other LOGIN PASSWORD 'other-pw-123'")
        try:
            psycopg.connect(_dsn("fcbpa_other", "other-pw-123", "fcb_cert")).close()
        finally:
            with env.su("postgres") as su:
                su.execute("DROP ROLE fcbpa_other")


# ------------------------------------------------------------------ runner least privilege

def test_runner_cannot_touch_any_defaultdb_table_but_the_coordinator(env):
    with env.admin() as conn:
        tables = [r["relname"] for r in conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' ORDER BY 1")]
    assert len(tables) == 33 and COORDINATOR <= set(tables)
    refused = 0
    for t in tables:
        statements = [f"UPDATE {t} SET {{c}} = {{c}}", f"TRUNCATE {t}", f"ALTER TABLE {t} ADD COLUMN x int",
                      f"DROP TABLE {t}"]
        if t not in COORDINATOR:
            statements += [f"SELECT * FROM {t}", f"DELETE FROM {t}", f"INSERT INTO {t} DEFAULT VALUES"]
        for statement in statements:
            with env.runner("defaultdb") as conn:
                col = conn.execute("SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum = 1",
                                   (t,)).fetchone()["attname"]
                if t == "api_football_quota_counters" and statement.startswith("UPDATE"):
                    col = "api_key_hash"  # only `used` is updatable
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement.format(c=col))
                refused += 1
    for statement in ("CREATE TABLE public.x (a int)", "CREATE SCHEMA x", "SET ROLE " + ADMIN, "SET ROLE " + OWNER,
                      "CREATE FUNCTION public.f() RETURNS int LANGUAGE sql AS 'select 1'"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with env.runner("defaultdb") as conn:
                conn.execute(statement)
        refused += 1
    assert refused == 30 * 7 + 3 * 4 + 5


def test_the_real_coordinator_works_as_the_runner_and_still_enforces_the_day(env):
    from bridge.api_football.quota import PostgresQuota, QuotaExceeded, QuotaLimits
    quota = PostgresQuota(QuotaLimits(10, 2, 1), connect=lambda: env.runner("defaultdb"))
    key = "PATH-A-KEY-" + uuid.uuid4().hex

    async def go():
        await quota.prepare()
        lease = await quota.acquire(key)
        await quota.release(lease)
        await quota.mark_exhausted(key + "x", "minute")
        lease = await quota.acquire(key)
        await quota.release(lease)
        with pytest.raises(QuotaExceeded):  # day budget 2 reached: the shared counter refuses
            await quota.acquire(key)
        return await quota.snapshot(key)
    assert asyncio.run(go())["remaining_day"] == 0


def test_runner_ledger_privileges_are_select_and_column_insert_only(env):
    with env.runner() as conn:
        for t in LEDGER_TABLES:
            privs = {p: conn.execute("SELECT has_table_privilege(%s, %s, %s) AS ok", (RUNNER, t, p)).fetchone()["ok"]
                     for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")}
            cols = tuple(r["attname"] for r in conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped "
                "AND has_column_privilege(%s, attrelid, attnum, 'INSERT') ORDER BY attnum", (t, RUNNER)))
            assert privs == {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False, "TRUNCATE": False,
                             "REFERENCES": False, "TRIGGER": False} and cols == WRITABLE[t], t


def test_runner_cannot_change_the_ledger_the_counter_or_the_guards(env):
    plan = _approve(env, 3, "guards")
    with env.runner() as conn:
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES ('camp-guard-0001', %s)", (plan,))
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) VALUES ('camp-guard-0001', 'C1')")
    statements = ["UPDATE api_football_cert_plans SET budget = 78", "DELETE FROM api_football_cert_plans",
                  f"INSERT INTO api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id) "
                  f"VALUES ('{'d' * 64}', 'BATCH1', 78, 'x', 1)",
                  "UPDATE api_football_cert_campaigns SET plan_sha256 = plan_sha256",
                  "UPDATE api_football_cert_campaign_attempts SET call_id = 'X'",
                  "DELETE FROM api_football_cert_campaign_attempts", "DELETE FROM api_football_cert_campaign_outcomes",
                  "TRUNCATE api_football_cert_campaign_attempts", "TRUNCATE api_football_cert_campaigns CASCADE",
                  "INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-guard-0001', 1, 'X')",
                  "INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id, reserved_at) "
                  "VALUES ('camp-guard-0001', 'X', '2000-01-01')",
                  "SELECT * FROM api_football_cert_plan_usage", "UPDATE api_football_cert_plan_usage SET used = 0",
                  "DELETE FROM api_football_cert_plan_usage", "SELECT * FROM apifootball_cert_schema_migrations",
                  "ALTER TABLE api_football_cert_campaign_attempts DISABLE TRIGGER ALL",
                  "ALTER TABLE api_football_cert_plan_usage DISABLE TRIGGER ALL",
                  "DROP TABLE api_football_cert_campaign_outcomes",
                  "DROP TRIGGER api_football_cert_attempts_immutable ON api_football_cert_campaign_attempts",
                  "SELECT public.api_football_cert_attempt_guard()", "SELECT public.api_football_cert_plan_usage_rule()",
                  "SET session_replication_role = replica", "SET ROLE " + OWNER, "SET SESSION AUTHORIZATION " + OWNER,
                  "CREATE TABLE public.x (a int)", "CREATE TEMP TABLE x (a int)", "DROP DATABASE fcb_cert",
                  "ALTER DATABASE fcb_cert OWNER TO " + RUNNER]
    for statement in statements:
        with pytest.raises(psycopg.Error) as exc:
            with env.runner() as conn:
                conn.execute(statement)
        assert exc.value.sqlstate in ("42501", "55006", "25001"), (statement, exc.value.sqlstate)
    assert len(statements) == 28
    _counter_matches(env, plan, 1)


def test_pg_temp_shadows_cannot_lift_the_budget(env):
    plan = _approve(env, 1, "shadow")
    with env.runner() as conn:
        conn.execute("INSERT INTO public.api_football_cert_campaigns (campaign_id, plan_sha256) VALUES ('camp-shadow-001', %s)", (plan,))
        conn.execute("INSERT INTO public.api_football_cert_campaign_attempts (campaign_id, call_id) VALUES ('camp-shadow-001', 'C1')")
    # PUBLIC has no TEMP on fcb_cert: a shadow cannot even be created there
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with env.runner() as conn:
            conn.execute("CREATE TEMP TABLE api_football_cert_plans (plan_sha256 text, budget int)")
    with pytest.raises(psycopg.errors.RaiseException, match="BUDGET_EXHAUSTED"):
        with env.runner() as conn:
            conn.execute("SET search_path = pg_temp, public")
            conn.execute("INSERT INTO public.api_football_cert_campaign_attempts (campaign_id, call_id) VALUES ('camp-shadow-001', 'C2')")
    _counter_matches(env, plan, 1)


# ------------------------------------------------------------------ INV-1: approved, immutable plans

def test_inv1_plans_are_approved_once_never_renewed_and_only_through_the_approval(env):
    sha = _approve(env, 78, "inv1")
    _approve(env, 78, "inv1", sha=sha, run_id="4343")  # same plan again: no-op, no new budget
    with pytest.raises(psycopg.errors.RaiseException, match="FCB_CERT_PLAN_CONFLICT"):
        _approve(env, 77, "inv1", sha=sha)  # a different budget for the same sha
    for bad in ({"kind": "OTHER"}, {"budget": 0}, {"budget": 79}, {"run_id": "0"}, {"sha": "A" * 64}):
        kw = {"kind": "BATCH1", "budget": 5, "sha": "f" * 64, "run_id": "1"} | bad
        with pytest.raises(psycopg.Error):
            path_a.approve_plan(lambda: env.admin("fcb_cert"), kw["sha"], kw["kind"], kw["budget"], "owner", kw["run_id"])
    with pytest.raises(psycopg.errors.CheckViolation):  # snapshot plans: at most 8
        _approve(env, 9, "snap", kind="LIVE_FULL_MATCH_SNAPSHOT")
    with env.su("fcb_cert") as conn:
        row = conn.execute("SELECT budget, github_run_id, approved_role FROM api_football_cert_plans WHERE plan_sha256 = %s",
                           (sha,)).fetchone()
        assert row == {"budget": 78, "github_run_id": 4242, "approved_role": ADMIN}
        # even the owner cannot change or remove an approved plan (append-only triggers)
        for statement in ("UPDATE api_football_cert_plans SET budget = 1", "DELETE FROM api_football_cert_plans",
                          "TRUNCATE api_football_cert_plans CASCADE"):
            with pytest.raises(psycopg.errors.RaiseException, match="APPEND_ONLY"):
                with conn.transaction():
                    conn.execute(f"SET LOCAL ROLE {OWNER}")
                    conn.execute(statement)
    with env.admin("fcb_cert") as conn:  # the approval left the administrator without SET on the owner
        assert conn.execute("SELECT pg_has_role(%s, 'SET') AS s, pg_has_role(%s, 'USAGE') AS u", (OWNER, OWNER)).fetchone() == {
            "s": False, "u": False}


def test_inv1_runner_binds_only_the_committed_approved_plan_and_sends_nothing_otherwise(env):
    plan_text = batch.render_plan(batch.build_plan())
    provider = Provider()
    with pytest.raises(batch.LedgerUnavailable, match="PLAN_NOT_APPROVED"):
        asyncio.run(batch.execute(json.loads(plan_text), campaign_id="camp-unapproved-1", settings=settings(),
                                  ledger=_ledger(env), inner_transport=provider.transport(), quota=Quota(),
                                  plan_text=plan_text, source_sha="x"))
    assert provider.sent == []


# ------------------------------------------------------------------ INV-2: atomic per-plan budget

def _iso_worker(dsn, campaign, plan, level, out):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from apifootball_campaign_kit import load as _load
    mod = _load("apifootball_cert_batch1_pa_iso", "apifootball_cert_batch1.py")
    os.environ[mod.AUTH_VAR] = "true"
    granted = conflicts = 0
    if level == "RUNNER":
        ledger = mod.PostgresCampaignLedger(lambda: psycopg.connect(dsn, row_factory=dict_row), schema="public",
                                            owner_role="fcb_cert_owner")
        asyncio.run(ledger.open(campaign, 78, plan))
        for i in range(20):
            try:
                asyncio.run(ledger.reserve(campaign, f"R{i}"))
                granted += 1
            except mod.SerializationRefused:
                conflicts += 1
            except mod.BudgetExhausted:
                break
        out.put((granted, conflicts))
        return
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES (%s, %s) "
                     "ON CONFLICT DO NOTHING", (campaign, plan))
    for i in range(60):
        conn = psycopg.connect(dsn)
        conn.isolation_level = getattr(psycopg.IsolationLevel, level.replace(" ", "_"))
        try:
            conn.execute("SELECT count(*) FROM api_football_cert_campaign_attempts")  # snapshot first
            conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) VALUES (%s, %s)",
                         (campaign, f"I{i}"))
            conn.commit()
            granted += 1
        except psycopg.Error as exc:
            conn.rollback()
            if exc.sqlstate in ("40001", "40P01"):
                conflicts += 1
                continue
            if "BUDGET_EXHAUSTED" in str(exc):
                break
            raise
        finally:
            conn.close()
    out.put((granted, conflicts))


@pytest.mark.parametrize("level", ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE", "RUNNER"])
def test_inv2_nine_runner_processes_never_exceed_the_plan_budget_at_any_isolation(env, level):
    plan = _approve(env, 7, "iso-" + level)
    if level == "RUNNER":  # the runner's own role default is hostile; the ledger forces READ COMMITTED
        with env.su("postgres") as su:
            su.execute(f"ALTER ROLE {RUNNER} SET default_transaction_isolation = 'repeatable read'")
    try:
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        tag = uuid.uuid4().hex[:8]
        procs = [ctx.Process(target=_iso_worker, args=(env.runner_dsn(), f"camp-iso-{tag}-{i:04d}", plan,
                                                       level, out)) for i in range(9)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(240)
        assert [p.exitcode for p in procs] == [0] * 9
        assert sum(out.get(timeout=5)[0] for _ in procs) == 7
    finally:
        with env.su("postgres") as su:
            su.execute(f"ALTER ROLE {RUNNER} RESET default_transaction_isolation")
    _counter_matches(env, plan, 7)


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_inv2_original_stale_snapshot_defect_is_a_serialization_failure_not_an_overrun(env, level):
    plan = _approve(env, 2, "defect-" + level)
    tag = uuid.uuid4().hex[:8]
    t1c, t2c = f"camp-t1-{tag}", f"camp-t2-{tag}"
    with env.runner() as conn:
        conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES (%s, %s), (%s, %s)",
                     (t1c, plan, t2c, plan))
        conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) VALUES (%s, 'A')", (t2c,))
    t1 = env.runner()
    t1.execute(f"BEGIN ISOLATION LEVEL {level}")
    t1.execute("SELECT count(*) FROM api_football_cert_campaign_attempts")
    with env.runner() as t2:
        t2.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) VALUES (%s, 'B')", (t2c,))
    with pytest.raises(psycopg.errors.SerializationFailure):
        t1.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) VALUES (%s, 'X')", (t1c,))
    t1.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="API_FOOTBALL_CERT_CAMPAIGN_BUDGET_EXHAUSTED"):
        t1.execute(f"BEGIN ISOLATION LEVEL {level}")
        t1.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) VALUES (%s, 'X')", (t1c,))
    t1.rollback()
    t1.close()
    _counter_matches(env, plan, 2)


def _plan_worker(dsn, campaign, plan, mode, out):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from apifootball_campaign_kit import load as _load
    mod = _load("apifootball_cert_batch1_pa_plan", "apifootball_cert_batch1.py")
    os.environ[mod.AUTH_VAR] = "true"

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row)
    ledger = mod.PostgresCampaignLedger(connect, schema="public", owner_role="fcb_cert_owner")
    asyncio.run(ledger.open(campaign, 78, plan))
    got = 0
    for i in range(80):
        if mode == "abort":
            conn = connect()
            try:
                conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) VALUES (%s, 'ABORTED')",
                             (campaign,))
            except psycopg.Error:
                pass
            conn.rollback()
            conn.close()
        try:
            asyncio.run(ledger.reserve(campaign, f"P{i}"))
            got += 1
        except mod.BudgetExhausted:
            break
        if mode == "kill" and got == 3:
            os._exit(9)
        if mode == "kill_uncommitted" and got == 2:
            conn = connect()
            conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) VALUES (%s, 'UNCOMMITTED')",
                         (campaign,))
            os._exit(9)
    out.put(got)


def test_inv2_kills_rollbacks_restarts_and_new_campaign_ids_never_renew_the_budget(env):
    plan = _approve(env, 25, "kills")
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()

    def spawn(modes, offset):
        procs = [ctx.Process(target=_plan_worker, args=(env.runner_dsn(), f"camp-kill-{offset + i:04d}", plan, mode, out))
                 for i, mode in enumerate(modes)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(180)
        return [p.exitcode for p in procs]
    assert spawn(["kill", "kill", "kill_uncommitted"], 0) == [9, 9, 9]
    _counter_matches(env, plan, 8)  # committed reservations stay; the uncommitted one is gone with its increment
    assert spawn(["normal", "abort", "normal", "abort", "normal", "normal"], 10) == [0] * 6
    assert sum(out.get(timeout=5) for _ in range(6)) == 25 - 8
    _counter_matches(env, plan, 25)
    ledger = _ledger(env)
    for campaign in ("camp-kill-0002", "camp-kill-new-01"):  # restart and a new campaign_id
        asyncio.run(ledger.open(campaign, 78, plan))
        with pytest.raises(batch.BudgetExhausted):
            asyncio.run(ledger.reserve(campaign, "AGAIN"))
    with env.runner() as conn:
        assert conn.execute("SELECT count(*) AS n FROM api_football_cert_campaign_attempts "
                            "WHERE call_id IN ('UNCOMMITTED', 'ABORTED')").fetchone()["n"] == 0
    _counter_matches(env, plan, 25)


# ------------------------------------------------------------------ INV-3: evidence, RESERVED_UNKNOWN, fail-closed

def _crash_worker(runner_dsn, coordinator_dsn, plan_sha, where, marker):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    import httpx as _httpx
    from apifootball_campaign_kit import Provider as _Provider, load as _load, settings as _settings
    from bridge.api_football.quota import PostgresQuota, QuotaLimits
    mod = _load("apifootball_cert_batch1_pa_crash", "apifootball_cert_batch1.py")
    os.environ[mod.AUTH_VAR] = "true"

    class DiesBeforeOutcome(mod.PostgresCampaignLedger):
        async def record_outcome(self, *args):
            os._exit(17)

    def handler(request):
        if where == "between_reservation_and_send":
            os._exit(17)
        Path(marker).write_text("sent", encoding="utf-8")
        return _Provider().default(request)
    cls = DiesBeforeOutcome if where == "between_send_and_outcome" else mod.PostgresCampaignLedger
    ledger = cls(lambda: psycopg.connect(runner_dsn, row_factory=dict_row), schema="public", owner_role="fcb_cert_owner")
    quota = PostgresQuota(QuotaLimits(100, 1000, 8), connect=lambda: psycopg.connect(coordinator_dsn, row_factory=dict_row))
    plan_text = mod.render_plan(mod.build_plan())
    asyncio.run(mod.execute(json.loads(plan_text), campaign_id="camp-crash-" + where.split("_")[1][:4], settings=_settings(),
                            ledger=ledger, inner_transport=_httpx.MockTransport(handler), quota=quota,
                            plan_text=plan_text, source_sha="x"))
    os._exit(0)


@pytest.mark.parametrize("where", ["between_reservation_and_send", "between_send_and_outcome"])
def test_inv3_killed_runner_full_stack_reserved_unknown_counted_and_never_retried(env, tmp_path, where):
    """The real runner, the real PostgresQuota (defaultdb) and the real ledger (fcb_cert), both as
    fcb_cert_runner. The batch-1 plan is approved once (78) and shared by both parametrizations."""
    from bridge.api_football.quota import PostgresQuota, QuotaLimits
    plan_text = batch.render_plan(batch.build_plan())
    sha = batch.plan_sha256(plan_text)
    _approve(env, 78, "batch1", sha=sha)
    marker = tmp_path / "sent.txt"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_crash_worker, args=(env.runner_dsn(), env.runner_dsn("defaultdb"), sha, where, str(marker)))
    proc.start()
    proc.join(120)
    assert proc.exitcode == 17 and marker.exists() == (where == "between_send_and_outcome")
    campaign = "camp-crash-" + where.split("_")[1][:4]
    first = json.loads(plan_text)["calls"][0]["call_id"]
    ledger = _ledger(env)
    assert asyncio.run(ledger.attempt_states(campaign)) == [
        {"seq": asyncio.run(ledger.attempt_states(campaign))[0]["seq"], "call_id": first, "state": "RESERVED_UNKNOWN",
         "http_status": None}]
    again = Provider()
    quota = PostgresQuota(QuotaLimits(1000, 100000, 8), connect=lambda: env.runner("defaultdb"))
    run = asyncio.run(batch.execute(json.loads(plan_text), campaign_id=campaign, settings=settings(), ledger=ledger,
                                    inner_transport=again.transport(), quota=quota, plan_text=plan_text, source_sha="x"))
    assert next(c for c in run["calls"] if c["call_id"] == first)["ledger_state"] == "RESERVED_UNKNOWN"
    assert run["ledger_attempt_states"]["RESERVED_UNKNOWN"] == 1
    assert run["plan_budget"]["used"] <= 78
    _counter_matches(env, sha)


def test_inv3_evidence_is_append_only_even_for_the_owner(env):
    plan = _approve(env, 5, "evidence")
    ledger = _ledger(env)
    asyncio.run(ledger.open("camp-evid-00001", 78, plan))
    seq = asyncio.run(ledger.reserve("camp-evid-00001", "C1"))
    asyncio.run(ledger.reserve("camp-evid-00001", "C2"))
    asyncio.run(ledger.record_outcome("camp-evid-00001", seq, "HTTP_RESPONSE", 200))
    asyncio.run(ledger.record_outcome("camp-evid-00001", seq, "TRANSPORT_ERROR", None))  # first stays
    assert [s["state"] for s in asyncio.run(ledger.attempt_states("camp-evid-00001"))] == [
        "RESPONSE_RECEIVED", "RESERVED_UNKNOWN"]
    with env.su("fcb_cert") as conn:
        for statement in ("DELETE FROM api_football_cert_campaign_attempts WHERE seq = 2",
                          "UPDATE api_football_cert_campaign_outcomes SET http_status = 500",
                          "TRUNCATE api_football_cert_campaign_outcomes",
                          "UPDATE api_football_cert_plan_usage SET used = 0",
                          "DELETE FROM apifootball_cert_schema_migrations"):
            with pytest.raises(psycopg.errors.RaiseException):
                with conn.transaction():
                    conn.execute(f"SET LOCAL ROLE {OWNER}")
                    conn.execute(statement)
    _counter_matches(env, plan, 2)


def test_fail_closed_when_the_runner_identity_is_the_administrator(env, monkeypatch, capsys):
    """Main path: DATABASE_URL = the ADMIN DSN (e.g. the Render one) -> refused before any request."""
    monkeypatch.setenv("DATABASE_URL", _dsn(ADMIN, env.admin_pw, "defaultdb"))
    monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    monkeypatch.setenv("APIFOOTBALL_LIVE_AUTHORIZED", "true")
    monkeypatch.setenv("APIFOOTBALL_API_KEY", KEY)
    sent = []
    import httpx
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(lambda r: sent.append(r)))
    assert batch.main(["--execute", "--authorize-live-campaign", "--campaign-id", "camp-admin-0001",
                       "--out", str(Path(os.environ.get("TMPDIR", "/tmp")) / "pa-out")]) == 2
    assert "CAMPAIGN_REFUSED RUNNER_IDENTITY_NOT_DEDICATED" in capsys.readouterr().out and sent == []
    # the ledger itself also refuses the administrator (it cannot even read the tables)
    admin_ledger = batch.PostgresCampaignLedger(lambda: env.admin("fcb_cert"), schema="public", owner_role=OWNER)
    with pytest.raises(batch.LedgerUnavailable):
        asyncio.run(admin_ledger.open("camp-admin-0002", 78, "a" * 64))


def test_fail_closed_when_a_database_is_unavailable(env, monkeypatch, capsys):
    plan_text = batch.render_plan(batch.build_plan())
    provider = Provider()
    down = batch.PostgresCampaignLedger(lambda: psycopg.connect(_dsn(RUNNER, env.runner_pw, "fcb_cert_missing")),
                                        schema="public", owner_role=OWNER)
    with pytest.raises(batch.LedgerUnavailable, match="CAMPAIGN_LEDGER_UNAVAILABLE"):
        asyncio.run(batch.execute(json.loads(plan_text), campaign_id="camp-down-00001", settings=settings(),
                                  ledger=down, inner_transport=provider.transport(), quota=Quota(),
                                  plan_text=plan_text, source_sha="x"))
    assert provider.sent == []
    monkeypatch.setenv("DATABASE_URL", _dsn(RUNNER, env.runner_pw, "no_such_coordinator"))
    monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    monkeypatch.setenv("APIFOOTBALL_LIVE_AUTHORIZED", "true")
    monkeypatch.setenv("APIFOOTBALL_API_KEY", KEY)
    assert batch.main(["--execute", "--authorize-live-campaign", "--campaign-id", "camp-down-00002", "--out", "/tmp/pa-x"]) == 2
    assert "CAMPAIGN_REFUSED COORDINATOR_DATABASE_UNAVAILABLE" in capsys.readouterr().out


# ------------------------------------------------------------------ the administrator (= Render runtime identity)

ADMIN_REFUSED = ("SELECT * FROM public.api_football_cert_plans", "SELECT * FROM public.api_football_cert_plan_usage",
                 "INSERT INTO public.api_football_cert_campaign_attempts (campaign_id, call_id) VALUES ('x', 'y')",
                 "UPDATE public.api_football_cert_plan_usage SET used = 0", "DELETE FROM public.api_football_cert_campaign_attempts",
                 "TRUNCATE public.api_football_cert_campaign_attempts CASCADE", "DROP TABLE public.api_football_cert_campaign_outcomes",
                 "ALTER TABLE public.api_football_cert_campaign_attempts DISABLE TRIGGER ALL",
                 "ALTER TABLE public.api_football_cert_campaign_attempts OWNER TO " + ADMIN,
                 "DROP SCHEMA public CASCADE", "CREATE TABLE public.x (a int)", "CREATE TEMP TABLE x (a int)",
                 "SET ROLE " + OWNER, "ALTER DATABASE fcb_cert OWNER TO " + ADMIN,
                 "GRANT SELECT ON public.api_football_cert_plan_usage TO " + ADMIN,
                 "REVOKE INSERT ON public.api_football_cert_campaign_attempts FROM " + RUNNER,
                 "SELECT public.api_football_cert_attempt_guard()")


def test_administrator_without_a_deliberate_act_can_neither_read_nor_change_nor_drop_the_ledger(env):
    for statement in ADMIN_REFUSED:
        with pytest.raises(psycopg.Error) as exc:
            with env.admin("fcb_cert") as conn:
                conn.execute(statement)
        assert exc.value.sqlstate in ("42501",), (statement, exc.value.sqlstate)
    for statement in ("DROP DATABASE fcb_cert", "ALTER DATABASE fcb_cert RENAME TO fcb_cert_old"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with env.admin("defaultdb", autocommit=True) as conn:
                conn.execute(statement)


def test_a_deliberate_admin_act_is_needed_to_bypass_and_the_runner_detects_it_while_it_lasts(env):
    """The ONLY bypass: the administrator grants itself the owner (its automatic ADMIN option allows
    it, PostgreSQL has no way to take that option away from a CREATEROLE creator), then acts as the
    owner. Visible in pg_auth_members; while it lasts every runner transaction refuses."""
    plan = _approve(env, 5, "deliberate")
    ledger = _ledger(env)
    asyncio.run(ledger.open("camp-delib-0001", 78, plan))
    asyncio.run(ledger.reserve("camp-delib-0001", "C1"))
    with env.admin("defaultdb", autocommit=True) as conn:
        conn.execute(f"GRANT {OWNER} TO {ADMIN} WITH INHERIT TRUE, SET TRUE")  # the deliberate act
    try:
        with pytest.raises(batch.LedgerUnavailable, match="CAMPAIGN_LEDGER_INTEGRITY_FAILED"):
            asyncio.run(ledger.reserve("camp-delib-0001", "C2"))
        with env.admin("fcb_cert") as conn:  # now possible: the guards can be switched off
            conn.execute("ALTER TABLE api_football_cert_campaign_attempts DISABLE TRIGGER api_football_cert_attempt_budget")
            conn.rollback()
    finally:
        with env.admin("defaultdb", autocommit=True) as conn:
            conn.execute(f"REVOKE {OWNER} FROM {ADMIN}")
    assert asyncio.run(ledger.reserve("camp-delib-0001", "C2")) == 2
    # a disabled guard or a loginable owner (deliberate owner acts) is refused too, then restored
    for break_it, restore in (
            ("ALTER TABLE api_football_cert_campaign_outcomes DISABLE TRIGGER api_football_cert_outcomes_immutable",
             "ALTER TABLE api_football_cert_campaign_outcomes ENABLE TRIGGER api_football_cert_outcomes_immutable"),
            (f"ALTER ROLE {OWNER} LOGIN", f"ALTER ROLE {OWNER} NOLOGIN")):
        with env.su("fcb_cert") as conn:
            conn.execute(break_it)
        try:
            with pytest.raises(batch.LedgerUnavailable, match="CAMPAIGN_LEDGER_INTEGRITY_FAILED"):
                asyncio.run(ledger.reserve("camp-delib-0001", "C3"))
        finally:
            with env.su("fcb_cert") as conn:
                conn.execute(restore)
    # a counter reset (owner, guard disabled): review #201 option A refuses the very next reservation
    # in its own transaction (rolled back), before any request
    with env.su("fcb_cert") as conn:
        with conn.transaction():
            conn.execute("ALTER TABLE api_football_cert_plan_usage DISABLE TRIGGER api_football_cert_plan_usage_monotonic")
            conn.execute("UPDATE api_football_cert_plan_usage SET used = 0 WHERE plan_sha256 = %s", (plan,))
            conn.execute("ALTER TABLE api_football_cert_plan_usage ENABLE TRIGGER api_football_cert_plan_usage_monotonic")
    asyncio.run(ledger.open("camp-delib-0002", 78, plan))
    with pytest.raises(batch.LedgerUnavailable, match="CAMPAIGN_LEDGER_COUNTER_MISMATCH"):
        asyncio.run(ledger.reserve("camp-delib-0002", "C1"))
    assert _committed_attempts(env, "camp-delib-0002") == 0


def test_why_not_defaultdb_public_its_owner_can_drop_another_roles_table_without_any_grant(env):
    """A1 rejected in `public`: the administrator owns defaultdb, hence (pg_database_owner) its public
    schema, and a schema owner may DROP any table in it - one statement from the Render identity."""
    with env.su("defaultdb") as conn:
        conn.execute(f"CREATE TABLE public.fcbpa_probe (a int)")
        conn.execute(f"ALTER TABLE public.fcbpa_probe OWNER TO {OWNER}")
    with env.admin() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM public.fcbpa_probe")
        conn.rollback()
        conn.execute("DROP TABLE public.fcbpa_probe")  # succeeds: no SET ROLE, no grant
    with env.admin() as conn:
        assert conn.execute("SELECT to_regclass('public.fcbpa_probe') AS t").fetchone()["t"] is None


# ------------------------------------------------------------------ the bridge, unchanged, with the admin DSN

def test_bridge_readiness_and_oauth_unchanged_with_the_admin_dsn(env, monkeypatch):
    from bridge import oauth_store, schema_readiness
    monkeypatch.setenv("MCP_OAUTH_THROTTLE_MODE", "off")
    result = schema_readiness.evaluate(lambda: env.admin())
    assert result["state"] == "READY" and result["deferred_versions"] == [9, 10], result
    monkeypatch.setattr(oauth_store, "_conn", lambda: env.admin())
    redirect = "https://claude.ai/api/mcp/auth_callback"
    code = "c-" + uuid.uuid4().hex
    oauth_store.save_code(code, client_id="cli", redirect_uri=redirect, code_challenge="CH",
                          resource="https://bridge.example/mcp", scope="browser:use offline_access")
    access, refresh = "A-" + uuid.uuid4().hex, "R-" + uuid.uuid4().hex
    assert oauth_store.exchange_code(code, client_id="cli", redirect_uri=redirect, verifier_matches=lambda ch: ch == "CH",
                                     access=access, refresh=refresh) == {"resource": "https://bridge.example/mcp",
                                                                         "scope": "browser:use offline_access"}
    assert oauth_store.valid_access_token(access, "https://bridge.example/mcp", "browser:use")
    client = oauth_store.register_claude_dcr_client(redirect)
    assert oauth_store.valid_claude_dcr_client(client, redirect)


# ------------------------------------------------------------------ A2 (owner GO): coordinator grant, order, escalation

COORDINATOR_PRIVILEGES = [  # (table, privilege, column or None): the EXACT runner grant, each one needed
    ("api_football_quota_counters", "SELECT", None), ("api_football_quota_counters", "INSERT", None),
    ("api_football_quota_counters", "UPDATE", "used"),
    ("api_football_quota_leases", "SELECT", None), ("api_football_quota_leases", "INSERT", None),
    ("api_football_quota_leases", "DELETE", None),
    ("api_football_quota_attempts", "SELECT", None), ("api_football_quota_attempts", "INSERT", None),
    ("api_football_quota_attempts", "DELETE", None)]


def _coordinator_cycle(env):
    """Every coordinator operation the runner performs: prepare, acquire (+ expired cleanup), release,
    provider-reported zero (mark_exhausted) and snapshot."""
    from bridge.api_football.quota import PostgresQuota, QuotaLimits
    quota = PostgresQuota(QuotaLimits(50, 1000, 4), connect=lambda: env.runner("defaultdb"))
    key = "PATH-A2-PRIV-" + uuid.uuid4().hex

    async def go():
        await quota.prepare()
        lease = await quota.acquire(key)
        await quota.release(lease)
        lease = await quota.acquire(key)  # second time: the minute/day buckets exist -> ON CONFLICT DO UPDATE
        await quota.release(lease)
        await quota.mark_exhausted(key, "day")
        return await quota.snapshot(key)
    return asyncio.run(go())


def test_a2_the_runner_coordinator_grant_is_exactly_nine_privileges(env):
    with env.admin() as conn:
        rows = conn.execute(
            "SELECT c.relname, x.privilege_type, x.is_grantable, pg_get_userbyid(x.grantor) AS grantor FROM pg_class c, "
            "LATERAL aclexplode(c.relacl) x WHERE c.relnamespace = 'public'::regnamespace AND x.grantee = %s::regrole "
            "ORDER BY 1, 2", (RUNNER,)).fetchall()
        cols = conn.execute(
            "SELECT c.relname, a.attname, x.privilege_type FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid, "
            "LATERAL aclexplode(a.attacl) x WHERE c.relnamespace = 'public'::regnamespace AND x.grantee = %s::regrole",
            (RUNNER,)).fetchall()
        schema = conn.execute("SELECT count(*) AS n FROM pg_namespace n, LATERAL aclexplode(n.nspacl) x "
                              "WHERE x.grantee = %s::regrole", (RUNNER,)).fetchone()["n"]
    got = {(r["relname"], r["privilege_type"], None) for r in rows} | {(c["relname"], c["privilege_type"], c["attname"]) for c in cols}
    assert got == set(COORDINATOR_PRIVILEGES) and len(got) == 9 and schema == 0
    assert all(r["is_grantable"] is False and r["grantor"] == ADMIN for r in rows)
    assert _coordinator_cycle(env)["remaining_day"] == 0  # the full cycle works with exactly these


@pytest.mark.parametrize("table,priv,column", COORDINATOR_PRIVILEGES)
def test_a2_each_coordinator_privilege_is_needed(env, table, priv, column):
    target = f"{priv} ({column})" if column else priv
    with env.admin() as conn:
        conn.execute(f"REVOKE {target} ON public.{table} FROM {RUNNER}")
    try:
        with pytest.raises(Exception) as exc:
            _coordinator_cycle(env)
        assert isinstance(exc.value, psycopg.errors.InsufficientPrivilege) or "permission denied" in str(exc.value)
    finally:
        with env.admin() as conn:
            conn.execute(f"GRANT {target} ON public.{table} TO {RUNNER}")
    _coordinator_cycle(env)


def test_a2_runner_cannot_escalate(env):
    statements = {
        "defaultdb": [f"CREATE ROLE fcbpa_x", f"ALTER ROLE {RUNNER} CREATEDB", f"ALTER ROLE {OWNER} SET search_path = x",
                      f"GRANT {OWNER} TO {RUNNER}", f"GRANT {ADMIN} TO {RUNNER}", f"SET ROLE {ADMIN}", f"SET ROLE {OWNER}",
                      "CREATE DATABASE fcbpa_x", "ALTER TABLE public.api_football_quota_counters OWNER TO " + RUNNER,
                      "CREATE SCHEMA fcbpa_x", "SELECT * FROM public.oauth_access_tokens"],
        "fcb_cert": [f"GRANT SELECT ON public.api_football_cert_plan_usage TO {RUNNER}", f"ALTER DATABASE fcb_cert OWNER TO {RUNNER}", f"ALTER DATABASE fcb_cert SET search_path = pg_temp",
                     f"ALTER ROLE {OWNER} LOGIN", "CREATE FUNCTION public.f() RETURNS int LANGUAGE sql AS 'select 1'",
                     "SELECT pg_catalog.set_config('role', 'fcb_cert_owner', false)"]}
    n = 0
    for db, stmts in statements.items():
        for statement in stmts:
            with pytest.raises(psycopg.Error) as exc:
                with env.runner(db) as conn:
                    conn.autocommit = True  # CREATE DATABASE must reach the privilege check
                    conn.execute(statement)
            assert exc.value.sqlstate in ("42501", "22023", "0LP01", "42704"), (statement, exc.value.sqlstate)
            n += 1
    assert n == 17
    # without a grant option a GRANT is a no-op (PostgreSQL only warns): the ACL is unchanged
    with env.admin() as conn:
        acl = conn.execute("SELECT relacl::text AS a FROM pg_class WHERE oid = 'public.api_football_quota_counters'::regclass").fetchone()
    for statement in ("GRANT SELECT ON public.api_football_quota_counters TO PUBLIC",
                      f"GRANT DELETE, TRUNCATE ON public.api_football_quota_counters TO {RUNNER}"):
        with env.runner("defaultdb") as conn:
            conn.execute(statement)
    with env.admin() as conn:
        assert conn.execute("SELECT relacl::text AS a FROM pg_class WHERE oid = 'public.api_football_quota_counters'::regclass").fetchone() == acl
    with env.su("fcb_cert") as conn:
        acls = conn.execute("SELECT string_agg(relacl::text, ';' ORDER BY relname) AS a FROM pg_class "
                            "WHERE relnamespace = 'public'::regnamespace").fetchone()
    for statement in (f"GRANT INSERT ON public.api_football_cert_plans TO {RUNNER}",
                      "GRANT INSERT ON public.api_football_cert_campaign_attempts TO PUBLIC"):
        with env.runner() as conn:
            conn.execute(statement)
    with env.su("fcb_cert") as conn:
        assert conn.execute("SELECT string_agg(relacl::text, ';' ORDER BY relname) AS a FROM pg_class "
                            "WHERE relnamespace = 'public'::regnamespace").fetchone() == acls
    with env.su("postgres") as su:  # nothing changed
        assert su.execute("SELECT rolcreatedb OR rolcreaterole OR rolsuper OR rolbypassrls AS x FROM pg_roles WHERE rolname = %s",
                          (RUNNER,)).fetchone()["x"] is False
        assert su.execute("SELECT count(*) AS n FROM pg_auth_members WHERE member = %s::regrole", (RUNNER,)).fetchone()["n"] == 0


class _Events(list):
    pass


def _plan_variant(env, budget, tag):
    """A distinct committed-looking plan text (its own sha) approved with `budget`: lets each test own its budget."""
    text = json.dumps({**json.loads(batch.render_plan(batch.build_plan())), "x_test_variant": tag}, sort_keys=True)
    sha = batch.plan_sha256(text)
    with env.su("fcb_cert") as conn:
        known = conn.execute("SELECT 1 FROM api_football_cert_plans WHERE plan_sha256 = %s", (sha,)).fetchone()
    if not known:
        _approve(env, budget, tag, sha=sha)
    return text, sha


def _recording_quota(env, events, after_acquire=None):
    from bridge.api_football.quota import PostgresQuota, QuotaLimits
    real = PostgresQuota(QuotaLimits(1000, 100000, 8), connect=lambda: env.runner("defaultdb"))

    class Q:
        async def prepare(self):
            await real.prepare()

        async def acquire(self, secret):
            lease = await real.acquire(secret)
            with env.su("defaultdb") as su:  # the lease is REALLY committed in defaultdb
                assert su.execute("SELECT count(*) AS n FROM api_football_quota_leases WHERE lease_id = %s",
                                  (lease.lease_id,)).fetchone()["n"] == 1
            events.append("lease")
            if after_acquire:
                after_acquire()
            return lease

        async def release(self, lease):
            events.append("release")
            await real.release(lease)

        async def mark_exhausted(self, secret, kind):
            await real.mark_exhausted(secret, kind)

        async def snapshot(self, secret):
            return await real.snapshot(secret)
    return Q()


def _recording_provider(events):
    import httpx
    sent = []

    def handler(request):
        events.append("send")
        sent.append(request.url.path)
        return Provider().default(request)
    return httpx.MockTransport(handler), sent


def test_a2_order_is_authorization_plan_lease_reservation_call_outcome(env):
    text, sha = _plan_variant(env, 78, "order")
    events = _Events()

    class Recording(batch.PostgresCampaignLedger):
        async def open(self, *a):
            events.append("plan")
            return await super().open(*a)

        async def reserve(self, *a):
            seq = await super().reserve(*a)
            with env.su("fcb_cert") as su:  # COMMITTED before the call
                assert su.execute("SELECT count(*) AS n FROM api_football_cert_campaign_attempts WHERE seq = %s "
                                  "AND campaign_id = %s", (seq, a[0])).fetchone()["n"] == 1
            events.append("reserve")
            return seq

        async def record_outcome(self, *a):
            events.append("outcome")
            return await super().record_outcome(*a)
    transport, sent = _recording_provider(events)
    plan = json.loads(text)
    plan["calls"] = plan["calls"][:2]
    run = asyncio.run(batch.execute(plan, campaign_id="camp-order-" + uuid.uuid4().hex[:8], settings=settings(),
                              ledger=Recording(env.runner, schema="public", owner_role=OWNER),
                              inner_transport=transport, quota=_recording_quota(env, events), plan_text=text,
                              source_sha="x"))
    assert len(sent) == 2, run["calls"]
    assert events == ["plan", "lease", "reserve", "send", "outcome", "release"] * 1 + ["lease", "reserve", "send", "outcome", "release"]


def _refused_run(env, ledger, events, tag, budget=78, after_acquire=None):
    text, sha = _plan_variant(env, budget, tag)
    transport, sent = _recording_provider(events)
    plan = json.loads(text)
    plan["calls"] = plan["calls"][:3]
    campaign = "camp-neg-" + uuid.uuid4().hex[:10]
    codes = []
    try:
        run = asyncio.run(batch.execute(plan, campaign_id=campaign, settings=settings(), ledger=ledger,
                                        inner_transport=transport, quota=_recording_quota(env, events, after_acquire),
                                        plan_text=text, source_sha="x"))
        codes = [c.get("refusal_code") for c in run["calls"]]
        assert all(c["outcome"] == "REFUSED_BEFORE_SEND" and c["verdict"] == "NOT_TESTED" for c in run["calls"]), run["calls"]
    except batch.LedgerUnavailable as exc:  # the next call's pre-check refused the whole run: still pre-send
        codes = [str(exc)]
    return sent, codes, campaign, sha


def _committed_attempts(env, campaign):
    with env.su("fcb_cert") as conn:
        return conn.execute("SELECT count(*) AS n FROM api_football_cert_campaign_attempts WHERE campaign_id = %s",
                            (campaign,)).fetchone()["n"]


@pytest.mark.parametrize("case", ["ledger_unreachable_after_lease", "budget_exhausted", "serialization_failure",
                                  "integrity_violation", "identity_violation", "commit_connection_killed"])
def test_a2_no_provider_request_with_only_a_quota_lease(env, case):
    """OWNER REQUIREMENT: a quota lease alone never reaches the provider. The lease is REALLY taken in
    defaultdb (asserted), then the persistent reservation in fcb_cert fails or is never committed:
    MockTransport records ZERO requests and every call is a pre-send refusal (NOT_TESTED)."""
    events = _Events()
    switch = {"on": False}
    expected = {"ledger_unreachable_after_lease": "CAMPAIGN_LEDGER_UNAVAILABLE", "budget_exhausted": "CAMPAIGN_BUDGET_EXHAUSTED",
                "serialization_failure": "SERIALIZATION_FAILURE", "integrity_violation": "CAMPAIGN_LEDGER_INTEGRITY_FAILED",
                "identity_violation": "RUNNER_IDENTITY_NOT_DEDICATED",
                "commit_connection_killed": "CAMPAIGN_LEDGER_UNAVAILABLE"}[case]
    inserted = []

    class Ledger(batch.PostgresCampaignLedger):
        def _reserve(self, campaign_id, call_id):
            if not switch["on"]:
                return super()._reserve(campaign_id, call_id)
            if case == "ledger_unreachable_after_lease":
                self._connect, keep = (lambda: psycopg.connect(_dsn(RUNNER, env.runner_pw, "fcb_cert_down"),
                                                               connect_timeout=3)), self._connect
            elif case == "serialization_failure":
                def conflicting():  # every reservation attempt is cancelled by the server-side conflict code
                    with env.runner() as conn:  # the ledger database answers, then the reservation conflicts
                        conn.execute("SELECT 1")
                    raise psycopg.errors.SerializationFailure("could not serialize access")
                self._connect, keep = conflicting, self._connect
            elif case == "commit_connection_killed":
                class Killed(psycopg.Connection):
                    def commit(inner):
                        inserted.append(inner.info.backend_pid)  # the INSERT ran (RETURNING seq) inside this tx
                        with env.su("fcb_cert") as su:
                            su.execute("SELECT pg_terminate_backend(%s)", (inner.info.backend_pid,))
                        return super().commit()
                self._connect, keep = (lambda: Killed.connect(env.runner_dsn(), row_factory=dict_row)), self._connect
            else:
                keep = self._connect
            try:
                return super()._reserve(campaign_id, call_id)
            finally:
                self._connect = keep
    budget = 78
    tag = case + uuid.uuid4().hex[:6]
    restore = None
    if case == "budget_exhausted":
        budget = 1
        text, sha = _plan_variant(env, 1, tag)
        with env.runner() as conn:  # another campaign of the same plan already spent its only unit
            conn.execute("INSERT INTO api_football_cert_campaigns (campaign_id, plan_sha256) VALUES (%s, %s)",
                         ("camp-spent-" + uuid.uuid4().hex[:8], sha))
            conn.execute("INSERT INTO api_football_cert_campaign_attempts (campaign_id, call_id) SELECT campaign_id, 'X' "
                         "FROM api_football_cert_campaigns WHERE plan_sha256 = %s", (sha,))

    def after_acquire():
        nonlocal restore
        switch["on"] = True
        if case == "integrity_violation" and restore is None:
            with env.su("fcb_cert") as su:
                su.execute("ALTER TABLE api_football_cert_campaign_attempts DISABLE TRIGGER api_football_cert_attempt_budget")
            restore = ("fcb_cert", "ALTER TABLE api_football_cert_campaign_attempts ENABLE TRIGGER api_football_cert_attempt_budget")
        if case == "identity_violation" and restore is None:
            with env.su("postgres") as su:
                su.execute("CREATE ROLE fcbpa_any NOLOGIN")
                su.execute(f"GRANT fcbpa_any TO {RUNNER}")
            restore = ("postgres", "DROP ROLE fcbpa_any")
    try:
        sent, codes, campaign, sha = _refused_run(env, Ledger(env.runner, schema="public", owner_role=OWNER), events, tag,
                                                  budget=budget, after_acquire=after_acquire)
    finally:
        if restore:
            with env.su(restore[0]) as su:
                su.execute(restore[1])
    assert sent == [] and "send" not in events, events
    assert "lease" in events and events.index("lease") < len(events)  # a lease WAS obtained first
    # the first call meets the failing reservation; later ones may also be stopped by the client's own
    # circuit breaker after consecutive refusals (CIRCUIT_OPEN): every one of them is pre-send
    assert codes and codes[0] == expected and set(codes) <= {expected, "CIRCUIT_OPEN"}, codes
    assert _committed_attempts(env, campaign) == 0
    if case == "commit_connection_killed":
        assert inserted, "the INSERT must have run before the killed COMMIT"
    if case == "budget_exhausted":
        _counter_matches(env, sha, 1)
    with env.su("defaultdb") as su:  # every lease taken was released (or would expire): none left active by this run
        assert events.count("release") == events.count("lease")


def _kill_between_insert_and_commit(runner_dsn, coordinator_dsn, plan_text, campaign, marker):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    import httpx as _httpx
    from apifootball_campaign_kit import load as _load, settings as _settings
    from bridge.api_football.quota import PostgresQuota, QuotaLimits
    mod = _load("apifootball_cert_batch1_pa_killcommit", "apifootball_cert_batch1.py")
    os.environ[mod.AUTH_VAR] = "true"

    class Dies(psycopg.Connection):
        def commit(self):
            if self.execute("SELECT EXISTS (SELECT 1 FROM public.api_football_cert_campaign_attempts a "
                            "WHERE a.xmin = pg_current_xact_id_if_assigned()::text::xid)").fetchone()[0]:
                os._exit(9)  # the attempt row is INSERTed (xid assigned) and the process dies before COMMIT
            return super().commit()

    def handler(request):
        Path(marker).write_text("sent", encoding="utf-8")
        return _httpx.Response(200, json={})
    ledger = mod.PostgresCampaignLedger(lambda: Dies.connect(runner_dsn), schema="public",
                                        owner_role="fcb_cert_owner")
    quota = PostgresQuota(QuotaLimits(1000, 100000, 8), connect=lambda: psycopg.connect(coordinator_dsn, row_factory=dict_row))
    plan = json.loads(plan_text)
    plan["calls"] = plan["calls"][:1]
    asyncio.run(mod.execute(plan, campaign_id=campaign, settings=_settings(), ledger=ledger,
                            inner_transport=_httpx.MockTransport(handler), quota=quota, plan_text=plan_text, source_sha="x"))
    os._exit(0)


def test_a2_process_killed_between_insert_and_commit_sends_nothing_and_consumes_nothing(env, tmp_path):
    text, sha = _plan_variant(env, 78, "killcommit")
    campaign = "camp-kc-" + uuid.uuid4().hex[:8]
    with env.su("fcb_cert") as su:
        before = su.execute("SELECT used FROM api_football_cert_plan_usage WHERE plan_sha256 = %s", (sha,)).fetchone()["used"]
    with env.su("defaultdb") as su:
        leases_before = su.execute("SELECT count(*) AS n FROM api_football_quota_leases").fetchone()["n"]
    marker = tmp_path / "sent"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_kill_between_insert_and_commit,
                       args=(env.runner_dsn(), env.runner_dsn("defaultdb"), text, campaign, str(marker)))
    proc.start()
    proc.join(120)
    assert proc.exitcode == 9 and not marker.exists()  # died; the provider was never reached
    assert _committed_attempts(env, campaign) == 0  # the uncommitted reservation rolled back...
    _counter_matches(env, sha, before)  # ...with its counter increment: nothing consumed
    with env.su("defaultdb") as su:  # the lease WAS committed before (orphan, expires after 180 s)
        assert su.execute("SELECT count(*) AS n FROM api_football_quota_leases").fetchone()["n"] == leases_before + 1
    # MANDATORY 6: rollback + retry, no regression: the next legitimate reservation passes the check
    ledger = _ledger(env)
    asyncio.run(ledger.open(campaign, 78, sha))
    seq = asyncio.run(ledger.reserve(campaign, "RETRY"))
    assert seq == before + 1
    _counter_matches(env, sha, before + 1)


def test_a2_a_second_ledger_in_defaultdb_is_refused_before_any_request(env, monkeypatch, capsys):
    with env.admin() as conn:
        conn.execute("CREATE TABLE public.api_football_cert_plans (plan_sha256 text)")
        conn.execute(f"GRANT SELECT ON public.api_football_cert_plans TO {RUNNER}")
    try:
        monkeypatch.setenv("DATABASE_URL", env.runner_dsn("defaultdb"))
        monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
        monkeypatch.setenv("APIFOOTBALL_LIVE_AUTHORIZED", "true")
        monkeypatch.setenv("APIFOOTBALL_API_KEY", KEY)
        assert batch.main(["--execute", "--authorize-live-campaign", "--campaign-id", "camp-second-001",
                           "--out", "/tmp/pa-second"]) == 2
        assert "CAMPAIGN_REFUSED SECOND_LEDGER_PRESENT" in capsys.readouterr().out
    finally:
        with env.admin() as conn:
            conn.execute("DROP TABLE public.api_football_cert_plans")


def test_a2_evidence_is_catalog_only_stable_and_free_of_secrets(env):
    def ev():
        return path_a.evidence(env.admin, lambda: env.admin("fcb_cert"))
    first = ev()
    assert first == ev()
    text = "\n".join(first)
    assert env.admin_pw not in text and env.runner_pw not in text and "SCRAM" not in text
    assert "database fcb_cert owner=fcb_cert_owner acl_entries=3" in first
    assert "defaultdb bridge_versions=1,2,3,4,5,6,7,8" in first
    assert sum(line.startswith("grant ") for line in first) == 8 and sum(line.startswith("column_grant ") for line in first) == 1
    assert "ledger triggers_enabled=13 triggers=13" in first


# ------------------------------------------------------------------ review of PR #201: F1 / F3 / F4 and the counter reset

LEDGER_FUNCTIONS = ("api_football_cert_attempt_guard", "api_football_cert_plan_usage_rule",
                    "api_football_cert_reject_mutation", "api_football_cert_stamp")


def _function_body(name):
    import re as _re
    text = (ROOT / "sql/migrations/bridge/0010_apifootball_cert_campaign_ledger.sql").read_text(encoding="utf-8")
    m = _re.search(rf"CREATE FUNCTION {name}\(\) RETURNS trigger LANGUAGE plpgsql( SECURITY DEFINER)?\s+"
                   r"SET search_path = pg_catalog, pg_temp AS \$\$(.*?)\$\$;", text, _re.S)
    return bool(m.group(1)), m.group(2)


def _as_owner_deliberately(env, *statements):
    """The deliberate administrator act: self-grant SET on the owner (its automatic ADMIN option allows
    it), act AS the owner, then revoke, leaving no membership behind (catalog looks clean again)."""
    with env.admin("fcb_cert") as conn:
        conn.execute(f"GRANT {OWNER} TO {ADMIN} WITH INHERIT FALSE, SET TRUE")
        conn.execute(f"SET LOCAL ROLE {OWNER}")
        for statement in statements:
            conn.execute(statement)
        conn.execute("RESET ROLE")
        conn.execute(f"REVOKE {OWNER} FROM {ADMIN}")


def _replace_function(name, body):
    secdef, _ = _function_body(name)
    return (f"CREATE OR REPLACE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql"
            f"{' SECURITY DEFINER' if secdef else ''} SET search_path = pg_catalog, pg_temp AS $fcbf${body}$fcbf$")


def _execute_counting(env, ledger, tag, calls=2):
    text, _sha = _plan_variant(env, 78, tag)
    events = _Events()
    transport, sent = _recording_provider(events)
    plan = json.loads(text)
    plan["calls"] = plan["calls"][:calls]
    try:
        run = asyncio.run(batch.execute(plan, campaign_id="camp-rev-" + uuid.uuid4().hex[:10], settings=settings(),
                                        ledger=ledger, inner_transport=transport, quota=_recording_quota(env, events),
                                        plan_text=text, source_sha="x"))
        return sent, [c.get("refusal_code") or c["outcome"] for c in run["calls"]]
    except batch.LedgerUnavailable as exc:
        return sent, [str(exc)]


def test_f1_installed_functions_match_the_pinned_fingerprint(env):
    with env.su("fcb_cert") as conn:
        got = conn.execute(
            "SELECT string_agg(p.proname || '|' || pg_get_function_identity_arguments(p.oid) || '|' || "
            "format_type(p.prorettype, NULL) || '|' || l.lanname || '|' || p.prosecdef || '|' || COALESCE(p.proconfig::text, '-') "
            "|| '|' || p.provolatile::text || '|' || md5(p.prosrc), ';' ORDER BY p.proname) AS s FROM pg_proc p "
            "JOIN pg_language l ON l.oid = p.prolang WHERE p.pronamespace = 'public'::regnamespace").fetchone()["s"]
    assert got == batch.LEDGER_FUNCTION_SIGNATURES


@pytest.mark.parametrize("name", LEDGER_FUNCTIONS)
def test_f1_a_replaced_function_body_is_refused_before_any_request(env, name):
    """The owner (deliberate act) replaces the body keeping SECURITY DEFINER, proconfig, owner and the
    enabled triggers, then revokes: the catalog looks clean, but the pinned fingerprint differs."""
    secdef, body = _function_body(name)
    _as_owner_deliberately(env, _replace_function(name, body + "-- tampered\n"))
    try:
        with env.su("fcb_cert") as conn:  # everything else is exactly as installed
            row = conn.execute("SELECT p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner) AS o FROM pg_proc p "
                               "WHERE p.proname = %s", (name,)).fetchone()
            assert row == {"prosecdef": secdef, "proconfig": ["search_path=pg_catalog, pg_temp"], "o": OWNER}
            assert conn.execute("SELECT count(*) AS n FROM pg_trigger WHERE NOT tgisinternal AND tgenabled <> 'O'").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM pg_auth_members WHERE roleid = %s::regrole AND (set_option OR inherit_option)",
                                (OWNER,)).fetchone()["n"] == 0
        sent, codes = _execute_counting(env, _ledger(env), "f1-" + name)
        assert sent == [] and codes == ["CAMPAIGN_LEDGER_INTEGRITY_FAILED"], codes
    finally:
        _as_owner_deliberately(env, _replace_function(name, body))
    sent, codes = _execute_counting(env, _ledger(env), "f1-ok-" + name, calls=1)
    assert len(sent) == 1  # restored: works again


@pytest.mark.parametrize("option", ["INHERIT TRUE, SET FALSE", "INHERIT FALSE, SET TRUE"])
def test_f3_any_member_of_the_runner_is_refused_before_any_request(env, option, monkeypatch, capsys):
    with env.su("postgres") as su:
        su.execute("CREATE ROLE fcbpa_member NOLOGIN")
        su.execute(f"GRANT {RUNNER} TO fcbpa_member WITH {option}")
    try:
        sent, codes = _execute_counting(env, _ledger(env), "f3-" + option[:7])
        assert sent == [] and codes == ["RUNNER_IDENTITY_NOT_DEDICATED"], codes
        monkeypatch.setenv("DATABASE_URL", env.runner_dsn("defaultdb"))
        monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
        monkeypatch.setenv("APIFOOTBALL_LIVE_AUTHORIZED", "true")
        monkeypatch.setenv("APIFOOTBALL_API_KEY", KEY)
        assert batch.main(["--execute", "--authorize-live-campaign", "--campaign-id", "camp-f3-00001", "--out", "/tmp/pa-f3"]) == 2
        assert "CAMPAIGN_REFUSED RUNNER_IDENTITY_NOT_DEDICATED" in capsys.readouterr().out
    finally:
        with env.su("postgres") as su:
            su.execute("DROP ROLE fcbpa_member")
    sent, _codes = _execute_counting(env, _ledger(env), "f3-ok-" + option[:7], calls=1)
    assert len(sent) == 1


def test_f4_each_connection_must_really_be_on_its_database(env, monkeypatch, capsys):
    # the ledger on the coordinator database (a DSN pointing elsewhere): refused before any lease or call
    wrong = batch.PostgresCampaignLedger(lambda: env.runner("defaultdb"), schema="public", owner_role=OWNER)
    sent, codes = _execute_counting(env, wrong, "f4-ledger")
    assert sent == [] and codes == ["LEDGER_DATABASE_WRONG"], codes
    # the coordinator on the ledger database: refused by the runner before any lease
    monkeypatch.setenv("DATABASE_URL", env.runner_dsn("fcb_cert"))
    monkeypatch.setenv("APIFOOTBALL_ENABLED", "true")
    monkeypatch.setenv("APIFOOTBALL_LIVE_AUTHORIZED", "true")
    monkeypatch.setenv("APIFOOTBALL_API_KEY", KEY)
    import httpx
    hits = []
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(lambda r: hits.append(r)))
    assert batch.main(["--execute", "--authorize-live-campaign", "--campaign-id", "camp-f4-00001", "--out", "/tmp/pa-f4"]) == 2
    assert "CAMPAIGN_REFUSED COORDINATOR_DATABASE_WRONG" in capsys.readouterr().out and hits == []
    with env.su("defaultdb") as su:
        assert su.execute("SELECT count(*) AS n FROM api_football_quota_leases WHERE api_key_hash IS NOT NULL "
                          "AND expires_at > now() + interval '170 seconds'").fetchone()["n"] == 0  # no fresh lease


def _set_counter_deliberately(env, sha, value):
    """The deliberate privileged act: admin self-grants SET on the owner, disables the monotonic guard,
    writes the counter, re-enables the guard and revokes (the catalog looks clean afterwards)."""
    _as_owner_deliberately(env,
                           "ALTER TABLE public.api_football_cert_plan_usage DISABLE TRIGGER api_football_cert_plan_usage_monotonic",
                           f"UPDATE public.api_football_cert_plan_usage SET used = {int(value)} WHERE plan_sha256 = '{sha}'",
                           "ALTER TABLE public.api_football_cert_plan_usage ENABLE TRIGGER api_football_cert_plan_usage_monotonic")


def _plan_run(env, text, calls, campaign):
    events = _Events()
    transport, sent = _recording_provider(events)
    plan = json.loads(text)
    plan["calls"] = plan["calls"][:calls]
    try:
        run = asyncio.run(batch.execute(plan, campaign_id=campaign, settings=settings(), ledger=_ledger(env),
                                        inner_transport=transport, quota=_recording_quota(env, events),
                                        plan_text=text, source_sha="x"))
        return len(sent), [c.get("refusal_code") or c["outcome"] for c in run["calls"]]
    except batch.LedgerUnavailable as exc:
        return len(sent), [str(exc)]


def _counter_value(env, sha):
    with env.su("fcb_cert") as conn:
        return conn.execute("SELECT used FROM api_football_cert_plan_usage WHERE plan_sha256 = %s", (sha,)).fetchone()["used"]


def test_review201_counter_reset_by_a_deliberate_owner_act_is_refused_before_any_request(env):
    """MANDATORY 1 + 3. The previous bypass, reproduced exactly (reset to 0 through SET on
    fcb_cert_owner + trigger disable/enable, new campaign_id of the same plan): now refused in the
    reservation's own transaction, rolled back, MockTransport 0 requests. Not by ordinary means at all."""
    budget = 3
    text, sha = _plan_variant(env, budget, "reset-" + uuid.uuid4().hex[:6])
    assert _plan_run(env, text, budget, "camp-reset-a-01") == (budget, ["ATTEMPTED"] * budget)
    _counter_matches(env, sha, budget)
    assert _plan_run(env, text, budget, "camp-reset-b-01")[0] == 0  # budget spent: nothing for a new campaign
    _counter_matches(env, sha, budget)
    with pytest.raises(psycopg.Error):  # not by ordinary means: the runner has no privilege on the counter...
        with env.runner() as conn:
            conn.execute("UPDATE public.api_football_cert_plan_usage SET used = 0")
    with pytest.raises(psycopg.errors.RaiseException):  # ...and the owner cannot while the guard is on
        _as_owner_deliberately(env, "UPDATE public.api_football_cert_plan_usage SET used = 0")
    _set_counter_deliberately(env, sha, 0)
    sent, codes = _plan_run(env, text, budget, "camp-reset-c-01")
    assert sent == 0 and codes[0] == "CAMPAIGN_LEDGER_COUNTER_MISMATCH", codes
    assert set(codes) <= {"CAMPAIGN_LEDGER_COUNTER_MISMATCH", "CIRCUIT_OPEN"}, codes
    assert _committed_attempts(env, "camp-reset-c-01") == 0 and _counter_value(env, sha) == 0  # rolled back
    _set_counter_deliberately(env, sha, budget)  # put back: counter == committed again
    _counter_matches(env, sha, budget)


@pytest.mark.parametrize("delta", [-2, -1, +1])
def test_review201_counter_tampered_to_an_inconsistent_value_is_refused(env, delta):
    """MANDATORY 2 + 3: a non-zero counter lower (or higher) than the committed attempts."""
    budget = 6
    text, sha = _plan_variant(env, budget, f"tamper{delta}-" + uuid.uuid4().hex[:6])
    assert _plan_run(env, text, 3, "camp-tamp-a-" + uuid.uuid4().hex[:6])[0] == 3
    _counter_matches(env, sha, 3)
    _set_counter_deliberately(env, sha, 3 + delta)
    sent, codes = _plan_run(env, text, 2, "camp-tamp-b-" + uuid.uuid4().hex[:6])
    assert sent == 0 and codes[0] == "CAMPAIGN_LEDGER_COUNTER_MISMATCH", codes
    assert _counter_value(env, sha) == 3 + delta  # the refused increment was rolled back
    _set_counter_deliberately(env, sha, 3)
    _counter_matches(env, sha, 3)
    assert _plan_run(env, text, 2, "camp-tamp-c-" + uuid.uuid4().hex[:6])[0] == 2  # consistent again: legit passes
    _counter_matches(env, sha, 5)


@pytest.mark.parametrize("sql,label", [
    ("SELECT pg_catalog.count(*) AS n FROM {attempts} a WHERE a.no_such_column = %s", "query_error"),
    ("SELECT NULL::bigint AS n, NULL::bigint AS d, NULL::int AS m WHERE %s::text IS NOT NULL", "null"),
    ("SELECT 1 AS n, 1 AS d, 1 AS m FROM {campaigns} k WHERE false AND k.campaign_id = %s::text", "no_row"),
])
def test_review201_the_check_itself_failing_refuses_fail_closed(env, monkeypatch, sql, label):
    """MANDATORY 7: an error, NULL or missing row of the check refuses (fixed code), rolls back, 0 requests."""
    text, sha = _plan_variant(env, 78, "checkfail-" + label + uuid.uuid4().hex[:6])
    before = _counter_value(env, sha)
    monkeypatch.setattr(batch, "COUNTER_CHECK_SQL", sql)
    campaign = "camp-chk-" + uuid.uuid4().hex[:8]
    sent, codes = _plan_run(env, text, 2, campaign)
    assert sent == 0 and codes[0] == "CAMPAIGN_LEDGER_COUNTER_CHECK_FAILED", codes
    assert _committed_attempts(env, campaign) == 0 and _counter_value(env, sha) == before
    _counter_matches(env, sha, before)


def _iso_runner_worker(dsn, campaign, plan, level, out):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from apifootball_campaign_kit import load as _load
    mod = _load("apifootball_cert_batch1_pa_iso_runner", "apifootball_cert_batch1.py")
    os.environ[mod.AUTH_VAR] = "true"

    class Ledger(mod.PostgresCampaignLedger):
        @staticmethod
        def _read_committed(conn):  # the reservation path at the requested isolation level (test only)
            conn.isolation_level = getattr(psycopg.IsolationLevel, level.replace(" ", "_"))
            return conn
    connect = lambda: psycopg.connect(dsn, row_factory=dict_row)  # noqa: E731
    # the campaign is opened as the runner does (READ COMMITTED); the RESERVATIONS run at `level`
    asyncio.run(mod.PostgresCampaignLedger(connect, schema="public", owner_role="fcb_cert_owner").open(campaign, 78, plan))
    ledger = Ledger(connect, schema="public", owner_role="fcb_cert_owner")
    granted = conflicts = false_refusals = 0
    for i in range(40):
        try:
            asyncio.run(ledger.reserve(campaign, f"Q{i}"))
            granted += 1
        except mod.SerializationRefused:
            conflicts += 1  # retried 3 times in new transactions, still conflicting: fine, not a refusal
        except mod.BudgetExhausted:
            break
        except mod.LedgerUnavailable:
            false_refusals += 1
    out.put((granted, conflicts, false_refusals))


@pytest.mark.parametrize("level", ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"])
def test_review201_nine_processes_each_isolation_no_overrun_no_false_refusal(env, level):
    """MANDATORY 4 + 5: 9 real processes (spawn), one plan with budget 7, the full reservation path
    (budget trigger + the same-transaction counter check) at each isolation level."""
    plan = _approve(env, 7, "rev201-" + level)
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    tag = uuid.uuid4().hex[:8]
    procs = [ctx.Process(target=_iso_runner_worker, args=(env.runner_dsn(), f"camp-r201-{tag}-{i}", plan, level, out))
             for i in range(9)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(240)
    assert [p.exitcode for p in procs] == [0] * 9
    results = [out.get(timeout=5) for _ in procs]
    assert sum(r[0] for r in results) == 7, results  # no overrun, no lost legitimate reservation
    assert sum(r[2] for r in results) == 0, results  # no false positive of the counter check
    _counter_matches(env, plan, 7)
