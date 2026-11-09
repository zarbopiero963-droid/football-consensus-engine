"""FCB-WP-009 on real PostgreSQL: the owner-gated roles transaction and the runtime as fcb_app.

Production shape: a restored bridge 0001-0008 owned by a NON-superuser CREATEROLE identity that
owns the database (Aiven's avnadmin). This module creates exactly that in a throwaway database,
runs the roles transaction with the files and the order of .github/workflows/fcb-aiven-defaultdb-roles.yml
(its own $GUARD text, minus the PG18/TLS predicates a local server cannot meet), then:

* runs every runtime database path through a real password LOGIN as fcb_app: OAuth codes,
  access/refresh tokens, refresh rotation, DCR; browser lease register/renew/stop/reaper;
  both shared quota coordinators; schema readiness;
* proves what fcb_app, fcb_migrator and fcb_signer cannot do (DDL, legacy/signer tables);
* proves the transaction is idempotent, changes no schema evidence, keeps the administrative
  identity working (rollback path) and refuses unexpected states before writing.

Every role created here has a random password (CI authenticates with passwords) and is dropped.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

psycopg = pytest.importorskip("psycopg")
import yaml  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.sql import SQL, Identifier, Literal  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AIVEN = ROOT / "sql" / "aiven"
BRIDGE_SCRIPT = AIVEN / "aiven_migrations_bridge_0001_0008.sql"
WORKFLOW = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-roles.yml"
HELPER = ROOT / "scripts" / "fcb_db_roles.py"
ROLES = ("fcb_app", "fcb_signer", "fcb_migrator")
TX_FILES = ("roles_apply_guard.sql", "upsert_bridge_roles.sql", "prepare_bridge_roles.sql",
            "finalize_bridge_privileges.sql", "assert_bridge_counts.sql", "assert_bridge_roles.sql")
RUNTIME_TABLES = (
    "oauth_authorization_codes", "oauth_access_tokens", "oauth_refresh_tokens", "oauth_claude_dcr_clients",
    "browser_session_leases", "api_football_quota_counters", "api_football_quota_leases",
    "api_football_quota_attempts", "sxbet_rate_attempts", "sxbet_rate_cooldowns",
)
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://bridge.example/mcp"
_TLS_PG18 = (" OR pg_catalog.current_setting('server_version_num')::pg_catalog.int4 OPERATOR(pg_catalog.<) 180000"
             " OR pg_catalog.current_setting('server_version_num')::pg_catalog.int4 OPERATOR(pg_catalog.>) 189999"
             " OR NOT COALESCE((SELECT s.ssl FROM pg_catalog.pg_stat_ssl s WHERE s.pid OPERATOR(pg_catalog.=) "
             "pg_catalog.pg_backend_pid()), false)")

pytestmark = [
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                       reason="requires isolated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(os.environ.get("FCB_REQUIRE_PSQL") != "1", reason="requires real psql in CI"),
]


def _dsn_for(dbname, **override):
    params = conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
    params["dbname"] = dbname
    params.update(override)
    return make_conninfo(**params)


def _drop_bridge_roles():
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        for role in ROLES:
            conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))


def _workflow_guard(dbname):
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["roles"]["steps"] if s.get("name", "").startswith("Apply roles"))
    line = next(l.strip() for l in step["run"].splitlines() if l.strip().startswith('GUARD="DO'))
    guard = line[len('GUARD="'):-1].replace("\\$", "$")
    assert guard.count(_TLS_PG18) == 1
    return guard.replace(_TLS_PG18, "").replace("$EXPECTED_DB", dbname)


class Env:
    """One throwaway database restored to bridge 0001-0008 by its non-superuser owner."""

    def __init__(self):
        self.dbname = "wp009_" + uuid.uuid4().hex[:12]
        self.admin_role = "wp009_admin_" + uuid.uuid4().hex[:8]
        self.admin_password = secrets.token_urlsafe(24)
        self.passwords = {"fcb_app": secrets.token_urlsafe(32), "fcb_migrator": secrets.token_urlsafe(32)}
        self.admin_dsn = _dsn_for(self.dbname, user=self.admin_role, password=self.admin_password)

    def dsn(self, role):
        return _dsn_for(self.dbname, user=role, password=self.passwords[role])

    def connect(self, role="fcb_app", **kw):
        dsn = self.admin_dsn if role == "admin" else self.dsn(role)
        return psycopg.connect(dsn, row_factory=dict_row, **kw)

    def psql(self, *args, pgoptions="-c search_path=public", env_extra=None, dsn=None):
        env = {**os.environ, "PGOPTIONS": pgoptions, **(env_extra or {})}
        for var in ("FCB_APP_DB_VERIFIER", "FCB_MIGRATOR_DB_VERIFIER"):
            env.setdefault(var, "")
            if not env[var]:
                env.pop(var)
        out = subprocess.run(["psql", dsn or self.admin_dsn, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", *args],
                             env=env, capture_output=True, text=True, timeout=180)
        return out.returncode, out.stdout.strip(), out.stderr

    def ok(self, *args, **kw):
        rc, out, err = self.psql(*args, **kw)
        assert rc == 0, err
        return out

    def state(self):
        out = self.ok("-f", str(AIVEN / "defaultdb_precheck.sql"))
        if out == "TRACKED":
            out = self.ok("-f", str(AIVEN / "defaultdb_precheck_tracked.sql"))
        return out

    def roles_state(self):
        return self.ok("-f", str(AIVEN / "verify_bridge_roles.sql"),
                       pgoptions="-c search_path=public -c default_transaction_read_only=on")

    def diagnose(self):
        return self.ok("-f", str(AIVEN / "diagnose_bridge_roles.sql"),
                       pgoptions="-c search_path=public -c default_transaction_read_only=on")

    def schema_evidence(self):
        return self.ok("-F", "=", "-f", str(AIVEN / "verify_bridge_schema.sql"))

    def verifiers(self):
        env = {**os.environ, "FCB_APP_DB_PASSWORD": self.passwords["fcb_app"],
               "FCB_MIGRATOR_DB_PASSWORD": self.passwords["fcb_migrator"]}
        out = {}
        for role, var, target in (("fcb_app", "FCB_APP_DB_PASSWORD", "FCB_APP_DB_VERIFIER"),
                                  ("fcb_migrator", "FCB_MIGRATOR_DB_PASSWORD", "FCB_MIGRATOR_DB_VERIFIER")):
            proc = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "verifier", var],
                                  env=env, capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, proc.stderr
            out[target] = proc.stdout.strip()
        return out


def _expected_migrations():
    import hashlib
    rows = []
    for f in sorted((ROOT / "sql" / "migrations" / "bridge").glob("000[1-8]_*.sql")):
        rows.append(f"{int(f.name[:4])}={f.name}={hashlib.sha256(f.read_bytes()).hexdigest()}")
    return " ".join(rows)


def _roles_tx(env: Env, *, counts, verifiers=None, migrations=None, extra_before=(), guard=True):
    args = ["-v", f"expected_counts={counts}", "-v", f"expected_migrations={migrations or _expected_migrations()}", "-1"]
    if guard:
        args += ["-c", _workflow_guard(env.dbname)]
    for name in TX_FILES:
        if name == "upsert_bridge_roles.sql":
            for c in extra_before:
                args += ["-c", c]
        args += ["-f", str(AIVEN / name)]
    return env.psql(*args, env_extra=verifiers if verifiers is not None else env.verifiers())


def _create_env() -> Env:
    env = Env()
    _drop_bridge_roles()
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
            Identifier(env.admin_role), Literal(env.admin_password)))
        conn.execute(SQL("CREATE DATABASE {} OWNER {}").format(Identifier(env.dbname), Identifier(env.admin_role)))
    env.ok("-1", "-f", str(BRIDGE_SCRIPT))
    assert env.state() == "BRIDGE_0001_0008"
    env.counts = " ".join(env.schema_evidence().splitlines()[:7])
    return env


def _destroy_env(env: Env):
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(Identifier(env.dbname)))
        for role in ROLES:
            conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))
        conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(env.admin_role)))


@pytest.fixture
def hardened():
    """The production path: restored 0001-0008, then the roles transaction (first run)."""
    env = _create_env()
    try:
        env.roles_before = env.roles_state()
        env.schema_before = env.schema_evidence()
        rc, _, err = _roles_tx(env, counts=env.counts)
        assert rc == 0, err
        yield env
    finally:
        _destroy_env(env)


@pytest.fixture
def fresh():
    """A restored 0001-0008 database with no roles, for refusal tests (function scope)."""
    env = _create_env()
    try:
        yield env
    finally:
        _destroy_env(env)


# ------------------------------------------------------------------ the transaction itself

def test_first_run_reaches_the_hardened_model_without_touching_the_schema(hardened):
    assert hardened.roles_before == "ROLES_ABSENT"
    assert hardened.roles_state() == "HARDENED"
    # Owners and grants changed; tables, columns, constraints, indexes, migrations did not.
    assert hardened.schema_evidence() == hardened.schema_before
    report = hardened.diagnose()
    assert "owners relations fcb_migrator=" in report and "session=" not in report.split("owners relations")[1].split("\n")[0]
    assert "role fcb_signer exists=true login=false" in report
    assert "role fcb_app exists=true login=true superuser=false createrole=false createdb=false" in report
    assert report.splitlines()[-1] == "HARDENED"


def test_second_run_is_a_no_op(hardened):
    first = hardened.diagnose()
    with hardened.connect("admin") as conn:
        applied = conn.execute("SELECT pg_catalog.string_agg(m.applied_at::text, ',' ORDER BY m.version) AS a "
                               "FROM public.schema_migrations m").fetchone()["a"]
    rc, _, err = _roles_tx(hardened, counts=hardened.counts)
    assert rc == 0, err
    assert hardened.diagnose() == first
    assert hardened.schema_evidence() == hardened.schema_before
    with hardened.connect("admin") as conn:
        assert conn.execute("SELECT pg_catalog.string_agg(m.applied_at::text, ',' ORDER BY m.version) AS a "
                            "FROM public.schema_migrations m").fetchone()["a"] == applied
    # The password set by the re-run still works (same secret, new salt).
    with hardened.connect("fcb_app") as conn:
        assert conn.execute("SELECT CURRENT_USER AS u").fetchone()["u"] == "fcb_app"


def test_after_hardening_the_restore_precheck_refuses(hardened):
    """The restore workflow must not run on a hardened database as the administrator; future
    migrations need a migrator-run workflow (documented residual in the runbook)."""
    assert hardened.state().startswith("REFUSE_")


def test_administrative_identity_keeps_working_for_rollback(hardened):
    from bridge import schema_readiness
    assert schema_readiness.evaluate(lambda: hardened.connect("admin"))["state"] == "READY"
    with hardened.connect("admin") as conn:
        conn.execute("INSERT INTO oauth_claude_dcr_clients(client_id_hash,redirect_uri,expires_at) "
                     "VALUES ('rollback-probe','https://example.invalid/cb',now()+interval '1 hour')")
        conn.rollback()


def test_both_identities_pass_the_workflow_login_check(hardened):
    url = _dsn_for(hardened.dbname, user=hardened.admin_role, password=hardened.admin_password)
    params = conninfo_to_dict(url)
    host = params.get("host", "")
    uri = (f"postgresql://{hardened.admin_role}:{hardened.admin_password}@/{hardened.dbname}"
           f"?host={host}&port={params.get('port', '5432')}")
    env = {**os.environ, "FCB_DEFAULTDB_URL": uri, "PYTHONPATH": str(ROOT),
           "FCB_APP_DB_PASSWORD": hardened.passwords["fcb_app"],
           "FCB_MIGRATOR_DB_PASSWORD": hardened.passwords["fcb_migrator"]}
    proc = subprocess.run([sys.executable, str(HELPER), "login-check"], env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "login fcb_app: tls=False schema_migrations_rows=8 readiness=READY create_denied=True " \
           "legacy_read_denied=True" in proc.stdout
    assert "login fcb_migrator: tls=False schema_migrations_rows=8 can_create=True" in proc.stdout
    for secret in (*hardened.passwords.values(), hardened.admin_password):
        assert secret not in proc.stdout + proc.stderr
    # The workflow requires TLS: a plaintext session is refused by the same check.
    proc = subprocess.run([sys.executable, str(HELPER), "login-check", "--require-tls"], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1 and "FCB_LOGIN_NOT_TLS" in proc.stderr
    with hardened.connect("admin") as conn:  # the probes were rolled back
        assert conn.execute("SELECT pg_catalog.to_regclass('public.fcb_wp009_login_probe') AS r").fetchone()["r"] is None


# ------------------------------------------------------------------ runtime through fcb_app

@pytest.fixture
def app_runtime(hardened, monkeypatch):
    from bridge import oauth_store, session_leases

    def oauth_conn():
        return psycopg.connect(hardened.dsn("fcb_app"), row_factory=dict_row,
                               connect_timeout=oauth_store.CONNECT_TIMEOUT_SECONDS,
                               options=f"-c statement_timeout={oauth_store.STATEMENT_TIMEOUT_MS}")

    def app_connect():
        return hardened.connect("fcb_app")

    monkeypatch.setattr(oauth_store, "_conn", oauth_conn)
    monkeypatch.setattr(session_leases, "connect", app_connect)
    with app_connect() as conn:
        assert conn.execute("SELECT CURRENT_USER AS u, SESSION_USER AS s").fetchone() == {"u": "fcb_app", "s": "fcb_app"}
    return app_connect


def test_schema_readiness_is_ready_as_fcb_app(app_runtime):
    from bridge import schema_readiness
    result = schema_readiness.evaluate(app_runtime)
    assert result["state"] == "READY" and not result["missing_tables"]


def test_oauth_code_exchange_tokens_and_refresh_rotation_as_fcb_app(app_runtime):
    from bridge import oauth_store
    code = "code-" + uuid.uuid4().hex
    oauth_store.save_code(code, client_id="cli", redirect_uri=CLAUDE_REDIRECT, code_challenge="CH",
                          resource=RESOURCE, scope="browser:use offline_access")
    access, refresh = "acc-" + uuid.uuid4().hex, "ref-" + uuid.uuid4().hex
    issued = oauth_store.exchange_code(code, client_id="cli", redirect_uri=CLAUDE_REDIRECT,
                                       verifier_matches=lambda ch: ch == "CH", access=access, refresh=refresh)
    assert issued == {"resource": RESOURCE, "scope": "browser:use offline_access"}
    assert oauth_store.valid_access_token(access, RESOURCE, "browser:use")
    assert not oauth_store.valid_access_token(access, "https://other.example/mcp", "browser:use")
    assert not oauth_store.valid_access_token(access, RESOURCE, "admin")
    # Single use: a replayed code is refused and burns nothing else.
    assert oauth_store.exchange_code(code, client_id="cli", redirect_uri=CLAUDE_REDIRECT,
                                     verifier_matches=lambda ch: True, access="x" + access, refresh="x" + refresh) is None
    # A wrong PKCE verifier burns the code without issuing tokens.
    code2 = "code-" + uuid.uuid4().hex
    oauth_store.save_code(code2, client_id="cli", redirect_uri=CLAUDE_REDIRECT, code_challenge="CH",
                          resource=RESOURCE, scope="browser:use")
    assert oauth_store.exchange_code(code2, client_id="cli", redirect_uri=CLAUDE_REDIRECT,
                                     verifier_matches=lambda ch: False, access="y" + access, refresh="y" + refresh) is None
    assert not oauth_store.valid_access_token("y" + access, RESOURCE, "browser:use")
    # Refresh rotation in one transaction; the old refresh token is revoked.
    access2, refresh2 = "acc-" + uuid.uuid4().hex, "ref-" + uuid.uuid4().hex
    assert oauth_store.refresh_and_issue(refresh, client_id="cli", access=access2, refresh=refresh2) == \
        {"resource": RESOURCE, "scope": "browser:use offline_access"}
    assert oauth_store.valid_access_token(access2, RESOURCE, "browser:use")
    assert oauth_store.refresh_and_issue(refresh, client_id="cli", access="z" + access2, refresh="z" + refresh2) is None
    assert oauth_store.refresh_and_issue(refresh2, client_id="other", access="w" + access2, refresh="w" + refresh2) is None
    # Legacy single-statement helpers used by the token endpoint paths.
    code3 = "code-" + uuid.uuid4().hex
    oauth_store.save_code(code3, client_id="cli", redirect_uri=CLAUDE_REDIRECT, code_challenge="CH",
                          resource=RESOURCE, scope="browser:use")
    assert oauth_store.consume_code(code3)["client_id"] == "cli"
    assert oauth_store.consume_code(code3) is None
    oauth_store.save_access_token("solo-" + access, client_id="cli", resource=RESOURCE, scope="browser:use")
    assert oauth_store.valid_access_token("solo-" + access, RESOURCE, "browser:use")
    oauth_store.save_refresh_token("solo-" + refresh, client_id="cli", resource=RESOURCE, scope="browser:use")
    assert oauth_store.rotate_refresh_token("solo-" + refresh)["client_id"] == "cli"
    assert oauth_store.rotate_refresh_token("solo-" + refresh) is None


def test_oauth_dcr_register_validate_and_expiry_cleanup_as_fcb_app(app_runtime, hardened):
    from bridge import oauth_store
    with hardened.connect("admin") as conn:  # an expired registration for the cleanup DELETE
        conn.execute("INSERT INTO oauth_claude_dcr_clients(client_id_hash,redirect_uri,expires_at) "
                     "VALUES ('expired-probe','https://example.invalid/cb',now()-interval '1 second')")
    client_id = oauth_store.register_claude_dcr_client(CLAUDE_REDIRECT)
    assert oauth_store.valid_claude_dcr_client(client_id, CLAUDE_REDIRECT)
    assert not oauth_store.valid_claude_dcr_client(client_id, "https://evil.example/cb")
    with hardened.connect("admin") as conn:
        assert conn.execute("SELECT count(*) AS n FROM oauth_claude_dcr_clients "
                            "WHERE client_id_hash='expired-probe'").fetchone()["n"] == 0


def _expire(hardened, session_id):
    with hardened.connect("fcb_app") as conn:
        conn.execute("UPDATE browser_session_leases SET lease_until=NOW()-INTERVAL '1 second', "
                     "last_activity_at=NOW()-INTERVAL '10 minutes' WHERE session_id=%s", (session_id,))


def test_browser_lease_lifecycle_and_reaper_as_fcb_app(app_runtime, hardened, monkeypatch):
    from bridge import session_leases
    from bridge.browser_use import BrowserUseBridge
    sid = "wp009-" + uuid.uuid4().hex[:8]
    session_leases.register(sid, "worker-a")
    session_leases.renew(sid, "worker-a")
    with pytest.raises(RuntimeError):
        session_leases.renew(sid, "worker-b")
    assert session_leases.get_session(sid)["state"] == "ACTIVE"
    assert any(s["session_id"] == sid for s in session_leases.list_sessions())
    assert session_leases.claim_stop(sid, "worker-a")
    session_leases.renew_stop(sid, "worker-a")
    session_leases.finish(sid, "worker-a", True)
    assert session_leases.get_session(sid)["state"] == "STOPPED"
    assert session_leases.state_summary().get("STOPPED", 0) >= 1
    assert session_leases.healthcheck()
    # Reaper: an orphan whose stop fails is durable and retried after a restart.
    orphan = "wp009-orphan-" + uuid.uuid4().hex[:8]
    session_leases.register(orphan, "dead-worker")
    _expire(hardened, orphan)
    first = BrowserUseBridge()
    monkeypatch.setattr(first, "_stop_remote", AsyncMock(side_effect=RuntimeError("provider unavailable")))
    assert asyncio.run(first.reap())["failed"]
    assert session_leases.get_session(orphan)["state"] == "STOP_FAILED"
    _expire(hardened, orphan)
    restarted = BrowserUseBridge()
    stop = AsyncMock()
    monkeypatch.setattr(restarted, "_stop_remote", stop)
    assert asyncio.run(restarted.reap()) == {"stopped": [orphan], "failed": []}
    assert session_leases.get_session(orphan)["state"] == "STOPPED"
    assert session_leases.claim_stale("later") == []


def test_api_football_quota_coordinator_as_fcb_app(app_runtime):
    from bridge.api_football.quota import PostgresQuota, QuotaExceeded, QuotaLimits
    quota = PostgresQuota(QuotaLimits(2, 100, 2), connect=app_runtime)
    secret = "wp009-key-" + uuid.uuid4().hex

    async def flow():
        await quota.prepare()
        lease = await quota.acquire(secret)
        snap = await quota.snapshot(secret)
        assert snap["active_leases"] == 1 and snap["remaining_day"] == 99
        await quota.release(lease)
        await quota.release(await quota.acquire(secret))
        with pytest.raises(QuotaExceeded):
            await quota.acquire(secret)
        await quota.mark_exhausted(secret, "day")
        await quota.mark_exhausted(secret, "minute")
        assert (await quota.snapshot(secret))["remaining_day"] == 0

    asyncio.run(flow())


def test_sxbet_rate_coordinator_as_fcb_app(app_runtime):
    from bridge.sxbet.ratelimit import PostgresRateCoordinator, RateLimited, build_plan
    coordinator = PostgresRateCoordinator(connect=app_runtime)
    plan = build_plan(network="testnet", api_key="wp009-" + uuid.uuid4().hex, max_rpm=60,
                      path="/markets/active", auth=False)

    async def flow():
        await coordinator.prepare()
        await coordinator.acquire(plan)
        await coordinator.record_rate_limited(plan, 30)
        with pytest.raises(RateLimited):
            await coordinator.acquire(plan)

    asyncio.run(flow())


# ------------------------------------------------------------------ what each role cannot do

@pytest.mark.parametrize("statement", [
    "CREATE TABLE public.wp009_forbidden(id integer)",
    "CREATE TABLE wp009_forbidden(id integer)",
    "CREATE SCHEMA wp009_forbidden",
    "CREATE FUNCTION public.wp009_f() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
    "CREATE VIEW public.wp009_v AS SELECT 1 AS x",
    "ALTER TABLE oauth_access_tokens ADD COLUMN wp009 integer",
    "ALTER TABLE browser_session_leases OWNER TO fcb_app",
    "DROP TABLE oauth_refresh_tokens",
    "DROP TABLE schema_migrations",
    "TRUNCATE api_football_quota_counters",
    "CREATE INDEX wp009_idx ON oauth_access_tokens(client_id)",
    "INSERT INTO schema_migrations(version,name,sha256) VALUES (99,'x','y')",
    "UPDATE schema_migrations SET sha256='x'",
    "DELETE FROM schema_migrations",
    "SELECT nextval('ingest_runs_id_seq')",
    "SELECT * FROM model_snapshots",
    "SELECT * FROM source_pilot_runs",
    "SELECT * FROM canonical_entities",
    "SELECT * FROM decision_requests",
    "SELECT * FROM evaluation_protocols",
    "SELECT * FROM runtime_state",
    "ALTER DEFAULT PRIVILEGES FOR ROLE fcb_migrator GRANT SELECT ON TABLES TO fcb_app",
    "GRANT fcb_migrator TO fcb_app",
    "SET ROLE fcb_migrator",
])
def test_fcb_app_cannot_change_the_schema_or_read_outside_its_allowlist(hardened, statement):
    with hardened.connect("fcb_app") as conn:
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.InvalidGrantOperation)):
            conn.execute(statement)


def test_fcb_app_reaches_every_legacy_table_only_as_denied(hardened):
    with hardened.connect("admin") as conn:
        tables = [r["relname"] for r in conn.execute(
            "SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY 1").fetchall()]
    assert len(tables) == 33
    for table in tables:
        with hardened.connect("fcb_app") as conn:
            allowed = conn.execute("SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS a",
                                   (f"public.{table}",)).fetchone()["a"]
            assert allowed == (table in RUNTIME_TABLES or table == "schema_migrations"), table
            for priv in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not conn.execute("SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, %s) AS a",
                                        (f"public.{table}", priv)).fetchone()["a"], (table, priv)


def test_signer_tables_created_later_are_not_readable_by_fcb_app(hardened):
    """Signer objects never belong in defaultdb; if one ever appeared there (created by the
    migrator or by the administrator), neither default privileges nor PUBLIC hand it to fcb_app."""
    signer_sql = (ROOT / "sql" / "migrations" / "signer" / "0001_signer_ledger_anchor.sql").read_text()
    with hardened.connect("admin") as conn:
        conn.execute("SET ROLE fcb_migrator")
        conn.execute(signer_sql)
        conn.execute("RESET ROLE")
        conn.execute("CREATE TABLE public.wp009_admin_made(id integer)")
        conn.commit()
    with hardened.connect("fcb_app") as app:
        for table in ("signer_ledger_anchors", "signer_ledger_writer_leases", "wp009_admin_made"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute(f"SELECT * FROM public.{table}")
            app.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("SELECT public.signer_ledger_anchors_append_only()")
        app.rollback()
        # A GRANT by a non-owner grants nothing (PostgreSQL only warns).
        app.execute("GRANT SELECT ON oauth_access_tokens TO fcb_signer")
        app.commit()
    with hardened.connect("admin") as conn:
        assert not conn.execute("SELECT pg_catalog.has_table_privilege('fcb_signer', 'public.oauth_access_tokens', "
                                "'SELECT') AS a").fetchone()["a"]


def test_fcb_signer_has_no_bridge_access(hardened):
    with hardened.connect("admin") as conn:
        for table in (*RUNTIME_TABLES, "schema_migrations", "model_snapshots"):
            assert not conn.execute("SELECT pg_catalog.has_table_privilege('fcb_signer', %s, 'SELECT') AS a",
                                    (f"public.{table}",)).fetchone()["a"], table
        assert not conn.execute("SELECT pg_catalog.has_schema_privilege('fcb_signer', 'public', 'USAGE') AS a").fetchone()["a"]
        # NOLOGIN: no session can be opened as the signer.
        assert not conn.execute("SELECT r.rolcanlogin FROM pg_catalog.pg_roles r WHERE r.rolname='fcb_signer'").fetchone()["rolcanlogin"]
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_dsn_for(hardened.dbname, user="fcb_signer", password="x" * 32)).close()


def test_fcb_migrator_owns_the_bridge_but_has_no_cluster_privilege(hardened):
    with hardened.connect("fcb_migrator") as conn:
        row = conn.execute("SELECT r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolreplication, r.rolbypassrls "
                           "FROM pg_catalog.pg_roles r WHERE r.rolname = CURRENT_USER").fetchone()
        assert not any(row.values())
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE ROLE wp009_escalate")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE SCHEMA wp009_forbidden")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SET ROLE fcb_app")


# ------------------------------------------------------------------ refusals before any write

def _nothing_changed(env: Env):
    assert env.roles_state() == "ROLES_ABSENT"
    assert env.state() == "BRIDGE_0001_0008"


def test_missing_verifiers_abort_before_commit(fresh):
    rc, _, err = _roles_tx(fresh, counts=fresh.counts, verifiers={})
    assert rc != 0
    _nothing_changed(fresh)


def test_malformed_or_equal_verifiers_abort_before_commit(fresh):
    good = fresh.verifiers()
    for bad in ({**good, "FCB_APP_DB_VERIFIER": "plaintext-password-not-a-verifier-000000"},
                {**good, "FCB_MIGRATOR_DB_VERIFIER": good["FCB_APP_DB_VERIFIER"]}):
        rc, _, err = _roles_tx(fresh, counts=fresh.counts, verifiers=bad)
        assert rc != 0 and "FCB_ROLE_VERIFIER_INVALID" in err
        _nothing_changed(fresh)


def test_wrong_counts_or_migrations_abort_before_commit(fresh):
    rc, _, err = _roles_tx(fresh, counts=fresh.counts.replace("tables=33", "tables=34"))
    assert rc != 0 and "FCB_" in err
    _nothing_changed(fresh)
    expected = _expected_migrations()
    for wrong in (expected.rsplit(" ", 1)[0],                       # 0008 missing from the repository list
                  expected[:-1] + ("0" if expected[-1] != "0" else "1"),  # a different 0008 checksum
                  expected.replace("1=0001_", "9=0001_", 1)):       # renumbered
        rc, _, err = _roles_tx(fresh, counts=fresh.counts, migrations=wrong)
        assert rc != 0 and "FCB_ROLES_MIGRATIONS_GUARD_FAILED" in err
        _nothing_changed(fresh)


def test_object_of_a_third_owner_aborts_before_commit(fresh):
    other = "wp009_other_" + uuid.uuid4().hex[:8]
    with psycopg.connect(_dsn_for(fresh.dbname), autocommit=True) as conn:
        conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(other)))
        conn.execute(SQL("ALTER TABLE public.runtime_state OWNER TO {}").format(Identifier(other)))
    try:
        rc, _, err = _roles_tx(fresh, counts=fresh.counts)
        assert rc != 0 and "FCB_ROLES_OWNER_GUARD_FAILED" in err
        assert fresh.roles_state() == "ROLES_ABSENT"
    finally:
        with psycopg.connect(_dsn_for(fresh.dbname), autocommit=True) as conn:
            conn.execute(SQL("ALTER TABLE public.runtime_state OWNER TO {}").format(Identifier(fresh.admin_role)))
            conn.execute(SQL("DROP ROLE {}").format(Identifier(other)))


def test_preexisting_role_with_extra_privilege_or_membership_is_refused(fresh):
    """A pre-existing fcb_app that is CREATEDB, or a third member of fcb_migrator, must abort."""
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("CREATE ROLE fcb_app NOLOGIN CREATEDB")
        conn.execute(SQL("GRANT fcb_app TO {} WITH ADMIN TRUE").format(Identifier(fresh.admin_role)))
    rc, _, err = _roles_tx(fresh, counts=fresh.counts)
    assert rc != 0 and "FCB_ROLES_MODEL_MISMATCH" in err and "role_attributes" in err
    assert fresh.roles_state().startswith("NOT_HARDENED:")
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("ALTER ROLE fcb_app NOCREATEDB")
        conn.execute("CREATE ROLE fcb_migrator NOLOGIN")
        conn.execute(SQL("GRANT fcb_migrator TO {} WITH ADMIN TRUE").format(Identifier(fresh.admin_role)))
        conn.execute("GRANT fcb_migrator TO fcb_app")
    rc, _, err = _roles_tx(fresh, counts=fresh.counts)
    assert rc != 0 and "FCB_ROLES_MODEL_MISMATCH" in err
    assert "role_member_of_other_role" in err and "unexpected_migrator_member" in err
    assert fresh.state() == "BRIDGE_0001_0008"  # ownership untouched


def test_workflow_guard_refuses_a_wrong_database(fresh):
    guard = _workflow_guard("defaultdb_not_this_one")
    rc, _, err = fresh.psql("-c", guard)
    assert rc != 0 and "FCB_TARGET_GUARD_FAILED" in err


def test_in_transaction_guards_alone_accept_the_admin_and_refuse_a_set_role_session(fresh):
    # Without the outer $GUARD the in-file guards still run; a plain administrative session passes.
    rc, _, err = _roles_tx(fresh, counts=fresh.counts, guard=False)
    assert rc == 0, err
    # A SET ROLE session (CURRENT_USER <> SESSION_USER) is refused before any write.
    args = ["-v", f"expected_counts={fresh.counts}", "-v", f"expected_migrations={_expected_migrations()}", "-1",
            "-c", "SET ROLE fcb_migrator"]
    for name in TX_FILES:
        args += ["-f", str(AIVEN / name)]
    rc, _, err = fresh.psql(*args, env_extra=fresh.verifiers())
    assert rc != 0 and "FCB_ROLES_SESSION_GUARD_FAILED" in err
