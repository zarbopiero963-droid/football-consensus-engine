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

# Aiven defaultdb (read-only facts, 2026-11-09): createrole_self_grant='inherit,set' at server level,
# public owned by pg_database_owner, database owned by avnadmin (non-superuser, CREATEROLE). The
# admin sessions below carry the same setting (as a client option, so CI mirrors it too; the
# throwaway cluster used for the local evidence also sets it server-wide).
AIVEN_SELF_GRANT = "-c createrole_self_grant=inherit,set"
ADMIN_OPTIONS = f"-c search_path=public {AIVEN_SELF_GRANT}"
READ_ONLY_OPTIONS = f"{ADMIN_OPTIONS} -c default_transaction_read_only=on"

RESTORE_PINNED_FINGERPRINT = "b825ab11029502140b038ce6d3add670"  # fcb-aiven-defaultdb-restore.yml, PG18
EXPECTED_MEMBERSHIP_LINES = [
    "membership fcb_app <- session admin=true inherit=false set=false grantor=bootstrap",
    "membership fcb_migrator <- session admin=false inherit=true set=true grantor=session",
    "membership fcb_migrator <- session admin=true inherit=false set=false grantor=bootstrap",
    "membership fcb_signer <- session admin=true inherit=false set=false grantor=bootstrap",
]
# Effective reach between the four identities: only the administrative session reaches the
# migrator (privileges + SET ROLE); it is MEMBER (ADMIN only) of fcb_app/fcb_signer, without
# their privileges and without SET ROLE. No bridge role reaches anything.
EXPECTED_EFFECTIVE_LINES = [
    f"effective {a} -> {b} member={m} inherits_privileges={u} can_set_role={c}"
    for a, b, m, u, c in (
        ("session", "fcb_migrator", "true", "true", "true"),
        ("session", "fcb_app", "true", "false", "false"),
        ("session", "fcb_signer", "true", "false", "false"),
        *((x, y, "false", "false", "false")
          for x in ("fcb_app", "fcb_migrator", "fcb_signer")
          for y in ("session", "fcb_app", "fcb_migrator", "fcb_signer") if x != y),
    )
]


def _server_major(env):
    with env.connect("admin") as conn:
        return conn.info.server_version // 10000


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
        if role == "admin":
            kw.setdefault("options", AIVEN_SELF_GRANT)
            return psycopg.connect(self.admin_dsn, row_factory=dict_row, **kw)
        return psycopg.connect(self.dsn(role), row_factory=dict_row, **kw)

    def psql(self, *args, pgoptions=ADMIN_OPTIONS, env_extra=None, dsn=None):
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
                       pgoptions=READ_ONLY_OPTIONS)

    def diagnose(self):
        return self.ok("-f", str(AIVEN / "diagnose_bridge_roles.sql"),
                       pgoptions=READ_ONLY_OPTIONS)

    def structure_fingerprint(self):
        return self.ok("-f", str(AIVEN / "fingerprint_bridge_structure.sql"), pgoptions=READ_ONLY_OPTIONS)

    def full_fingerprint(self):
        return self.ok("-f", str(AIVEN / "fingerprint_bridge_schema.sql"), pgoptions=READ_ONLY_OPTIONS)

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
    # The Aiven shape: public owned by pg_database_owner, the admin owns the database, self-grant on.
    shape = env.ok("-c", "SELECT pg_catalog.pg_get_userbyid(n.nspowner)::text || ' ' || "
                         "(d.datdba = (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname = SESSION_USER))::text "
                         "|| ' ' || pg_catalog.current_setting('createrole_self_grant') || ' ' || r.rolsuper::text "
                         "|| ' ' || r.rolcreaterole::text FROM pg_catalog.pg_namespace n, pg_catalog.pg_database d, "
                         "pg_catalog.pg_roles r WHERE n.nspname = 'public' AND d.datname = pg_catalog.current_database() "
                         "AND r.rolname = SESSION_USER")
    assert shape == "pg_database_owner true inherit,set false true", shape
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
        env.structure_before = env.structure_fingerprint()
        env.full_before = env.full_fingerprint()
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
    assert "createrole_self_grant=inherit,set" in report
    assert "public_schema_owner=pg_database_owner" in report and "database_owner=session" in report
    # The Aiven self-grant was switched off inside the transaction: the creator holds ADMIN only
    # on fcb_app/fcb_signer, and the one explicit SET+INHERIT membership is in fcb_migrator.
    assert [l for l in report.splitlines() if l.startswith("membership ")] == EXPECTED_MEMBERSHIP_LINES
    effective = [l for l in report.splitlines() if l.startswith("effective ")]
    assert sorted(effective) == sorted(EXPECTED_EFFECTIVE_LINES), effective
    assert "role fcb_app exists=true login=true superuser=false createrole=false createdb=false" in report
    assert report.splitlines()[-1] == "HARDENED"


def test_owner_and_acl_change_by_design_and_the_structure_fingerprint_does_not(hardened):
    """Apply transfers ownership to fcb_migrator and rewrites ACLs BY DESIGN, so the restore's full
    catalog fingerprint changes; the same fingerprint without owner/ACL fields does not."""
    assert hardened.structure_fingerprint() == hardened.structure_before
    assert hardened.full_fingerprint() != hardened.full_before
    if _server_major(hardened) == 18:  # the restore's pinned PG18 value, as the workflow requires
        assert hardened.full_before == RESTORE_PINNED_FINGERPRINT
        wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        assert hardened.counts == wf["jobs"]["roles"]["env"]["EXPECTED_COUNTS"]
        assert wf["jobs"]["roles"]["env"]["EXPECTED_FINGERPRINT"] == RESTORE_PINNED_FINGERPRINT


def test_exact_membership_inventory_after_create_role(hardened):
    with hardened.connect("admin") as conn:
        rows = conn.execute(
            "SELECT r.rolname AS role, CASE WHEN m.member = (SELECT oid FROM pg_roles WHERE rolname = SESSION_USER) "
            "THEN 'session' ELSE mm.rolname END AS member, m.admin_option, m.inherit_option, m.set_option, "
            "CASE WHEN m.grantor = 10 THEN 'bootstrap' WHEN m.grantor = (SELECT oid FROM pg_roles WHERE rolname = SESSION_USER) "
            "THEN 'session' ELSE g.rolname END AS grantor "
            "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles mm ON mm.oid = m.member "
            "JOIN pg_roles g ON g.oid = m.grantor "
            "WHERE r.rolname IN ('fcb_app','fcb_migrator','fcb_signer') OR mm.rolname IN ('fcb_app','fcb_migrator','fcb_signer') "
            "ORDER BY 1, 2, 3, 4, 5, 6").fetchall()
    assert [tuple(r.values()) for r in rows] == [
        ("fcb_app", "session", True, False, False, "bootstrap"),
        ("fcb_migrator", "session", False, True, True, "session"),
        ("fcb_migrator", "session", True, False, False, "bootstrap"),
        ("fcb_signer", "session", True, False, False, "bootstrap"),
    ]


def test_an_extra_membership_row_is_refused_by_the_model(hardened):
    """Anything beyond the exact inventory (here: the admin given SET on fcb_app) is refused."""
    hardened.ok("-c", "GRANT fcb_app TO CURRENT_USER WITH INHERIT FALSE, SET TRUE")
    state = hardened.roles_state()
    assert state.startswith("NOT_HARDENED:") and "membership_inventory" in state
    rc, _, err = _roles_tx(hardened, counts=hardened.counts)
    assert rc != 0 and "membership_inventory" in err


def test_second_run_is_a_no_op(hardened):
    first = hardened.diagnose()
    full_first = hardened.full_fingerprint()
    with hardened.connect("admin") as conn:
        applied = conn.execute("SELECT pg_catalog.string_agg(m.applied_at::text, ',' ORDER BY m.version) AS a "
                               "FROM public.schema_migrations m").fetchone()["a"]
    rc, _, err = _roles_tx(hardened, counts=hardened.counts)
    assert rc == 0, err
    assert hardened.diagnose() == first
    assert hardened.schema_evidence() == hardened.schema_before
    assert hardened.full_fingerprint() == full_first  # owners/ACLs too: run 2 changes nothing
    assert hardened.structure_fingerprint() == hardened.structure_before
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


def test_without_disabling_the_aiven_self_grant_the_model_refuses_before_commit(fresh, tmp_path):
    """RED control for the SET LOCAL in upsert_bridge_roles.sql: with Aiven's
    createrole_self_grant='inherit,set' left on, the creator would hold INHERIT+SET on fcb_app and
    fcb_signer; the final model check refuses that and nothing is committed."""
    upsert = (AIVEN / "upsert_bridge_roles.sql").read_text()
    assert upsert.count("SET LOCAL createrole_self_grant = '';") == 1
    # A patched copy of the upsert (it has no \\ir include, so its directory does not matter).
    patched = tmp_path / "upsert_bridge_roles.sql"
    patched.write_text(upsert.replace("SET LOCAL createrole_self_grant = '';", "")
                       .replace("IF pg_catalog.current_setting('createrole_self_grant')", "IF false AND pg_catalog.current_setting('createrole_self_grant')"))
    args = ["-v", f"expected_counts={fresh.counts}", "-v", f"expected_migrations={_expected_migrations()}", "-1",
            "-c", _workflow_guard(fresh.dbname)]
    for name in TX_FILES:
        args += ["-f", str(patched if name == "upsert_bridge_roles.sql" else AIVEN / name)]
    rc, _, err = fresh.psql(*args, env_extra=fresh.verifiers())
    assert rc != 0 and "FCB_ROLES_MODEL_MISMATCH" in err and "unexpected_app_or_signer_member" in err
    _nothing_changed(fresh)


def test_roles_created_by_the_admin_outside_the_workflow_are_refused(fresh):
    """A pre-existing fcb_app/fcb_signer made by avnadmin by hand on Aiven carries the automatic
    INHERIT+SET self-grant: an unexpected membership, refused before commit."""
    fresh.ok("-c", "CREATE ROLE fcb_app NOLOGIN", "-c", "CREATE ROLE fcb_signer NOLOGIN")
    rc, _, err = _roles_tx(fresh, counts=fresh.counts)
    assert rc != 0 and "FCB_ROLES_MODEL_MISMATCH" in err and "unexpected_app_or_signer_member" in err
    assert fresh.state() == "BRIDGE_0001_0008"  # ownership untouched
    assert "unexpected_app_or_signer_member" in fresh.roles_state()


def test_the_self_grant_switch_is_session_scoped_and_passes_the_target_guard(fresh):
    """SET LOCAL has source 'session' (not database/user), ends with the transaction, and the
    workflow $GUARD still accepts the session after it."""
    out = fresh.ok("-c", "BEGIN", "-c", "SET LOCAL createrole_self_grant = ''",
                   "-c", "SELECT s.setting || '|' || s.source FROM pg_catalog.pg_settings s WHERE s.name = 'createrole_self_grant'",
                   "-c", _workflow_guard(fresh.dbname), "-c", "COMMIT",
                   "-c", "SELECT pg_catalog.current_setting('createrole_self_grant')")
    assert out.splitlines() == ["|session", "inherit,set"], out


# ------------------------------------------------------------------ escalation (no path between identities)

@pytest.mark.parametrize("role", ["fcb_app", "fcb_migrator"])
def test_no_bridge_identity_can_become_another_or_the_administrator(hardened, role):
    targets = [r for r in ("fcb_app", "fcb_migrator", "fcb_signer") if r != role] + [hardened.admin_role, "postgres"]
    with hardened.connect(role) as conn:
        conn.autocommit = True
        for target in targets:
            for stmt in (SQL("SET ROLE {}").format(Identifier(target)),
                         SQL("SET SESSION AUTHORIZATION {}").format(Identifier(target)),
                         SQL("SET LOCAL ROLE {}").format(Identifier(target))):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.transaction():
                        conn.execute(stmt)
            # No membership can be self-granted, with or without options.
            for stmt in (SQL("GRANT {} TO {}").format(Identifier(target), Identifier(role)),
                         SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(Identifier(target), Identifier(role)),
                         SQL("GRANT {} TO {}").format(Identifier(role), Identifier(target))):
                with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.InvalidGrantOperation)):
                    conn.execute(stmt)
            row = conn.execute("SELECT pg_catalog.pg_has_role(CURRENT_USER, %s, 'MEMBER') AS m, "
                               "pg_catalog.pg_has_role(CURRENT_USER, %s, 'USAGE') AS u, "
                               "pg_catalog.pg_has_role(CURRENT_USER, %s, 'SET') AS s", (target,) * 3).fetchone()
            assert row == {"m": False, "u": False, "s": False}, (role, target)
        # No attribute escalation, no new role, no change of another role.
        for stmt in ("ALTER ROLE CURRENT_USER CREATEROLE", "ALTER ROLE CURRENT_USER SUPERUSER",
                     "ALTER ROLE CURRENT_USER BYPASSRLS", "ALTER ROLE CURRENT_USER CREATEDB",
                     "ALTER ROLE CURRENT_USER REPLICATION", "CREATE ROLE wp009_escalate",
                     "ALTER ROLE fcb_signer LOGIN", "ALTER ROLE fcb_signer PASSWORD 'x'"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
        # Nor through a routine run with another identity's rights: no EXECUTE on any public routine.
        assert conn.execute(
            "SELECT pg_catalog.count(*) AS n FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND pg_catalog.has_function_privilege(CURRENT_USER, p.oid, 'EXECUTE')"
        ).fetchone()["n"] == (0 if role == "fcb_app" else 4)
        assert conn.execute("SELECT pg_catalog.count(*) AS n FROM pg_catalog.pg_proc p "
                            "WHERE p.prosecdef AND p.pronamespace = 'public'::regnamespace").fetchone()["n"] == 0


def test_signer_cannot_be_reached_by_anyone_but_its_admin_grant(hardened):
    with hardened.connect("admin") as conn:
        for member in ("fcb_app", "fcb_migrator"):
            assert not conn.execute("SELECT pg_catalog.pg_has_role(%s, 'fcb_signer', 'MEMBER') AS m",
                                    (member,)).fetchone()["m"]
        # The administrator holds ADMIN only on the signer: it can neither act as it nor use its rights.
        row = conn.execute("SELECT pg_catalog.pg_has_role(SESSION_USER, 'fcb_signer', 'SET') AS s, "
                           "pg_catalog.pg_has_role(SESSION_USER, 'fcb_signer', 'USAGE') AS u").fetchone()
        assert row == {"s": False, "u": False}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SET ROLE fcb_signer")


# ------------------------------------------------------------------ the MCP app itself through fcb_app

MCP_APP_SCRIPT = r'''
import asyncio, base64, hashlib, json, os, secrets, urllib.parse
import bridge.server as srv
from starlette.testclient import TestClient

async def noop(*a, **k):
    return None

async def stop_all():
    return {"failed": []}

# No browser provider and no Chromium download offline; the real reaper (claim_stale on
# browser_session_leases through DATABASE_URL) still runs once at startup.
srv.bridge.prepare_cloak_runtime = noop
srv.bridge.stop_all = stop_all
host = srv.public_host
cb = "https://claude.ai/api/mcp/auth_callback"
out = {}
with TestClient(srv.app, base_url="https://" + host) as c:
    out["healthz"] = c.get("/healthz").json()
    reg = c.post("/oauth/register", json={"redirect_uris": [cb], "token_endpoint_auth_method": "none",
                                          "grant_types": ["authorization_code", "refresh_token"],
                                          "response_types": ["code"], "client_name": "Claude"})
    out["register"] = reg.status_code
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    form = {"client_id": client_id, "redirect_uri": cb, "response_type": "code",
            "scope": "browser:use offline_access", "state": "s1", "code_challenge": challenge,
            "code_challenge_method": "S256", "resource": "https://" + host + "/mcp",
            "password": os.environ["MCP_OAUTH_PASSWORD"]}
    auth = c.post("/oauth/authorize", data=form, follow_redirects=False)
    out["authorize"] = auth.status_code
    code = urllib.parse.parse_qs(urllib.parse.urlsplit(auth.headers["location"]).query)["code"][0]
    tok = c.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                       "redirect_uri": cb, "code_verifier": verifier})
    out["token"] = tok.status_code
    out["code_replay"] = c.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id,
                                                      "code": code, "redirect_uri": cb, "code_verifier": verifier}).status_code
    pair = tok.json()
    ref = c.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                       "refresh_token": pair["refresh_token"]})
    out["refresh"] = ref.status_code
    out["refresh_replay"] = c.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                         "refresh_token": pair["refresh_token"]}).status_code
    access = ref.json()["access_token"]
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Host": host}
    out["no_token"] = c.post("/mcp/", headers=base, json={}).status_code
    out["unknown_token"] = c.post("/mcp/", headers={**base, "Authorization": "Bearer " + "A" * 43}, json={}).status_code
    hdr = {**base, "Authorization": "Bearer " + access}
    init = c.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "wp009", "version": "0"}}})
    out["initialize"] = [init.status_code, "result" in init.json()]
    tools = c.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    out["tools_list"] = [tools.status_code, len(tools.json()["result"]["tools"])]
    out["reaper_error"] = srv.bridge.reaper_error
print("RESULT_JSON=" + json.dumps(out))
'''


@pytest.mark.parametrize("identity", ["fcb_app", "admin"])
def test_mcp_app_oauth_and_tools_through_the_database_identity(hardened, identity):
    """The real Starlette app with DATABASE_URL = fcb_app (target) or the administrator (rollback):
    /healthz READY, DCR + consent + code exchange + refresh over HTTP (all DB-backed), then MCP
    initialize and tools/list with the DB-issued bearer. Nothing is stubbed in OAuth or storage."""
    dsn = hardened.admin_dsn if identity == "admin" else hardened.dsn("fcb_app")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("APIFOOTBALL_", "SXBET_", "BROWSER_USE_"))}
    env.update({"PYTHONPATH": str(ROOT), "DATABASE_URL": dsn, "MCP_OAUTH_PASSWORD": secrets.token_urlsafe(24),
                "MCP_PUBLIC_HOST": "wp009.test.invalid"})
    if identity == "admin":
        env["PGOPTIONS"] = AIVEN_SELF_GRANT
    proc = subprocess.run([sys.executable, "-c", MCP_APP_SCRIPT], env=env, capture_output=True, text=True,
                          timeout=180, cwd=str(ROOT))
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-3000:]
    out = __import__("json").loads(line[len("RESULT_JSON="):])
    assert out["healthz"]["schema_state"] == "READY" and out["healthz"]["schema_missing_tables"] == []
    assert out["healthz"]["database_configured"] is True
    assert (out["register"], out["authorize"], out["token"], out["refresh"]) == (201, 302, 200, 200)
    assert out["code_replay"] == 400 and out["refresh_replay"] == 400
    assert out["no_token"] == 401 and out["unknown_token"] == 401
    assert out["initialize"] == [200, True]
    assert out["tools_list"][0] == 200 and out["tools_list"][1] == out["healthz"]["mcp_tools_total"] > 0
    assert out["reaper_error"] is None
    assert "permission denied" not in proc.stderr.lower() and "oauth_storage_error" not in proc.stderr
    for secret in (*hardened.passwords.values(), hardened.admin_password):
        assert secret not in proc.stdout + proc.stderr
