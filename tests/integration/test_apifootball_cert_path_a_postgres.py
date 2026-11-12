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
    # a counter reset (owner, guard disabled) leaves duplicate seqs: the next open of the plan refuses
    with env.su("fcb_cert") as conn:
        with conn.transaction():
            conn.execute("ALTER TABLE api_football_cert_plan_usage DISABLE TRIGGER api_football_cert_plan_usage_monotonic")
            conn.execute("UPDATE api_football_cert_plan_usage SET used = 0 WHERE plan_sha256 = %s", (plan,))
            conn.execute("ALTER TABLE api_football_cert_plan_usage ENABLE TRIGGER api_football_cert_plan_usage_monotonic")
    asyncio.run(ledger.open("camp-delib-0002", 78, plan))
    assert asyncio.run(ledger.reserve("camp-delib-0002", "C1")) == 1  # the reset counter numbers again from 1
    with pytest.raises(batch.LedgerUnavailable, match="CAMPAIGN_LEDGER_INTEGRITY_FAILED"):
        asyncio.run(ledger.open("camp-delib-0003", 78, plan))


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
