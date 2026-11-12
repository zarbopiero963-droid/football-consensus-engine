"""WP-009 compatibility (owner GO "WP-009 COMPATIBILITY FIX"): the roles workflow's verification
model admits EXACTLY the bridge states 0001-0008, 0001-0009 (#198) and 0001-0010 (#199).

Each state is built the production way on a throwaway database restored to 0001-0008 by its
non-superuser owner (the Aiven shape of test_wp009_roles_runtime_postgres.py): phase 1, phase 1
again, phase 2, phase 2 again; then 0009 and 0010 are applied by THEIR workflows' SQL as
fcb_migrator (they need its login, i.e. phase 2), with ledger rows written by the real fcb_app; then
phase 2 is re-run twice (phase 1 is refused after phase 2, by design). Every run is checked the
way the workflow checks it (`_workflow_checks`): the model is HARDENED, the catalog bridge state,
the schema_migrations rows (names and checksums, read AS fcb_app), the object counts and the
structure fingerprint are those pinned for the state; ACLs, owners and table contents are
unchanged by the re-runs. Negative tests: partial or mixed states, a missing table or trigger,
changed routines, extra or missing table/column/routine grants, owner drift, rules, tampered
migration rows: each FAILS. With 0010, the three #199 invariants are re-checked after WP-009 with
a real SCRAM login as fcb_app. No production credentials; every role and database is dropped.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
import yaml  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AIVEN = ROOT / "sql" / "aiven"
WORKFLOW = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-roles.yml"
HELPER = ROOT / "scripts" / "fcb_db_roles.py"

_spec = importlib.util.spec_from_file_location("wp009_rr_shared", Path(__file__).with_name("test_wp009_roles_runtime_postgres.py"))
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)
pytestmark = rr.pytestmark

sys.path.insert(0, str(ROOT / "tests"))
from apifootball_campaign_kit import load  # noqa: E402

batch = load("apifootball_cert_batch1_wp009", "apifootball_cert_batch1.py")
WF_ENV = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["roles"]["env"]
PINNED_COUNTS = {8: WF_ENV["EXPECTED_COUNTS"], 9: WF_ENV["EXPECTED_COUNTS_0009"], 10: WF_ENV["EXPECTED_COUNTS_0010"]}
PINNED_STRUCTURE = {n: WF_ENV[f"EXPECTED_STRUCTURE_{n:04d}"] for n in (8, 9, 10)}
LEDGER = ("api_football_cert_plans", "api_football_cert_plan_usage", "api_football_cert_campaigns",
          "api_football_cert_campaign_attempts", "api_football_cert_campaign_outcomes")
APP_COLUMN_INSERT = {"api_football_cert_campaigns": ("campaign_id", "plan_sha256"),
                     "api_football_cert_campaign_attempts": ("campaign_id", "call_id"),
                     "api_football_cert_campaign_outcomes": ("campaign_id", "seq", "outcome", "http_status")}


# ------------------------------------------------------------------ helpers

def _app_dsn(env):
    """fcb_app's own credentials, SCRAM required (require_auth): no trust, no superuser session."""
    return rr._dsn_for(env.dbname, user="fcb_app", password=env.passwords["fcb_app"], require_auth="scram-sha-256")


def _as_app(env):
    conn = psycopg.connect(_app_dsn(env), row_factory=dict_row)
    assert conn.execute("SELECT session_user AS u").fetchone()["u"] == "fcb_app" and conn.pgconn.used_password
    conn.commit()
    return conn


def _migrator_psql(env, *args):
    """As the 0009/0010/plan-approval workflows: fcb_migrator's login, search_path public, one tx."""
    return env.psql("-v", "ON_ERROR_STOP=1", *args, pgoptions="-c search_path=public", dsn=env.dsn("fcb_migrator"))


def _apply_0009(env):
    rc, _, err = _migrator_psql(env, "-1", "-f", str(AIVEN / "aiven_migrations_bridge_0001_0009.sql"),
                                "-f", str(AIVEN / "oauth_throttle_0009_grant.sql"))
    assert rc == 0, err


def _apply_0010(env):
    rc, _, err = _migrator_psql(env, "-1", "-f", str(AIVEN / "apifootball_cert_ledger_0010_require_0009.sql"),
                                "-f", str(AIVEN / "aiven_migrations_bridge_0001_0010.sql"),
                                "-f", str(AIVEN / "apifootball_cert_ledger_0010_grant.sql"))
    assert rc == 0, err


def _approve_plan(env, sha, budget, kind="BATCH1"):
    rc, _, err = _migrator_psql(env, "-1", "-v", f"plan_sha256={sha}", "-v", f"plan_kind={kind}", "-v", f"budget={budget}",
                                "-v", "approved_by=owner", "-v", "run_id=7", "-f", str(AIVEN / "apifootball_cert_plan_approve.sql"))
    assert rc == 0, err


def _ledger(env):
    return batch.PostgresCampaignLedger(lambda: _as_app(env), schema="public")


def _write_runtime_rows(env, state):
    """Rows that must survive every re-run untouched (the runtime's, through fcb_app)."""
    with _as_app(env) as conn:
        conn.execute("INSERT INTO oauth_claude_dcr_clients (client_id_hash, redirect_uri, expires_at) "
                     "VALUES ('wp009-compat', 'https://example.invalid/cb', now() + interval '1 day')")
        if state >= 9:
            conn.execute("INSERT INTO oauth_throttle_buckets (scope, key_hash, tat) VALUES ('consent', %s, now())",
                         ("v4:" + "a" * 64,))
    if state >= 10:
        _approve_plan(env, "b" * 64, 6)
        ledger = _ledger(env)
        os.environ[batch.AUTH_VAR] = "true"
        asyncio.run(ledger.open("camp-wp009-rows-1", 78, "b" * 64))
        seq = asyncio.run(ledger.reserve("camp-wp009-rows-1", "C01"))
        asyncio.run(ledger.record_outcome("camp-wp009-rows-1", seq, "HTTP_RESPONSE", 200))
        asyncio.run(ledger.reserve("camp-wp009-rows-1", "C02"))  # RESERVED_UNKNOWN evidence


def _snapshot(env):
    """ACLs and owners (tables, columns, routines, schema, defaults, memberships) and the content
    of every table of public, read by the test superuser."""
    with psycopg.connect(rr._dsn_for(env.dbname), row_factory=dict_row) as conn:
        acl = conn.execute(
            "SELECT 'rel ' || c.relname || ' ' || c.relowner::regrole::text || ' ' || COALESCE(c.relacl::text, '-') AS x "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "UNION ALL SELECT 'col ' || c.relname || '.' || a.attname || ' ' || a.attacl::text FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND a.attacl IS NOT NULL "
            "UNION ALL SELECT 'fn ' || p.proname || ' ' || p.proowner::regrole::text || ' ' || COALESCE(p.proacl::text, '-') "
            "|| ' ' || p.prosecdef::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
            "UNION ALL SELECT 'nsp ' || COALESCE(nspacl::text, '-') FROM pg_namespace WHERE nspname = 'public' "
            "UNION ALL SELECT 'defacl ' || defaclrole::regrole::text || ' ' || defaclacl::text FROM pg_default_acl "
            "UNION ALL SELECT 'member ' || roleid::regrole::text || ' ' || member::regrole::text || ' ' || admin_option::text "
            "|| inherit_option::text || set_option::text FROM pg_auth_members "
            "WHERE roleid::regrole::text IN ('fcb_app', 'fcb_migrator', 'fcb_signer') "
            "UNION ALL SELECT 'role ' || rolname || ' ' || rolcanlogin::text || rolsuper::text || rolcreaterole::text "
            "|| rolcreatedb::text || rolbypassrls::text FROM pg_roles WHERE rolname IN ('fcb_app', 'fcb_migrator', 'fcb_signer') "
            "ORDER BY 1").fetchall()
        tables = [r["relname"] for r in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1")]
        data = {t: conn.execute(f'SELECT count(*) AS n, md5(COALESCE(string_agg(x::text, \'|\' ORDER BY x::text), \'\')) AS h '
                                f'FROM public."{t}" x').fetchone() for t in tables}
    return [r["x"] for r in acl], data


def _catalog_state(env):
    out = env.ok("-f", str(AIVEN / "verify_bridge_roles.sql"), "-c", "\\echo :fcb_bridge_state")
    return out.splitlines()[-1]


def _helper(*args, stdin=""):
    return subprocess.run([sys.executable, "-I", "-S", str(HELPER), *args], input=stdin, capture_output=True,
                          text=True, timeout=60)


def _workflow_checks(env, expected_state):
    """The workflow's phase-2 checks for a HARDENED database: returns the failures (empty = PASS)."""
    failures = []
    if env.roles_state() != "HARDENED":
        failures.append("model:" + env.roles_state())
    if _catalog_state(env) != f"BRIDGE_0001_{expected_state:04d}":
        failures.append("catalog_state:" + _catalog_state(env))
    rows = env.schema_evidence()  # AS fcb_app
    got = _helper("bridge-state", stdin=rows + "\n")
    if got.returncode != 0 or got.stdout.strip() != str(expected_state):
        failures.append("rows")
    expected_catalog = " ".join(c for c in PINNED_COUNTS[expected_state].split()
                                if not c.startswith(("constraints_information_schema=", "schema_migrations_rows=")))
    if env.catalog_counts() != expected_catalog:
        failures.append("counts:" + env.catalog_counts())
    if rr._server_major_raw() == 18 and env.structure_fingerprint() != PINNED_STRUCTURE[expected_state]:
        failures.append("structure:" + env.structure_fingerprint())
    return failures


def _step(env, prefix, **kw):
    """The REAL workflow step bash (test_wp009_roles_runtime_postgres._run_step), local server."""
    import tempfile
    with tempfile.TemporaryDirectory(dir=os.environ.get("FCB_TEST_TMP")) as tmp:
        return rr._run_step(env, prefix, Path(tmp), **kw)


def _phase2_twice_unchanged(env, state):
    before = _snapshot(env)
    rc, out = _step(env, rr.PRECHECK, mode="diagnose")
    assert rc == 0 and "Roles model: HARDENED" in out, out[-2000:]
    for _ in range(2):  # the workflow's phase 2 (enable-migrator re-dispatch), as it runs in Actions
        rc, out = _step(env, rr.PHASE2, passwords=rr._passwords(env))
        assert rc == 0 and "## Phase 2: PASS" in out, out[-3000:]
        assert _workflow_checks(env, state) == []
        assert _snapshot(env) == before  # privileges AND data unchanged
    rc, _, _err = rr._roles_tx(env, counts=PINNED_COUNTS[state],
                               migrations=_helper("expected-migrations", str(state)).stdout.strip())
    assert rc != 0  # phase 1 is refused after phase 2 (by design), and changes nothing
    assert _snapshot(env) == before


def _build(state):
    """fresh 0001-0008 -> phase 1 x2 -> phase 2 x2 -> [0009] -> [0010] -> runtime rows."""
    env = rr._create_env()
    try:
        snaps = []
        for _ in range(2):  # phase 1, then phase 1 again (the real workflow bash)
            rc, out = _step(env, rr.PHASE1, passwords=rr._passwords(env, "apply"))
            assert rc == 0 and "## Phase 1: PASS" in out, out[-3000:]
            snaps.append(_snapshot(env))
        assert snaps[0] == snaps[1]
        assert env.roles_state() == "HARDENED_PHASE1" and _workflow_checks_phase1(env) == []
        snaps = []
        for _ in range(2):  # phase 2, then phase 2 again
            rc, out = _step(env, rr.PHASE2, passwords=rr._passwords(env))
            assert rc == 0 and "## Phase 2: PASS" in out, out[-3000:]
            snaps.append(_snapshot(env))
        assert snaps[0] == snaps[1] and _workflow_checks(env, 8) == []
        if state >= 9:
            _apply_0009(env)
        if state >= 10:
            _apply_0010(env)
        _write_runtime_rows(env, state)
    except BaseException:
        rr._destroy_env(env)
        raise
    return env


def _workflow_checks_phase1(env):
    failures = []
    if _catalog_state(env) != "BRIDGE_0001_0008":
        failures.append("catalog_state")
    if rr._server_major_raw() == 18 and env.structure_fingerprint() != PINNED_STRUCTURE[8]:
        failures.append("structure:" + env.structure_fingerprint())
    return failures


@pytest.fixture
def built(request):
    env = _build(request.param)
    try:
        yield request.param, env
    finally:
        rr._destroy_env(env)


# ------------------------------------------------------------------ requirement 1-3: the three exact states

@pytest.mark.parametrize("built", [8, 9, 10], indirect=True, ids=["0001-0008", "0001-0009", "0001-0010"])
def test_each_admitted_state_passes_the_workflow_checks_twice_and_keeps_privileges_and_data(built):
    state, env = built
    assert _workflow_checks(env, state) == []
    _phase2_twice_unchanged(env, state)
    # fcb_app: least privilege, unchanged (#198/#199) - no administrative or ledger-changing privilege
    with _as_app(env) as conn:
        def priv(table, p):
            return conn.execute("SELECT has_table_privilege('fcb_app', %s, %s) AS ok", (table, p)).fetchone()["ok"]
        if state >= 9:
            assert all(priv("oauth_throttle_buckets", p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE"))
            assert not any(priv("oauth_throttle_unlock_audit", p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE"))
        if state >= 10:
            for table in LEDGER:
                assert not any(priv(table, p) for p in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"))
                assert priv(table, "SELECT") == (table != "api_football_cert_plan_usage")
            for table, cols in APP_COLUMN_INSERT.items():
                got = tuple(r["attname"] for r in conn.execute(
                    "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped "
                    "AND has_column_privilege('fcb_app', attrelid, attnum, 'INSERT') ORDER BY attnum", (table,)))
                assert got == cols
            assert not conn.execute("SELECT has_any_column_privilege('fcb_app', 'api_football_cert_plan_usage', "
                                    "'SELECT,INSERT,UPDATE,REFERENCES') AS x").fetchone()["x"]
        assert not conn.execute("SELECT pg_has_role('fcb_app', 'fcb_migrator', 'MEMBER') AS m").fetchone()["m"]
        for p in ("CREATE",):
            assert not conn.execute("SELECT has_schema_privilege('fcb_app', 'public', %s) AS x", (p,)).fetchone()["x"]


def test_pinned_counts_match_the_three_states():
    assert PINNED_COUNTS[8].endswith("schema_migrations_rows=8")
    assert PINNED_COUNTS[9].endswith("schema_migrations_rows=9") and PINNED_COUNTS[10].endswith("schema_migrations_rows=10")


# ------------------------------------------------------------------ requirement 3 / owner condition 1: negatives

def _mig(*statements):
    return [("migrator", s) for s in statements]


def _su(*statements):
    return [("superuser", s) for s in statements]


MUTATIONS_0010 = [
    # (expected failure marker, mutation)
    ("bridge_state", _su("DROP TABLE public.api_football_cert_campaign_outcomes")),                  # partial 0010
    ("bridge_state", _su("DROP TABLE public.oauth_throttle_unlock_audit")),                          # 0010 without full 0009
    ("trigger_set", _su("DROP TRIGGER api_football_cert_plans_stamp ON public.api_football_cert_plans")),
    ("trigger_set", _su("ALTER TABLE public.api_football_cert_plans DISABLE TRIGGER api_football_cert_plans_immutable")),
    ("routine_set", _mig("ALTER FUNCTION public.api_football_cert_attempt_guard() SECURITY INVOKER")),
    ("routine_set", _mig("ALTER FUNCTION public.api_football_cert_stamp() RESET search_path")),
    ("routine_set", _mig("CREATE OR REPLACE FUNCTION public.api_football_cert_reject_mutation() RETURNS trigger "
                         "LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$BEGIN RETURN NULL; END;$$")),
    ("unexpected_relation_grant", _mig("GRANT UPDATE ON public.api_football_cert_plans TO fcb_app")),
    ("unexpected_relation_grant", _mig("GRANT SELECT ON public.api_football_cert_plan_usage TO fcb_app")),
    ("unexpected_relation_grant", _mig("GRANT INSERT ON public.api_football_cert_campaign_attempts TO fcb_app")),
    ("unexpected_relation_grant", _mig("GRANT SELECT ON public.oauth_throttle_unlock_audit TO fcb_app")),
    ("missing_runtime_grant", _mig("REVOKE SELECT ON public.api_football_cert_plans FROM fcb_app")),
    ("column_grant", _mig("GRANT INSERT (seq) ON public.api_football_cert_campaign_attempts TO fcb_app")),
    ("column_grant", _mig("GRANT UPDATE (used) ON public.api_football_cert_plan_usage TO fcb_app")),
    ("column_grant", _mig("REVOKE INSERT (call_id) ON public.api_football_cert_campaign_attempts FROM fcb_app")),
    ("routine_grant", _mig("GRANT EXECUTE ON FUNCTION public.api_football_cert_attempt_guard() TO fcb_app")),
    ("owner_model", _su("ALTER TABLE public.api_football_cert_plans OWNER TO fcb_signer")),
    ("relation_hooks", _mig("CREATE RULE no_more AS ON INSERT TO public.api_football_cert_plans DO INSTEAD NOTHING")),
    ("rows", _su("DELETE FROM public.schema_migrations WHERE version = 10")),                         # rows != catalog
    ("rows", _su("UPDATE public.schema_migrations SET sha256 = repeat('0', 64) WHERE version = 9")),  # checksum
    ("counts", _mig("CREATE INDEX wp009_extra ON public.api_football_cert_plans (budget)")),
]


@pytest.fixture(scope="module")
def _skip_without_psql():
    return None


@pytest.mark.parametrize("marker,mutation", MUTATIONS_0010, ids=[f"{m}-{i}" for i, (m, _) in enumerate(MUTATIONS_0010)])
def test_a_tampered_0010_state_fails_the_workflow_checks(marker, mutation):
    env = _build(10)
    try:
        assert _workflow_checks(env, 10) == []
        for who, statement in mutation:
            if who == "migrator":
                rc, _, err = _migrator_psql(env, "-c", statement)
                assert rc == 0, err
            else:
                env.superuser(statement)
        failures = _workflow_checks(env, 10)
        assert failures, "the tampered state was accepted"
        text = " ".join(failures)
        assert marker in text or (marker == "rows" and "rows" in failures), (marker, failures)
        before = _snapshot(env)
        rc, out = _step(env, rr.PHASE2, passwords=rr._passwords(env))  # the real workflow step refuses
        assert rc != 0 and "## Phase 2: PASS" not in out
        assert _snapshot(env) == before  # and changes nothing
    finally:
        rr._destroy_env(env)


def test_mixed_state_0010_without_0009_is_refused():
    """0010 objects on a database without 0009's: not one of the admitted states."""
    env = rr._create_env()
    try:
        for _ in range(1):
            rc, _, err = rr._roles_tx(env)
            assert rc == 0, err
        rc, _, err = rr._enable_tx(env)
        assert rc == 0, err
        # the 0010 SQL refuses itself without 0009 ...
        rc, _, err = _migrator_psql(env, "-1", "-f", str(AIVEN / "apifootball_cert_ledger_0010_require_0009.sql"))
        assert rc != 0 and "FCB_CERT_LEDGER_REQUIRES_BRIDGE_0009" in err
        # ... and if the 0010 objects were planted anyway, the model refuses the state.
        sql = (ROOT / "sql" / "migrations" / "bridge" / "0010_apifootball_cert_campaign_ledger.sql").read_text()
        rc, _, err = _migrator_psql(env, "-1", "-c", sql)
        assert rc == 0, err
        assert _catalog_state(env) == "BRIDGE_STATE_INVALID" and "bridge_state" in env.roles_state()
        assert _workflow_checks(env, 10) and _workflow_checks(env, 8)
        rc, _, _err = rr._enable_tx(env)
        assert rc != 0
    finally:
        rr._destroy_env(env)


# ------------------------------------------------------------------ owner condition 3: #199 invariants after WP-009

def _budget_worker(dsn, campaign, plan, out):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from apifootball_campaign_kit import load as _load
    mod = _load("apifootball_cert_batch1_wp009_worker", "apifootball_cert_batch1.py")
    os.environ[mod.AUTH_VAR] = "true"

    def connect():
        c = psycopg.connect(dsn, row_factory=dict_row)
        c.commit()
        return c
    ledger = mod.PostgresCampaignLedger(connect, schema="public")
    asyncio.run(ledger.open(campaign, 78, plan))
    got = 0
    for i in range(20):
        try:
            asyncio.run(ledger.reserve(campaign, f"W{i}"))
            got += 1
        except mod.BudgetExhausted:
            break
        except mod.SerializationRefused:
            continue
    out.put(got)


@pytest.mark.parametrize("built", [10], indirect=True, ids=["0001-0010"])
def test_199_invariants_hold_after_wp009_with_a_real_scram_fcb_app(built):
    state, env = built
    _phase2_twice_unchanged(env, state)
    # INVARIANT 1: approved plans are immutable for fcb_app
    for statement in ("INSERT INTO api_football_cert_plans (plan_sha256, plan_kind, budget, approved_by, github_run_id) "
                      f"VALUES ('{'c' * 64}', 'BATCH1', 78, 'x', 1)",
                      "UPDATE api_football_cert_plans SET budget = 78",
                      "DELETE FROM api_football_cert_plans",
                      "TRUNCATE api_football_cert_plans CASCADE",
                      "ALTER TABLE api_football_cert_plans DISABLE TRIGGER ALL",
                      "UPDATE api_football_cert_plan_usage SET used = 0",
                      "SELECT * FROM api_football_cert_plan_usage"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with _as_app(env) as conn:
                conn.execute(statement)
    # INVARIANT 2: atomic budget, 6 real processes (SCRAM fcb_app), 6 campaign_ids of one plan
    _approve_plan(env, "d" * 64, 9)
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_budget_worker, args=(_app_dsn(env), f"camp-wp009-b-{i:03d}", "d" * 64, out)) for i in range(6)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(180)
    assert [p.exitcode for p in procs] == [0] * 6 and sum(out.get(timeout=5) for _ in procs) == 9
    with psycopg.connect(rr._dsn_for(env.dbname), row_factory=dict_row) as conn:
        for plan, budget in (("d" * 64, 9), ("b" * 64, 6)):
            used = conn.execute("SELECT used FROM api_football_cert_plan_usage WHERE plan_sha256 = %s", (plan,)).fetchone()["used"]
            seqs = [r["seq"] for r in conn.execute(
                "SELECT a.seq FROM api_football_cert_campaign_attempts a JOIN api_football_cert_campaigns c USING (campaign_id) "
                "WHERE c.plan_sha256 = %s ORDER BY 1", (plan,))]
            assert used == len(seqs) and seqs == list(range(1, used + 1)) and used <= budget
    # INVARIANT 3: evidence cannot vanish, be reused or be rewritten
    for statement, error in (
            ("UPDATE api_football_cert_campaign_attempts SET call_id = 'X'", psycopg.errors.InsufficientPrivilege),
            ("DELETE FROM api_football_cert_campaign_attempts", psycopg.errors.InsufficientPrivilege),
            ("UPDATE api_football_cert_campaign_outcomes SET http_status = 500", psycopg.errors.InsufficientPrivilege),
            ("INSERT INTO api_football_cert_campaign_attempts (campaign_id, seq, call_id) VALUES ('camp-wp009-rows-1', 1, 'R')",
             psycopg.errors.InsufficientPrivilege),
            ("INSERT INTO api_football_cert_campaign_outcomes (campaign_id, seq, outcome, http_status) "
             "VALUES ('camp-wp009-rows-1', 1, 'TRANSPORT_ERROR', NULL)", psycopg.errors.UniqueViolation)):
        with pytest.raises(error):
            with _as_app(env) as conn:
                conn.execute(statement)
    states = asyncio.run(_ledger(env).attempt_states("camp-wp009-rows-1"))
    assert [(s["call_id"], s["state"]) for s in states] == [("C01", "RESPONSE_RECEIVED"), ("C02", "RESERVED_UNKNOWN")]
    assert _workflow_checks(env, 10) == []  # the runtime writes did not alter the model


def test_helper_admits_exactly_the_three_states_from_the_rows():
    for n in (8, 9, 10):
        rows = _helper("expected-migrations", str(n)).stdout.split()
        evidence = "\n".join([f"schema_migrations_rows={n}", *rows])
        assert _helper("bridge-state", stdin=evidence).stdout.strip() == str(n)
    ten = _helper("expected-migrations", "10").stdout.split()
    for bad in ([*ten[:8], ten[9]],                                  # 0010 without 0009
                [*ten[:9], ten[9].rsplit("=", 1)[0] + "=" + "0" * 64],  # tampered checksum
                ten[:7]):                                            # 0001-0007
        evidence = "\n".join([f"schema_migrations_rows={len(bad)}", *bad])
        assert _helper("bridge-state", stdin=evidence).returncode == 1
