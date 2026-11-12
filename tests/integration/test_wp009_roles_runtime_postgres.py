"""FCB-WP-009 on real PostgreSQL: the two-phase roles cutover and the runtime as fcb_app.

Production shape (Aiven defaultdb, verified read-only): a restored bridge 0001-0008 owned by a
NON-superuser CREATEROLE identity that owns the database (avnadmin), public owned by
pg_database_owner, createrole_self_grant=inherit,set. This module builds exactly that in a
throwaway database (the self-grant also per session, so any server, e.g. the CI PostgreSQL 16/18
jobs, exercises it) and runs the transactions with the files and order of
.github/workflows/fcb-aiven-defaultdb-roles.yml (its own $GUARD, minus the PG18/TLS predicates):

* phase 1 (mode=apply): fcb_app LOGIN with the runtime allowlist, fcb_migrator NOLOGIN owning
  nothing, fcb_signer NOLOGIN; the bridge stays administrator-owned (status quo, rollback safe);
* phase 2 (mode=enable-migrator): ownership to fcb_migrator, no administrator inheritance/SET,
  fcb_migrator LOGIN - atomically, and only with no other administrator session connected.

It runs every runtime path through a real fcb_app login (functions and the real Starlette app:
OAuth, MCP, leases + reaper, both coordinators, readiness, the database identity of R2), proves
what nobody can do, kills every model check with a planted violation, and runs RED/GREEN attacks
for the privilege-escalation paths of the reviews of PR #182 (planted triggers, forced row-level
policies, SET ROLE, the read-only "boundary", the cutover window, late administrator sessions).

Every role created here has a random password (CI authenticates with passwords) and is dropped.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import threading
import time
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
# Phase 1 transaction (after $GUARD), in the workflow's order.
TX_FILES = ("roles_apply_guard.sql", "upsert_bridge_roles.sql", "grant_bridge_runtime.sql",
            "assert_bridge_counts.sql", "assert_bridge_roles.sql")
# Phase 2 transaction (after $GUARD).
TX2_FILES = ("enable_bridge_migrator.sql", "assert_bridge_roles.sql")
RUNTIME_TABLES = (
    "oauth_authorization_codes", "oauth_access_tokens", "oauth_refresh_tokens", "oauth_claude_dcr_clients",
    "browser_session_leases", "api_football_quota_counters", "api_football_quota_leases",
    "api_football_quota_attempts", "sxbet_rate_attempts", "sxbet_rate_cooldowns",
)
# D09-02: the exact runtime allowlist (table -> privileges) and the code path using each.
ALLOWLIST = {
    "schema_migrations": {"SELECT"},
    "oauth_authorization_codes": {"SELECT", "INSERT", "UPDATE"},
    "oauth_access_tokens": {"SELECT", "INSERT"},
    "oauth_refresh_tokens": {"SELECT", "INSERT", "UPDATE"},
    "oauth_claude_dcr_clients": {"SELECT", "INSERT", "DELETE"},
    "browser_session_leases": {"SELECT", "INSERT", "UPDATE"},
    "api_football_quota_counters": {"SELECT", "INSERT", "UPDATE"},
    "api_football_quota_leases": {"SELECT", "INSERT", "DELETE"},
    "api_football_quota_attempts": {"SELECT", "INSERT", "DELETE"},
    "sxbet_rate_attempts": {"SELECT", "INSERT", "DELETE"},
    "sxbet_rate_cooldowns": {"SELECT", "INSERT", "UPDATE", "DELETE"},
}
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://bridge.example/mcp"
_TLS_PG18 = (" OR pg_catalog.current_setting('server_version_num')::pg_catalog.int4 OPERATOR(pg_catalog.<) 180000"
             " OR pg_catalog.current_setting('server_version_num')::pg_catalog.int4 OPERATOR(pg_catalog.>) 189999"
             " OR NOT COALESCE((SELECT s.ssl FROM pg_catalog.pg_stat_ssl s WHERE s.pid OPERATOR(pg_catalog.=) "
             "pg_catalog.pg_backend_pid()), false)")
# Aiven defaultdb: createrole_self_grant='inherit,set' at server level. The administrator's
# sessions below carry it as a client option, so every server reproduces it.
AIVEN_SELF_GRANT = "-c createrole_self_grant=inherit,set"
ADMIN_OPTIONS = f"-c search_path=public {AIVEN_SELF_GRANT}"
RESTORE_PINNED_FINGERPRINT = "b825ab11029502140b038ce6d3add670"  # fcb-aiven-defaultdb-restore.yml, PG18
# After both phases: only PostgreSQL's automatic ADMIN-only grants to the creator remain.
EXPECTED_MEMBERSHIP_LINES = [
    "membership fcb_app <- session admin=true inherit=false set=false grantor=bootstrap",
    "membership fcb_migrator <- session admin=true inherit=false set=false grantor=bootstrap",
    "membership fcb_signer <- session admin=true inherit=false set=false grantor=bootstrap",
]
EXPECTED_EFFECTIVE_LINES = [
    f"effective {a} -> {b} member={m} inherits_privileges=false can_set_role=false"
    for a, b, m in (
        ("session", "fcb_migrator", "true"), ("session", "fcb_app", "true"), ("session", "fcb_signer", "true"),
        *((x, y, "false") for x in ("fcb_app", "fcb_migrator", "fcb_signer")
          for y in ("session", "fcb_app", "fcb_migrator", "fcb_signer") if x != y),
    )
]

pytestmark = [
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                       reason="requires isolated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(os.environ.get("FCB_REQUIRE_PSQL") != "1", reason="requires real psql in CI"),
]


def _server_major_raw():
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        return conn.info.server_version // 10000


def _dsn_for(dbname, **override):
    params = conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
    params["dbname"] = dbname
    params.update(override)
    return make_conninfo(**params)


def _drop_bridge_roles():
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        for role in ROLES:
            conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))


def _guard_from(step_prefix, dbname):
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["roles"]["steps"] if s.get("name", "").startswith(step_prefix))
    line = next(l.strip() for l in step["run"].splitlines() if l.strip().startswith('GUARD="DO'))
    guard = line[len('GUARD="'):-1].replace("\\$", "$")
    assert guard.count(_TLS_PG18) == 1
    return guard.replace(_TLS_PG18, "").replace("$EXPECTED_DB", dbname)


def _workflow_guard(dbname):
    return _guard_from("Phase 1 -", dbname)


class Env:
    """One throwaway database restored to bridge 0001-0008 by its non-superuser owner."""

    def __init__(self):
        self.dbname = "wp009_" + uuid.uuid4().hex[:12]
        self.admin_role = "wp009_admin_" + uuid.uuid4().hex[:8]
        self.admin_password = secrets.token_urlsafe(24)
        self.passwords = {"fcb_app": secrets.token_urlsafe(32), "fcb_migrator": secrets.token_urlsafe(32)}
        self.admin_dsn = _dsn_for(self.dbname, user=self.admin_role, password=self.admin_password)
        self.extra_roles = []  # roles a test creates; dropped after the database

    def extra_role(self, login=False):
        name = "wp009_x_" + uuid.uuid4().hex[:8]
        self.extra_roles.append(name)
        with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
            conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(name)))
        return name

    def superuser(self, *statements, as_role=None):
        """Run statements in this database as the test superuser (optionally SET ROLE first)."""
        with psycopg.connect(_dsn_for(self.dbname), autocommit=True) as conn:
            if as_role:
                conn.execute(SQL("SET ROLE {}").format(Identifier(as_role)))
            for statement in statements:
                conn.execute(statement)

    def dsn(self, role):
        return _dsn_for(self.dbname, user=role, password=self.passwords[role])

    def uri(self):
        params = conninfo_to_dict(self.admin_dsn)
        return (f"postgresql://{self.admin_role}:{self.admin_password}@/{self.dbname}"
                f"?host={params['host']}&port={params['port']}")

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

    # Catalog-only reads (the administrator never needs to read a bridge row for these).
    def state(self):
        out = self.ok("-f", str(AIVEN / "defaultdb_precheck.sql"))
        if out == "TRACKED":  # only before any bridge role exists (administrator owns everything)
            out = self.ok("-f", str(AIVEN / "defaultdb_precheck_tracked.sql"))
        return out

    def roles_state(self):
        return self.ok("-f", str(AIVEN / "verify_bridge_roles.sql"))

    def diagnose(self):
        return self.ok("-f", str(AIVEN / "diagnose_bridge_roles.sql"))

    def structure_fingerprint(self):
        return self.ok("-f", str(AIVEN / "fingerprint_bridge_structure.sql"))

    def full_fingerprint(self):
        return self.ok("-f", str(AIVEN / "fingerprint_bridge_schema.sql"))

    def schema_evidence_as_admin(self):
        """Only valid while the administrator owns everything and no bridge role exists."""
        return self.ok("-F", "=", "-f", str(AIVEN / "verify_bridge_schema.sql"))

    def catalog_counts(self):
        return self.ok("-f", str(AIVEN / "count_bridge_catalog.sql"))

    def schema_evidence(self):
        """Rows read AS fcb_app (the workflow's `fcb_db_roles.py evidence`)."""
        proc = subprocess.run([sys.executable, "-I", str(HELPER), "evidence"], env=self.helper_env(),
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def helper_env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "FCB_"))}
        env.update({"FCB_DEFAULTDB_URL": self.uri(), "FCB_APP_DB_PASSWORD": self.passwords["fcb_app"],
                    "FCB_MIGRATOR_DB_PASSWORD": self.passwords["fcb_migrator"], "PGOPTIONS": AIVEN_SELF_GRANT})
        env.pop("PYTHONPATH", None)
        env.update(extra)
        return env

    def verifiers(self):
        env = {**os.environ, "FCB_APP_DB_PASSWORD": self.passwords["fcb_app"],
               "FCB_MIGRATOR_DB_PASSWORD": self.passwords["fcb_migrator"]}
        out = {}
        for var, target in (("FCB_APP_DB_PASSWORD", "FCB_APP_DB_VERIFIER"),
                            ("FCB_MIGRATOR_DB_PASSWORD", "FCB_MIGRATOR_DB_VERIFIER")):
            proc = subprocess.run([sys.executable, "-I", "-S", str(HELPER), "verifier", var],
                                  env=env, capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, proc.stderr
            out[target] = proc.stdout.strip()
        return out


def _expected_rows():
    return "\n".join(["schema_migrations_rows=8", *_expected_migrations().split(" ")])


def _expected_migrations():
    import hashlib
    rows = []
    for f in sorted((ROOT / "sql" / "migrations" / "bridge").glob("000[1-8]_*.sql")):
        rows.append(f"{int(f.name[:4])}={f.name}={hashlib.sha256(f.read_bytes()).hexdigest()}")
    return " ".join(rows)


def _roles_tx(env: Env, *, counts=None, verifiers=None, migrations=None, extra_before=(), guard=True,
              pgoptions=ADMIN_OPTIONS):
    """Phase 1, exactly as the workflow's `run`."""
    args = ["-v", f"expected_counts={counts or env.counts}",
            "-v", f"expected_migrations={migrations or _expected_migrations()}",
            "-v", "expected_roles_state=HARDENED_PHASE1", "-1"]
    if guard:
        args += ["-c", _workflow_guard(env.dbname)]
    for name in TX_FILES:
        if name == "upsert_bridge_roles.sql":
            for c in extra_before:
                args += ["-c", c]
        args += ["-f", str(AIVEN / name)]
    if verifiers is None:
        verifiers = {"FCB_APP_DB_VERIFIER": env.verifiers()["FCB_APP_DB_VERIFIER"]}
    return env.psql(*args, env_extra=verifiers, pgoptions=pgoptions)


def _enable_tx(env: Env, *, verifiers=None, observe=0, guard=True, pgoptions=ADMIN_OPTIONS):
    """Phase 2, exactly as the workflow's enable step."""
    args = ["-v", f"observe_seconds={observe}", "-v", "expected_roles_state=HARDENED", "-1"]
    if guard:
        args += ["-c", _guard_from("Phase 2 -", env.dbname)]
    for name in TX2_FILES:
        args += ["-f", str(AIVEN / name)]
    if verifiers is None:
        verifiers = {"FCB_MIGRATOR_DB_VERIFIER": env.verifiers()["FCB_MIGRATOR_DB_VERIFIER"]}
    return env.psql(*args, env_extra=verifiers, pgoptions=pgoptions)


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
    env.counts = " ".join(env.schema_evidence_as_admin().splitlines()[:7])
    env.schema_before = env.schema_evidence_as_admin()
    env.structure_before = env.structure_fingerprint()
    env.full_before = env.full_fingerprint()
    env.catalog_before = env.catalog_counts()
    assert env.catalog_before == " ".join(c for c in env.counts.split()
                                          if not c.startswith(("constraints_information_schema=", "schema_migrations_rows=")))
    return env


def _drop_database(conn, dbname: str, attempts: int = 6):
    """DROP DATABASE ... WITH (FORCE), retried on ObjectInUse.

    FORCE signals the other backends and waits only 5 s. An autovacuum worker busy in the
    database can outlive that window (seen in CI on PG16: the drop failed with "1 other
    session", the worker exited 1.4 s later, and every later test inherited the leftover
    database and its fcb_app dependencies). Retrying is safe: each attempt is a fresh FORCE.
    """
    for attempt in range(attempts):
        try:
            conn.execute(SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(Identifier(dbname)))
            return
        except psycopg.errors.ObjectInUse:
            if attempt == attempts - 1:
                raise
            time.sleep(1 + attempt)


def _destroy_env(env: Env):
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        _drop_database(conn, env.dbname)
        for role in (*ROLES, *getattr(env, "extra_roles", ())):
            conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))
        conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(env.admin_role)))


@pytest.fixture
def fresh():
    """A restored 0001-0008 database with no bridge role (status quo before WP-009)."""
    env = _create_env()
    try:
        yield env
    finally:
        _destroy_env(env)


@pytest.fixture
def phase1(fresh):
    """After phase 1: fcb_app works, fcb_migrator NOLOGIN owns nothing, administrator owns all."""
    rc, _, err = _roles_tx(fresh)
    assert rc == 0, err
    return fresh


@pytest.fixture
def hardened(phase1):
    """After phase 2 (the final production state)."""
    rc, _, err = _enable_tx(phase1)
    assert rc == 0, err
    return phase1


def _role_exists(role):
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None


def _role_flags(*roles):
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT rolname, rolcreaterole, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)", (list(roles),)).fetchall()}


# ------------------------------------------------------------------ phase 1 and phase 2 themselves

def test_phase1_creates_the_runtime_role_and_moves_nothing(phase1):
    assert phase1.roles_state() == "HARDENED_PHASE1"
    report = phase1.diagnose()
    assert "owners relations session=34" in report and "owners routines session=4" in report
    assert "role fcb_app exists=true login=true superuser=false createrole=false createdb=false" in report
    assert "role fcb_migrator exists=true login=false" in report and "role fcb_signer exists=true login=false" in report
    assert [l for l in report.splitlines() if l.startswith("membership ")] == EXPECTED_MEMBERSHIP_LINES
    assert sorted(l for l in report.splitlines() if l.startswith("effective ")) == sorted(EXPECTED_EFFECTIVE_LINES)
    assert "schema_public_acl fcb_app:USAGE pg_database_owner:CREATE pg_database_owner:USAGE" in report
    assert "default_acl none" in report and not [l for l in report.splitlines() if l.startswith("reach ")]
    assert phase1.structure_fingerprint() == phase1.structure_before
    assert phase1.schema_evidence() == _expected_rows()  # rows read AS fcb_app
    assert phase1.catalog_counts() == phase1.catalog_before
    # The restore precheck refuses (runtime grants) BEFORE its tracked part would read a table.
    assert phase1.state() == "REFUSE_RUNTIME_TABLE_DRIFT"


def test_phase1_rollback_is_the_status_quo_administrator_dsn(phase1):
    """Before phase 2 the administrator still owns the bridge and nobody can act as fcb_migrator,
    so reverting Render to the administrator DSN is safe and works."""
    from bridge import schema_readiness
    assert schema_readiness.evaluate(lambda: phase1.connect("admin"))["state"] == "READY"
    with phase1.connect("admin") as conn:
        conn.execute("INSERT INTO oauth_claude_dcr_clients(client_id_hash,redirect_uri,expires_at) "
                     "VALUES ('rollback-probe','https://example.invalid/cb',now()+interval '1 hour')")
        conn.rollback()


def test_phase1_migrator_cannot_be_used_by_anyone(phase1):
    """R1 during the window: NOLOGIN (any password refused), owns nothing, and no non-superuser
    role of the cluster can SET ROLE to it or inherit it; the administrator neither (ADMIN only)."""
    for pw in ("", phase1.passwords["fcb_migrator"], "x" * 40):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(_dsn_for(phase1.dbname, user="fcb_migrator", password=pw)).close()
    with phase1.connect("admin") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SET ROLE fcb_migrator")
        conn.rollback()
        reach = conn.execute(
            "SELECT r.rolname FROM pg_catalog.pg_roles r WHERE NOT r.rolsuper AND r.rolname <> 'fcb_migrator' AND "
            "(pg_catalog.pg_has_role(r.oid, 'fcb_migrator', 'SET') OR pg_catalog.pg_has_role(r.oid, 'fcb_migrator', 'USAGE'))"
        ).fetchall()
        assert reach == []
        owned = conn.execute("SELECT count(*) AS n FROM pg_catalog.pg_shdepend d WHERE d.refobjid = "
                             "'fcb_migrator'::regrole AND d.deptype = 'o'").fetchone()["n"]
        assert owned == 0
    with phase1.connect("fcb_app") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SET ROLE fcb_migrator")


def test_phase1_rerun_is_a_no_op(phase1):
    first = phase1.diagnose()
    rc, _, err = _roles_tx(phase1)
    assert rc == 0, err
    assert phase1.diagnose() == first and phase1.roles_state() == "HARDENED_PHASE1"
    with phase1.connect("fcb_app") as conn:  # the re-set password still works
        assert conn.execute("SELECT CURRENT_USER AS u").fetchone()["u"] == "fcb_app"


def test_phase2_hands_the_bridge_over_and_leaves_the_administrator_without_privileges(hardened):
    assert hardened.roles_state() == "HARDENED"
    report = hardened.diagnose()
    assert "owners relations fcb_migrator=34" in report and "owners routines fcb_migrator=4" in report
    assert "role fcb_migrator exists=true login=true superuser=false createrole=false createdb=false" in report
    # D09-01: the administrator keeps only the automatic ADMIN-only grant: no INHERIT, no SET.
    assert [l for l in report.splitlines() if l.startswith("membership ")] == EXPECTED_MEMBERSHIP_LINES
    assert sorted(l for l in report.splitlines() if l.startswith("effective ")) == sorted(EXPECTED_EFFECTIVE_LINES)
    assert ("schema_public_acl fcb_app:USAGE fcb_migrator:CREATE fcb_migrator:USAGE "
            "pg_database_owner:CREATE pg_database_owner:USAGE") in report
    assert "default_acl fcb_migrator:f:all_schemas:grantees=0" in report and "routine_non_owner_grants=0" in report
    assert report.splitlines()[-1] == "HARDENED"
    with hardened.connect("admin") as conn:
        row = conn.execute("SELECT pg_catalog.pg_has_role(SESSION_USER, 'fcb_migrator', 'USAGE') AS u, "
                           "pg_catalog.pg_has_role(SESSION_USER, 'fcb_migrator', 'SET') AS s, "
                           "pg_catalog.pg_has_role(SESSION_USER, 'fcb_migrator', 'MEMBER') AS m").fetchone()
        assert row == {"u": False, "s": False, "m": True}
    assert hardened.state() == "REFUSE_DEFAULT_PRIVILEGES"  # restore precheck: catalog-only refusal


def test_phase2_changes_owner_and_acl_by_design_but_not_the_structure(hardened):
    assert hardened.structure_fingerprint() == hardened.structure_before
    assert hardened.full_fingerprint() != hardened.full_before
    assert hardened.schema_evidence() == _expected_rows()  # rows read AS fcb_app
    assert hardened.catalog_counts() == hardened.catalog_before
    if _server_major_raw() == 18:  # the restore's pinned PG18 values, as the workflow requires
        assert hardened.full_before == RESTORE_PINNED_FINGERPRINT
        wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        assert hardened.counts == wf["jobs"]["roles"]["env"]["EXPECTED_COUNTS"]
        assert wf["jobs"]["roles"]["env"]["EXPECTED_FINGERPRINT"] == RESTORE_PINNED_FINGERPRINT


def test_phase2_rerun_only_resets_the_migrator_password(hardened):
    first, full = hardened.diagnose(), hardened.full_fingerprint()
    rc, _, err = _enable_tx(hardened)
    assert rc == 0, err
    assert hardened.diagnose() == first and hardened.full_fingerprint() == full
    with hardened.connect("fcb_migrator") as conn:
        assert conn.execute("SELECT CURRENT_USER AS u").fetchone()["u"] == "fcb_migrator"


def test_phase1_is_refused_after_phase2(hardened):
    rc, _, err = _roles_tx(hardened)
    assert rc != 0 and "FCB_ROLES_OWNER_GUARD_FAILED" in err


def test_phase2_is_refused_before_phase1(fresh):
    rc, _, err = _enable_tx(fresh)
    assert rc != 0 and "FCB_ROLES_PHASE2_STATE_REFUSED ROLES_ABSENT" in err


def test_accidental_administrator_dsn_after_phase2_fails_closed(hardened):
    """D09-01: every administrator access to a bridge table is refused by the privilege check,
    before any row is read or any policy, trigger, rule or default could run."""
    from bridge import schema_readiness
    assert schema_readiness.evaluate(lambda: hardened.connect("admin"))["state"] != "READY"
    for stmt in ("SELECT 1 FROM public.schema_migrations", "SELECT count(*) FROM public.oauth_access_tokens",
                 "INSERT INTO public.oauth_claude_dcr_clients(client_id_hash,redirect_uri,expires_at) VALUES ('a','b',now())",
                 "UPDATE public.browser_session_leases SET owner_id = 'x'", "DELETE FROM public.sxbet_rate_attempts",
                 "TRUNCATE public.model_snapshots", "SELECT public.canonical_reject_mutation()",
                 "ALTER TABLE public.runtime_state ADD COLUMN x integer", "SET ROLE fcb_migrator",
                 "LOCK TABLE public.schema_migrations IN ACCESS SHARE MODE"):
        with hardened.connect("admin") as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)


def test_both_identities_pass_the_workflow_login_check(phase1, tmp_path):
    def run(phase, require_tls=False):
        args = [sys.executable, "-I", str(HELPER), "login-check", "--phase", str(phase)]
        if require_tls:
            args.append("--require-tls")
        return subprocess.run(args, env=phase1.helper_env(), capture_output=True, text=True, timeout=120)
    proc = run(1)
    assert proc.returncode == 0, proc.stderr
    assert "login fcb_app: tls=False schema_migrations_rows=8 readiness=READY create_denied=True " \
           "legacy_read_denied=True" in proc.stdout
    assert "login fcb_migrator: refused (phase 1: NOLOGIN, no password)" in proc.stdout
    assert run(2).returncode == 1  # phase 2 checks fail in phase 1 (migrator cannot log in)
    rc, _, err = _enable_tx(phase1)
    assert rc == 0, err
    proc = run(2)
    assert proc.returncode == 0, proc.stderr
    assert "login fcb_migrator: tls=False schema_migrations_rows=8 can_create=True" in proc.stdout
    assert "admin: bridge access denied=True inherits_migrator=False can_set_migrator=False" in proc.stdout
    assert run(1).returncode == 1  # the phase-1 check now sees a migrator that can log in
    for out in (proc.stdout + proc.stderr,):
        for secret in (*phase1.passwords.values(), phase1.admin_password):
            assert secret not in out
    proc = run(2, require_tls=True)
    assert proc.returncode == 1 and "FCB_LOGIN_NOT_TLS" in proc.stderr
    with phase1.connect("fcb_migrator") as conn:  # the probes were rolled back
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
    with hardened.connect("fcb_migrator") as conn:  # an expired registration for the cleanup DELETE
        conn.execute("INSERT INTO oauth_claude_dcr_clients(client_id_hash,redirect_uri,expires_at) "
                     "VALUES ('expired-probe','https://example.invalid/cb',now()-interval '1 second')")
    client_id = oauth_store.register_claude_dcr_client(CLAUDE_REDIRECT)
    assert oauth_store.valid_claude_dcr_client(client_id, CLAUDE_REDIRECT)
    assert not oauth_store.valid_claude_dcr_client(client_id, "https://evil.example/cb")
    with hardened.connect("fcb_migrator") as conn:
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
    hardened.superuser(signer_sql, as_role="fcb_migrator")
    hardened.superuser("CREATE TABLE public.wp009_admin_made(id integer)", as_role=hardened.admin_role)
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
    with hardened.connect("fcb_migrator") as conn:
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


# ------------------------------------------------------------------ the MCP app itself

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
    health = c.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                "params": {"name": "apifootball_health", "arguments": {}}})
    hres = health.json().get("result", {})
    hdata = hres.get("structuredContent") or json.loads(hres["content"][0]["text"])
    out["database_identity"] = hdata.get("database_identity")
    out["reaper_error"] = srv.bridge.reaper_error
print("RESULT_JSON=" + json.dumps(out))
'''




@pytest.mark.parametrize("phase,identity", [(1, "fcb_app"), (1, "admin"), (2, "fcb_app"), (2, "fcb_migrator")])
def test_mcp_app_oauth_and_tools_through_the_database_identity(phase1, phase, identity):
    """The real Starlette app with DATABASE_URL = fcb_app (target, both phases), the administrator
    (rollback DSN of phase 1) or fcb_migrator (rollback DSN after phase 2): /healthz READY, DCR +
    consent + code exchange + refresh over HTTP (all DB-backed), MCP initialize, tools/list and the
    allowlisted apifootball_health reporting the database identity (R2). Nothing is stubbed in
    OAuth or storage."""
    if phase == 2:
        rc, _, err = _enable_tx(phase1)
        assert rc == 0, err
    out, proc = _run_mcp_app(phase1, identity)
    assert out["healthz"]["schema_state"] == "READY" and out["healthz"]["schema_missing_tables"] == []
    assert out["healthz"]["database_configured"] is True
    assert (out["register"], out["authorize"], out["token"], out["refresh"]) == (201, 302, 200, 200)
    assert out["code_replay"] == 400 and out["refresh_replay"] == 400
    assert out["no_token"] == 401 and out["unknown_token"] == 401
    assert out["initialize"] == [200, True]
    assert out["tools_list"][0] == 200 and out["tools_list"][1] == out["healthz"]["mcp_tools_total"] > 0
    role = phase1.admin_role if identity == "admin" else identity
    assert out["database_identity"] == {"status": "OK", "current_user": role, "session_user": role}
    assert out["reaper_error"] is None
    assert "permission denied" not in proc.stderr.lower() and "oauth_storage_error" not in proc.stderr
    for secret in (*phase1.passwords.values(), phase1.admin_password):
        assert secret not in proc.stdout + proc.stderr


def _run_mcp_app(env_obj, identity, script=None):
    dsn = env_obj.admin_dsn if identity == "admin" else env_obj.dsn(identity)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("APIFOOTBALL_", "SXBET_", "BROWSER_USE_", "PG"))}
    env.update({"PYTHONPATH": str(ROOT), "DATABASE_URL": dsn, "MCP_OAUTH_PASSWORD": secrets.token_urlsafe(24),
                "MCP_PUBLIC_HOST": "wp009.test.invalid", "APIFOOTBALL_ENABLED": "true",
                "APIFOOTBALL_LIVE_AUTHORIZED": "false"})
    if identity == "admin":
        env["PGOPTIONS"] = AIVEN_SELF_GRANT
    proc = subprocess.run([sys.executable, "-c", script or MCP_APP_SCRIPT], env=env, capture_output=True, text=True,
                          timeout=180, cwd=str(ROOT))
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-3000:]
    return json.loads(line[len("RESULT_JSON="):]), proc


HEALTH_ONLY_SCRIPT = r'''
import json
import bridge.server as srv
from starlette.testclient import TestClient

async def noop(*a, **k):
    return None

async def stop_all():
    return {"failed": []}

srv.bridge.prepare_cloak_runtime = noop
srv.bridge.stop_all = stop_all
host = srv.public_host
with TestClient(srv.app, base_url="https://" + host) as c:
    out = {"healthz": c.get("/healthz").json()}
    out["register"] = c.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                                      "token_endpoint_auth_method": "none"}).status_code
    out["bearer"] = c.post("/mcp/", headers={"Accept": "application/json, text/event-stream",
                                             "Content-Type": "application/json", "Host": host,
                                             "Authorization": "Bearer " + "A" * 43}, json={}).status_code
print("RESULT_JSON=" + json.dumps(out))
'''


def test_mcp_app_on_the_administrator_dsn_after_phase2_fails_closed(hardened):
    """D09-01: Render accidentally left on the administrator after phase 2 serves no bridge data:
    readiness is not READY, OAuth storage answers 503, bearer checks fail closed."""
    out, proc = _run_mcp_app(hardened, "admin", HEALTH_ONLY_SCRIPT)
    assert out["healthz"]["schema_state"] != "READY"
    assert out["register"] == 503 and out["bearer"] == 503
    assert hardened.admin_password not in proc.stdout + proc.stderr


# ------------------------------------------------------------------ D09-02: least privilege for fcb_app

@pytest.mark.parametrize("table", sorted(ALLOWLIST))
def test_fcb_app_has_exactly_the_runtime_matrix(hardened, table):
    """Each table: exactly the privileges a runtime path uses (finalize_bridge_privileges.sql
    documents file:function for each); the full runtime is GREEN with this matrix (runtime tests,
    the real app, reaper, coordinators)."""
    with hardened.connect("fcb_app") as conn:
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            has = conn.execute("SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, %s) AS a",
                               (f"public.{table}", priv)).fetchone()["a"]
            assert has == (priv in ALLOWLIST[table]), (table, priv)


@pytest.mark.parametrize("stmt", [
    "DELETE FROM public.oauth_authorization_codes",
    "UPDATE public.oauth_access_tokens SET revoked_at = now()",
    "DELETE FROM public.oauth_access_tokens",
    "DELETE FROM public.oauth_refresh_tokens",
    "UPDATE public.oauth_claude_dcr_clients SET redirect_uri = 'x'",
    "DELETE FROM public.browser_session_leases",
    "DELETE FROM public.api_football_quota_counters",
    "UPDATE public.api_football_quota_leases SET expires_at = now()",
    "UPDATE public.api_football_quota_attempts SET created_at = now()",
    "UPDATE public.sxbet_rate_attempts SET created_at = now()",
])
def test_privileges_removed_in_d09_02_are_unusable(hardened, stmt):
    """RED before D09-02 (the uniform SELECT/INSERT/UPDATE/DELETE grant allowed these), GREEN now."""
    with hardened.connect("fcb_app") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(stmt)


def test_previous_uniform_grant_would_be_flagged_by_the_model(hardened):
    """RED control: re-granting the pre-D09-02 uniform DML is caught (unexpected_relation_grant)."""
    hardened.superuser("GRANT SELECT, INSERT, UPDATE, DELETE ON public.oauth_access_tokens TO fcb_app",
                       as_role="fcb_migrator")
    assert "unexpected_relation_grant" in hardened.roles_state()


def test_temporary_tables_red_and_a_revoke_keeps_the_runtime_green(hardened):
    """TEMPORARY (database-wide PUBLIC default): RED, fcb_app can create a temp table. A revoke
    from PUBLIC on this database keeps the whole runtime working (GREEN), so it is technically
    safe; it is NOT implemented because it changes a database-wide default that other Aiven
    tooling may rely on: decision for the owner (runbook)."""
    with hardened.connect("fcb_app") as conn:
        conn.execute("CREATE TEMP TABLE wp009_t(i integer)")
        conn.rollback()
    hardened.superuser(SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(Identifier(hardened.dbname)))
    with hardened.connect("fcb_app") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TEMP TABLE wp009_t(i integer)")
    out, _ = _run_mcp_app(hardened, "fcb_app")
    assert out["healthz"]["schema_state"] == "READY" and out["tools_list"][0] == 200
    assert (out["register"], out["authorize"], out["token"], out["refresh"]) == (201, 302, 200, 200)


def test_large_objects_cannot_be_restricted_by_a_non_superuser(hardened):
    """lo_create: RED, any login can create large objects (no privilege gates creation); the
    administrator cannot revoke EXECUTE on pg_catalog functions (not their owner). Residual."""
    with hardened.connect("fcb_app") as conn:
        assert conn.execute("SELECT pg_catalog.lo_create(0) > 0 AS ok").fetchone()["ok"]
        conn.rollback()
    with hardened.connect("admin") as conn:  # not the owner: PostgreSQL only warns, nothing is revoked
        conn.execute("REVOKE EXECUTE ON FUNCTION pg_catalog.lo_create(oid) FROM PUBLIC")
        conn.commit()
        assert conn.execute("SELECT pg_catalog.has_function_privilege('fcb_app', 'pg_catalog.lo_create(oid)', "
                            "'EXECUTE') AS a").fetchone()["a"]


def test_connect_to_other_databases_via_public_is_a_residual(hardened):
    """CONNECT is PUBLIC by default on every database; WP-009 changes defaultdb only (the only
    database the administrator should act on). fcb_app can open a session elsewhere, with no
    privilege there."""
    other = _dsn_for("postgres", user="fcb_app", password=hardened.passwords["fcb_app"])
    with psycopg.connect(other) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == "fcb_app"


@pytest.mark.parametrize("stmt", [
    "SELECT pg_catalog.pg_read_file('/etc/hostname')",
    "SELECT pg_catalog.pg_ls_dir('.')",
    "SELECT pg_catalog.pg_read_binary_file('/etc/hostname')",
    "SELECT pg_catalog.lo_import('/etc/hostname')",
    "COPY (SELECT 1) TO PROGRAM 'true'",
])
def test_privileged_pg_catalog_functions_are_denied(hardened, stmt):
    with hardened.connect("fcb_app") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(stmt)


def test_fcb_app_cannot_signal_other_backends(hardened):
    with hardened.connect("admin") as admin:
        pid = admin.execute("SELECT pg_catalog.pg_backend_pid() AS p").fetchone()["p"]
        with hardened.connect("fcb_app") as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT pg_catalog.pg_terminate_backend(%s)", (pid,))


@pytest.mark.parametrize("phase", [1, 2])
def test_advisory_lock_held_by_fcb_app_fails_both_phases_fast_and_is_reported(phase1, phase):
    """No GRANT can forbid an advisory lock id: fcb_app CAN hold 460244120 (RED, denial of service
    of the administrative workflows). Mitigation: both transactions try the lock without waiting
    and refuse with FCB_ROLES_LOCK_BUSY (no hang), and diagnose names the holder."""
    if phase == 2:
        rc, _, err = _enable_tx(phase1)
        assert rc == 0, err
    with phase1.connect("fcb_app") as holder:
        holder.execute("SELECT pg_catalog.pg_advisory_lock(460244120::bigint)")
        assert "bridge_lock_holders fcb_app" in phase1.diagnose()
        t0 = time.monotonic()
        rc, _, err = (_roles_tx(phase1) if phase == 1 else _enable_tx(phase1))
        assert rc != 0 and "FCB_ROLES_LOCK_BUSY" in err and time.monotonic() - t0 < 30
        holder.execute("SELECT pg_catalog.pg_advisory_unlock(460244120::bigint)")
    assert phase1.roles_state() == ("HARDENED_PHASE1" if phase == 1 else "HARDENED")


# ------------------------------------------------------------------ refusals before any write

def _nothing_changed(env: Env):
    assert env.roles_state() == "ROLES_ABSENT"
    assert env.state() == "BRIDGE_0001_0008"


def test_missing_verifiers_abort_before_commit(fresh):
    rc, _, err = _roles_tx(fresh, verifiers={})
    assert rc != 0
    _nothing_changed(fresh)


def test_malformed_verifiers_abort_before_commit(fresh):
    rc, _, err = _roles_tx(fresh, verifiers={"FCB_APP_DB_VERIFIER": "plaintext-password-not-a-verifier-000000"})
    assert rc != 0 and "FCB_ROLE_VERIFIER_INVALID" in err
    _nothing_changed(fresh)
    rc, _, err = _roles_tx(fresh)
    assert rc == 0, err
    for bad in ({}, {"FCB_MIGRATOR_DB_VERIFIER": "plaintext-password-not-a-verifier-000000"}):
        rc, _, err = _enable_tx(fresh, verifiers=bad)
        assert rc != 0
        assert fresh.roles_state() == "HARDENED_PHASE1"


def test_wrong_counts_or_migrations_abort_before_commit(fresh):
    rc, _, err = _roles_tx(fresh, counts=fresh.counts.replace("tables=33", "tables=34"))
    assert rc != 0 and "FCB_" in err
    _nothing_changed(fresh)
    expected = _expected_migrations()
    for wrong in (expected.rsplit(" ", 1)[0],
                  expected[:-1] + ("0" if expected[-1] != "0" else "1"),
                  expected.replace("1=0001_", "9=0001_", 1)):
        rc, _, err = _roles_tx(fresh, migrations=wrong)
        assert rc != 0 and "FCB_ROLES_MIGRATIONS_GUARD_FAILED" in err
        _nothing_changed(fresh)


def test_object_of_another_owner_aborts_phase1(fresh):
    other = fresh.extra_role()
    fresh.superuser(SQL("ALTER TABLE public.runtime_state OWNER TO {}").format(Identifier(other)))
    rc, _, err = _roles_tx(fresh)
    assert rc != 0 and "FCB_ROLES_OWNER_GUARD_FAILED" in err
    assert fresh.roles_state() == "ROLES_ABSENT"


def test_preexisting_role_with_extra_privilege_or_membership_is_refused(fresh):
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("CREATE ROLE fcb_app NOLOGIN CREATEDB")
        conn.execute(SQL("GRANT fcb_app TO {} WITH ADMIN TRUE").format(Identifier(fresh.admin_role)))
    rc, _, err = _roles_tx(fresh)
    assert rc != 0 and "FCB_ROLES_RERUN_NOT_HARDENED" in err and "role_attributes" in err
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("ALTER ROLE fcb_app NOCREATEDB")
        conn.execute("CREATE ROLE fcb_migrator NOLOGIN")
        conn.execute(SQL("GRANT fcb_migrator TO {} WITH ADMIN TRUE").format(Identifier(fresh.admin_role)))
        conn.execute("GRANT fcb_migrator TO fcb_app")
    rc, _, err = _roles_tx(fresh)
    assert rc != 0 and "FCB_ROLES_RERUN_NOT_HARDENED" in err
    assert "role_member_of_other_role" in err and "unexpected_migrator_member" in err
    assert fresh.state() == "BRIDGE_0001_0008"


def test_roles_created_by_the_admin_outside_the_workflow_are_refused(fresh):
    """Created by avnadmin by hand on Aiven, they carry the automatic INHERIT+SET self-grant."""
    fresh.ok("-c", "CREATE ROLE fcb_app NOLOGIN", "-c", "CREATE ROLE fcb_signer NOLOGIN")
    rc, _, err = _roles_tx(fresh)
    assert rc != 0 and "FCB_ROLES_RERUN_NOT_HARDENED" in err and "unexpected_app_or_signer_member" in err
    assert fresh.state() == "BRIDGE_0001_0008"


def test_without_disabling_the_aiven_self_grant_the_model_refuses_before_commit(fresh, tmp_path):
    """RED control for the SET LOCAL in upsert_bridge_roles.sql (createrole_self_grant=inherit,set)."""
    upsert = (AIVEN / "upsert_bridge_roles.sql").read_text()
    assert upsert.count("SET LOCAL createrole_self_grant = '';") == 1
    patched = tmp_path / "upsert_bridge_roles.sql"  # no \ir include, so its directory does not matter
    patched.write_text(upsert.replace("SET LOCAL createrole_self_grant = '';", "")
                       .replace("IF pg_catalog.current_setting('createrole_self_grant')",
                                "IF false AND pg_catalog.current_setting('createrole_self_grant')"))
    args = ["-v", f"expected_counts={fresh.counts}", "-v", f"expected_migrations={_expected_migrations()}",
            "-v", "expected_roles_state=HARDENED_PHASE1", "-1", "-c", _workflow_guard(fresh.dbname)]
    for name in TX_FILES:
        args += ["-f", str(patched if name == "upsert_bridge_roles.sql" else AIVEN / name)]
    rc, _, err = fresh.psql(*args, env_extra={"FCB_APP_DB_VERIFIER": fresh.verifiers()["FCB_APP_DB_VERIFIER"]})
    assert rc != 0 and "FCB_ROLES_MODEL_MISMATCH" in err
    for code in ("unexpected_app_or_signer_member", "membership_inventory", "session_reaches_migrator", "role_reachability"):
        assert code in err, code
    _nothing_changed(fresh)


def test_the_self_grant_switch_is_session_scoped_and_passes_the_target_guard(fresh):
    out = fresh.ok("-c", "BEGIN", "-c", "SET LOCAL createrole_self_grant = ''",
                   "-c", "SELECT s.setting || '|' || s.source FROM pg_catalog.pg_settings s WHERE s.name = 'createrole_self_grant'",
                   "-c", _workflow_guard(fresh.dbname), "-c", "COMMIT",
                   "-c", "SELECT pg_catalog.current_setting('createrole_self_grant')")
    assert out.splitlines() == ["|session", "inherit,set"], out


def test_workflow_guards_refuse_a_wrong_database(fresh):
    for prefix in ("Phase 1 -", "Phase 2 -"):
        rc, _, err = fresh.psql("-c", _guard_from(prefix, "defaultdb_not_this_one"))
        assert rc != 0 and "FCB_TARGET_GUARD_FAILED" in err


def test_signer_series_anywhere_aborts_phase1(fresh):
    fresh.superuser("CREATE SCHEMA wp009_signer", "CREATE TABLE wp009_signer.signer_ledger_anchors(id integer)")
    rc, _, err = _roles_tx(fresh, guard=False)  # $GUARD would refuse the schema first
    assert rc != 0 and "FCB_ROLES_SIGNER_SERIES_PRESENT" in err
    assert fresh.roles_state() == "ROLES_ABSENT"


@pytest.mark.parametrize("name", ["upsert_bridge_roles.sql", "assert_bridge_roles.sql", "roles_apply_guard.sql",
                                  "enable_bridge_migrator.sql"])
def test_every_in_transaction_file_refuses_a_set_role_or_moved_search_path(fresh, name):
    helper = fresh.extra_role()
    fresh.superuser(SQL("GRANT {} TO {} WITH SET TRUE").format(Identifier(helper), Identifier(fresh.admin_role)))
    base = ["-v", f"expected_counts={fresh.counts}", "-v", f"expected_migrations={_expected_migrations()}",
            "-v", "expected_roles_state=HARDENED", "-v", "observe_seconds=0", "-1"]
    for prefix, opts in ((["-c", SQL("SET ROLE {}").format(Identifier(helper)).as_string(None)], ADMIN_OPTIONS),
                         ([], ADMIN_OPTIONS.replace("search_path=public", "search_path=public,pg_catalog"))):
        rc, _, err = fresh.psql(*base, *prefix, "-f", str(AIVEN / name), env_extra=fresh.verifiers(), pgoptions=opts)
        assert rc != 0 and "FCB_ROLES_SESSION_GUARD_FAILED" in err, (name, err)
    assert fresh.roles_state() == "ROLES_ABSENT"


# ------------------------------------------------------------------ D09-03 / R3: the cutover window

def test_phase2_refuses_while_another_administrator_session_is_connected(phase1):
    with phase1.connect("admin") as other:  # e.g. Render still on the administrator DSN
        other.execute("SELECT 1")
        rc, _, err = _enable_tx(phase1)
        assert rc != 0 and "FCB_ROLES_ADMIN_SESSION_PRESENT" in err
    assert phase1.roles_state() == "HARDENED_PHASE1"


def test_phase2_refuses_an_administrator_session_that_appears_during_the_observation(phase1):
    def late_session():
        time.sleep(1.5)
        with phase1.connect("admin") as other:
            other.execute("SELECT 1")
            time.sleep(3)
    t = threading.Thread(target=late_session)
    t.start()
    try:
        rc, _, err = _enable_tx(phase1, observe=4)
    finally:
        t.join()
    assert rc != 0 and "FCB_ROLES_ADMIN_SESSION_PRESENT" in err
    assert phase1.roles_state() == "HARDENED_PHASE1"


def test_phase2_ignores_runtime_sessions_and_succeeds(phase1):
    with phase1.connect("fcb_app") as runtime:
        runtime.execute("SELECT 1")
        rc, _, err = _enable_tx(phase1, observe=2)
        assert rc == 0, err
    assert phase1.roles_state() == "HARDENED"


def test_phase2_refuses_a_drifted_phase1_model(phase1):
    other = phase1.extra_role()
    phase1.superuser(SQL("GRANT SELECT ON public.model_snapshots TO {}").format(Identifier(other)))
    rc, _, err = _enable_tx(phase1)
    assert rc != 0 and "FCB_ROLES_PHASE2_STATE_REFUSED" in err and "unexpected_relation_grant" in err
    assert phase1.roles_state().startswith("NOT_HARDENED:")


def test_a_late_administrator_session_after_phase2_cannot_reach_migrator_code(hardened):
    """R3: a session of the administrator opened during the cutover and still alive after commit
    is refused by the privilege check on its next statement; migrator code never runs as it."""
    with hardened.connect("admin") as late:
        late.execute("SELECT 1")
        role = _plant_trap(hardened)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            late.execute(_DCR_INSERT, ("late",))
    assert not _role_exists(role) and _trap_log(hardened) == []


# ------------------------------------------------------------------ privilege escalation through owner code
# Threat (reviews of PR #182): the bridge OWNER can attach code (triggers, forced row-level
# policies, defaults, CHECKs, rules, views, ...) that runs with the rights of WHOEVER touches the
# tables. The design removes every path where an identity above the owner touches them.

TRAP_SQL = """
CREATE TABLE public.wp009_trap_log(cu text, su text, created boolean);
GRANT INSERT ON public.wp009_trap_log TO PUBLIC;
CREATE FUNCTION public.wp009_trap() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ok boolean := false;
BEGIN
    BEGIN
        RESET ROLE;  -- SET ROLE is no boundary: invoker code can return to the session user
        EXECUTE 'CREATE ROLE {role}';
        ok := true;
    EXCEPTION WHEN OTHERS THEN ok := false;
    END;
    INSERT INTO public.wp009_trap_log VALUES (current_user, session_user, ok);
    RETURN NULL;
END $$;
CREATE TRIGGER wp009_trap AFTER INSERT ON public.oauth_claude_dcr_clients
    FOR EACH STATEMENT EXECUTE FUNCTION public.wp009_trap();
"""
_DCR_INSERT = ("INSERT INTO oauth_claude_dcr_clients(client_id_hash,redirect_uri,expires_at) "
               "VALUES (%s,'https://example.invalid/cb',now()+interval '1 hour')")
# The verified PoC of the review of 2add80b (poc_rls.py): a forced policy whose function turns the
# READ-ONLY transaction read-write (set_config of default_transaction_read_only is user-settable)
# and then escalates in the following statement of the same psql invocation.
POC_RLS_SQL = """
CREATE FUNCTION public.fcb_probe() RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_catalog.set_config('default_transaction_read_only', 'off', false);
  IF NOT pg_catalog.current_setting('transaction_read_only')::boolean THEN
    EXECUTE 'ALTER ROLE fcb_migrator CREATEROLE';
    EXECUTE 'ALTER ROLE fcb_signer LOGIN PASSWORD ''pwned-signer''';
  END IF;
  RETURN true;
END $$;
GRANT EXECUTE ON FUNCTION public.fcb_probe() TO PUBLIC;
ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY p ON public.schema_migrations USING (public.fcb_probe());
"""


def _plant_trap(env):
    role = "wp009_esc_" + uuid.uuid4().hex[:6]
    env.extra_roles.append(role)
    env.superuser(TRAP_SQL.replace("{role}", role), as_role="fcb_migrator")
    return role


def _trap_log(env):
    with psycopg.connect(_dsn_for(env.dbname)) as conn:
        return [tuple(r) for r in conn.execute("SELECT cu, su, created FROM public.wp009_trap_log").fetchall()]


def _previous_design(env):
    """The state the reviews attacked (c9f79ee/2add80b): the administrator INHERITs and can SET
    to fcb_migrator, which owns the bridge and can log in."""
    env.superuser(SQL("GRANT fcb_migrator TO {} WITH INHERIT TRUE, SET TRUE").format(Identifier(env.admin_role)))


def test_red_previous_design_planted_trigger_runs_as_the_administrator(hardened):
    _previous_design(hardened)
    role = _plant_trap(hardened)
    with hardened.connect("admin") as conn:
        conn.execute(_DCR_INSERT, ("red",))
        conn.commit()
    assert _trap_log(hardened) == [(hardened.admin_role, hardened.admin_role, True)]
    assert _role_exists(role)
    assert "session_reaches_migrator" in hardened.roles_state()  # and the model reports it


def test_red_previous_design_set_role_is_not_a_boundary(hardened):
    _previous_design(hardened)
    role = _plant_trap(hardened)
    with hardened.connect("admin") as conn:
        conn.execute("SET LOCAL ROLE fcb_migrator")
        conn.execute(_DCR_INSERT, ("set-role",))
        conn.commit()
    assert _trap_log(hardened) == [(hardened.admin_role, hardened.admin_role, True)]
    assert _role_exists(role)


def test_red_previous_design_read_only_session_is_not_a_boundary(hardened):
    """The review's PoC: the workflow's read-only `verify_bridge_schema.sql` read as the
    administrator escalates (CREATEROLE for fcb_migrator, LOGIN for fcb_signer)."""
    _previous_design(hardened)
    hardened.superuser(POC_RLS_SQL, as_role="fcb_migrator")
    rc, _, err = hardened.psql("-F", "=", "-f", str(AIVEN / "verify_bridge_schema.sql"),
                               pgoptions=ADMIN_OPTIONS + " -c default_transaction_read_only=on")
    assert rc == 0, err
    assert _role_flags("fcb_migrator", "fcb_signer") == {"fcb_migrator": (True, True), "fcb_signer": (False, True)}


def test_green_the_same_poc_is_denied_before_any_policy_runs(hardened):
    """GREEN: after phase 2 the administrator holds no privilege on schema_migrations: the read
    fails with permission denied before the policy function is evaluated; nothing changes."""
    hardened.superuser(POC_RLS_SQL, as_role="fcb_migrator")
    rc, _, err = hardened.psql("-F", "=", "-f", str(AIVEN / "verify_bridge_schema.sql"),
                               pgoptions=ADMIN_OPTIONS + " -c default_transaction_read_only=on")
    assert rc != 0 and "permission denied for table schema_migrations" in err
    assert _role_flags("fcb_migrator", "fcb_signer") == {"fcb_migrator": (False, True), "fcb_signer": (False, False)}
    # The workflow reads the evidence AS fcb_app instead: the planted code runs as fcb_app, cannot
    # escalate (fcb_app may not ALTER ROLE) and the evidence step fails closed.
    proc = subprocess.run([sys.executable, "-I", str(HELPER), "evidence"], env=hardened.helper_env(),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0 and "permission denied to alter role" in proc.stderr
    assert _role_flags("fcb_migrator", "fcb_signer") == {"fcb_migrator": (False, True), "fcb_signer": (False, False)}
    assert "relation_hooks" in hardened.roles_state()


@pytest.mark.parametrize("identity", ["fcb_app", "fcb_migrator"])
def test_green_runtime_and_rollback_identities_gain_nothing_from_planted_code(hardened, identity):
    role = _plant_trap(hardened)
    with hardened.connect(identity) as conn:
        conn.execute(_DCR_INSERT, (identity,))
        conn.commit()
    assert _trap_log(hardened) == [(identity, identity, False)]
    assert not _role_exists(role)


@pytest.mark.parametrize("stmt", [
    _DCR_INSERT.replace("%s", "'admin'"),
    "SELECT count(*) FROM public.schema_migrations",
    "UPDATE public.oauth_claude_dcr_clients SET redirect_uri = redirect_uri",
    "DELETE FROM public.oauth_claude_dcr_clients",
])
def test_green_late_administrator_access_fails_closed_on_every_path(hardened, stmt):
    """A trigger trap and a forced policy planted: an administrator session after
    phase 2 is refused by the privilege check first; no planted code runs."""
    role = _plant_trap(hardened)
    read_role = "wp009_esc_" + uuid.uuid4().hex[:6]
    hardened.extra_roles.append(read_role)
    hardened.superuser(POLICY_TRAP.replace("{role}", read_role), as_role="fcb_migrator")
    with hardened.connect("admin") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(stmt)
    assert not _role_exists(role) and not _role_exists(read_role) and _trap_log(hardened) == []


def test_red_unqualified_administrator_calls_can_resolve_to_migrator_functions(hardened):
    """Residual, documented: with search_path containing public, an unqualified call whose
    argument types match a migrator function exactly better than pg_catalog's runs THAT function
    (CVE-2018-1058 class) if the migrator granted EXECUTE to PUBLIC. The workflow never makes an
    unqualified call (static test: catalog-qualified SQL) and the model refuses the extra routine;
    administrators must keep search_path = pg_catalog in manual sessions (runbook)."""
    hardened.superuser("CREATE FUNCTION public.lower(character varying) RETURNS text LANGUAGE sql "
                       "AS 'SELECT ''hijacked''::text'", "GRANT EXECUTE ON FUNCTION public.lower(character varying) TO PUBLIC",
                       as_role="fcb_migrator")
    assert hardened.ok("-c", "SELECT lower('X'::varchar)") == "hijacked"
    assert hardened.ok("-c", "SELECT lower('X'::varchar)", pgoptions=AIVEN_SELF_GRANT + " -c search_path=pg_catalog") == "x"
    assert "routine_set" in hardened.roles_state()


POLICY_TRAP = """
CREATE FUNCTION public.wp009_read_trap() RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        RESET ROLE;
        PERFORM pg_catalog.set_config('default_transaction_read_only', 'off', false);
        EXECUTE 'CREATE ROLE {role}';
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
    RETURN true;
END $$;
GRANT EXECUTE ON FUNCTION public.wp009_read_trap() TO PUBLIC;
ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY wp009_read_trap ON public.schema_migrations USING (public.wp009_read_trap());
"""


def test_green_every_administrative_workflow_read_is_catalog_only(hardened):
    """With both traps planted, every SQL file the workflow runs as the administrator (diagnose,
    model, restore precheck, fingerprints) touches no bridge row: no trap fires."""
    role = _plant_trap(hardened)
    read_role = "wp009_esc_" + uuid.uuid4().hex[:6]
    hardened.extra_roles.append(read_role)
    hardened.superuser(POLICY_TRAP.replace("{role}", read_role), as_role="fcb_migrator")
    for f in ("defaultdb_precheck.sql", "verify_bridge_roles.sql", "diagnose_bridge_roles.sql",
              "fingerprint_bridge_schema.sql", "fingerprint_bridge_structure.sql"):
        rc, _, err = hardened.psql("-f", str(AIVEN / f))
        assert rc == 0, (f, err)
    assert hardened.state().startswith("REFUSE_")  # never TRACKED: the tracked part never runs
    assert not _role_exists(role) and not _role_exists(read_role) and _trap_log(hardened) == []


def test_green_phase1_rerun_and_phase2_rerun_refuse_planted_code_without_running_it(hardened):
    role = _plant_trap(hardened)
    read_role = "wp009_esc_" + uuid.uuid4().hex[:6]
    hardened.extra_roles.append(read_role)
    hardened.superuser(POLICY_TRAP.replace("{role}", read_role), as_role="fcb_migrator")
    rc, _, err = _enable_tx(hardened)
    assert rc != 0 and "FCB_ROLES_PHASE2_STATE_REFUSED" in err
    assert "routine_set" in err and "trigger_set" in err and "relation_hooks" in err
    rc, _, err = _roles_tx(hardened)
    assert rc != 0 and "FCB_ROLES_OWNER_GUARD_FAILED" in err
    assert not _role_exists(role) and not _role_exists(read_role) and _trap_log(hardened) == []


def test_green_restore_workflow_after_hardening_refuses_without_running_owner_code(hardened):
    """The restore workflow (main, unchanged): its catalog precheck refuses a hardened database
    (REFUSE_DEFAULT_PRIVILEGES) before its tracked part could read schema_migrations, and its
    diagnose-mode inventory (read-only, skips RLS tables) gets permission denied on the bridge;
    with both traps planted nothing runs."""
    from scripts.defaultdb_first_init_inventory import collect_inventory
    role = _plant_trap(hardened)
    read_role = "wp009_esc_" + uuid.uuid4().hex[:6]
    hardened.extra_roles.append(read_role)
    hardened.superuser(POLICY_TRAP.replace("{role}", read_role), as_role="fcb_migrator")
    assert hardened.state() != "TRACKED" and hardened.state().startswith("REFUSE_")
    report = collect_inventory(hardened.admin_dsn, source_sha="wp009")
    assert report["precheck_state"].startswith("REFUSE_") and report["first_time_initialization_proven"] is False
    assert not _role_exists(role) and not _role_exists(read_role) and _trap_log(hardened) == []


@pytest.mark.parametrize("identity", ["fcb_migrator", "fcb_app", "admin"])
def test_event_triggers_need_a_superuser(hardened, identity):
    """The only hook on DDL (which the phase-2 re-run still executes) needs a superuser."""
    hardened.superuser("CREATE FUNCTION public.wp009_evt() RETURNS event_trigger LANGUAGE plpgsql "
                       "AS $$BEGIN NULL; END$$", "GRANT EXECUTE ON FUNCTION public.wp009_evt() TO PUBLIC",
                       as_role="fcb_migrator")
    with hardened.connect(identity) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE EVENT TRIGGER wp009_evt ON ddl_command_start EXECUTE FUNCTION public.wp009_evt()")


def test_migrator_planted_security_definer_only_reaches_the_migrator(hardened):
    role = "wp009_esc_" + uuid.uuid4().hex[:6]
    hardened.extra_roles.append(role)
    hardened.superuser(
        "CREATE FUNCTION public.wp009_def() RETURNS text LANGUAGE plpgsql SECURITY DEFINER AS "
        f"$$BEGIN BEGIN EXECUTE 'CREATE ROLE {role}'; EXCEPTION WHEN OTHERS THEN NULL; END; RETURN current_user; END$$",
        "GRANT EXECUTE ON FUNCTION public.wp009_def() TO fcb_app", as_role="fcb_migrator")
    with hardened.connect("fcb_app") as conn:
        assert conn.execute("SELECT public.wp009_def() AS u").fetchone()["u"] == "fcb_migrator"
    assert not _role_exists(role)
    assert "routine_set" in hardened.roles_state()


def test_admin_option_can_regrant_inheritance_but_diagnose_detects_it(hardened):
    """R1: the automatic ADMIN option (CREATEROLE design) lets the administrator grant itself
    fcb_migrator again; that is a deliberate administrative act, never the default path, and the
    model refuses it immediately (session_reaches_migrator, membership_inventory)."""
    hardened.ok("-c", "GRANT fcb_migrator TO CURRENT_USER WITH INHERIT TRUE, SET TRUE")
    state = hardened.roles_state()
    for code in ("session_reaches_migrator", "membership_inventory", "role_reachability"):
        assert code in state, code


# ------------------------------------------------------------------ one planted violation per model check

def _plant_third_role_grant(env):
    other = env.extra_role()
    env.superuser(SQL("GRANT SELECT ON public.model_snapshots TO {}").format(Identifier(other)), as_role="fcb_migrator")


def _plant_third_role_routine_grant(env):
    other = env.extra_role()
    env.superuser(SQL("GRANT EXECUTE ON FUNCTION public.canonical_reject_mutation() TO {}").format(Identifier(other)),
                  as_role="fcb_migrator")


def _plant_migrator_member(env):
    other = env.extra_role()
    env.superuser(SQL("GRANT fcb_migrator TO {}").format(Identifier(other)))


def _plant_set_member(env):
    other = env.extra_role()
    env.superuser(SQL("GRANT fcb_app TO {} WITH INHERIT FALSE, SET TRUE").format(Identifier(other)))


def _plant_session_owned_object(env):
    env.superuser(SQL("ALTER TABLE public.runtime_state OWNER TO {}").format(Identifier(env.admin_role)))


def _plant_self_setting(env):
    with env.connect("fcb_app") as conn:  # any login may set its own per-role settings
        conn.execute("ALTER ROLE CURRENT_USER SET search_path = public")
        conn.commit()


def _plant_extension(env):
    with psycopg.connect(_dsn_for(env.dbname)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM pg_available_extensions WHERE name = ANY(%s)",
                                            (["pg_trgm", "citext", "pgcrypto", "hstore", "isn", "tablefunc"],)).fetchall()]
    if not names:
        pytest.skip("no contrib extension available on this server")
    env.superuser(f"CREATE EXTENSION {names[0]} SCHEMA public")


def _mig(*stmts):
    return lambda e: e.superuser(*stmts, as_role="fcb_migrator")


def _su(*stmts):
    return lambda e: e.superuser(*stmts)


# (code, plant). Every plant makes verify_bridge_roles.sql report the code. The workflow then
# refuses (state neither HARDENED nor HARDENED_PHASE1) and the phase-2 transaction refuses before
# any write (FCB_ROLES_PHASE2_STATE_REFUSED, catalog-only); nothing is healed automatically.
PLANTS = [
    ("roles_missing", _su("DROP ROLE fcb_signer")),
    ("role_attributes", _su("ALTER ROLE fcb_app CREATEDB")),
    ("role_login", _su("ALTER ROLE fcb_app NOLOGIN")),
    ("role_login", _su("ALTER ROLE fcb_signer LOGIN")),
    ("role_member_of_other_role", _su("GRANT pg_read_all_stats TO fcb_app")),
    ("unexpected_migrator_member", _plant_migrator_member),
    ("session_reaches_migrator", lambda e: e.ok("-c", "GRANT fcb_migrator TO CURRENT_USER WITH INHERIT TRUE, SET FALSE")),
    ("session_reaches_migrator", lambda e: e.ok("-c", "GRANT fcb_migrator TO CURRENT_USER WITH INHERIT FALSE, SET TRUE")),
    ("unexpected_app_or_signer_member", lambda e: e.ok("-c", "GRANT fcb_signer TO CURRENT_USER WITH INHERIT TRUE")),
    ("membership_inventory", lambda e: e.ok("-c", "GRANT fcb_app TO CURRENT_USER WITH INHERIT FALSE, SET FALSE")),
    ("role_reachability", _plant_set_member),
    ("role_reachability", _su("GRANT fcb_app TO pg_read_all_stats")),  # via a predefined role
    ("escalation_path", _su("GRANT fcb_signer TO fcb_app")),
    ("owner_model", _plant_session_owned_object),
    ("unexpected_relation_grant", _plant_third_role_grant),
    ("missing_runtime_grant", _mig("REVOKE INSERT ON public.oauth_access_tokens FROM fcb_app")),
    ("column_grant", _mig("GRANT SELECT (client_id) ON public.oauth_access_tokens TO fcb_signer")),
    ("routine_grant", _plant_third_role_routine_grant),
    ("schema_acl", _su("GRANT USAGE ON SCHEMA public TO PUBLIC")),
    ("signer_schema_usage", _su("GRANT USAGE ON SCHEMA public TO fcb_signer")),
    ("app_schema_create", _su("GRANT CREATE ON SCHEMA public TO fcb_app")),
    ("database_create", lambda e: e.superuser(SQL("GRANT CREATE ON DATABASE {} TO fcb_app").format(Identifier(e.dbname)))),
    ("database_create", lambda e: e.superuser(SQL("GRANT CREATE ON DATABASE {} TO fcb_migrator").format(Identifier(e.dbname)))),
    ("default_acl_grant", _mig("ALTER DEFAULT PRIVILEGES FOR ROLE fcb_migrator IN SCHEMA public GRANT SELECT ON TABLES TO fcb_app")),
    ("default_function_revoke_missing", _mig("ALTER DEFAULT PRIVILEGES FOR ROLE fcb_migrator GRANT EXECUTE ON FUNCTIONS TO PUBLIC")),
    ("role_settings", _plant_self_setting),
    ("routine_set", _mig("CREATE FUNCTION public.wp009_extra() RETURNS integer LANGUAGE sql AS 'SELECT 1'")),
    ("routine_set", _mig("CREATE OR REPLACE FUNCTION public.canonical_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS "
                         "$$BEGIN RETURN NULL; END$$")),
    ("routine_set", _mig("ALTER FUNCTION public.canonical_reject_mutation() SECURITY DEFINER")),
    ("routine_set", _mig("ALTER FUNCTION public.canonical_reject_mutation() SET search_path = public")),
    ("routine_set", _mig("CREATE FUNCTION public.wp009_chk(text) RETURNS boolean LANGUAGE sql AS 'SELECT true'",
                         "ALTER TABLE public.oauth_access_tokens ADD CONSTRAINT wp009_c CHECK (public.wp009_chk(client_id))")),
    ("trigger_set", _mig("CREATE TRIGGER wp009_t BEFORE INSERT ON public.oauth_access_tokens FOR EACH STATEMENT "
                         "EXECUTE FUNCTION public.canonical_reject_mutation()")),
    ("trigger_set", _mig("ALTER TABLE public.canonical_entities DISABLE TRIGGER canonical_entities_immutable")),
    ("trigger_set", _mig("DROP TRIGGER canonical_entities_immutable ON public.canonical_entities")),
    ("expression_functions", _mig("ALTER TABLE public.oauth_access_tokens ADD CONSTRAINT wp009_sc "
                                  "CHECK (pg_catalog.set_config('wp009.x', 'y', false) IS NOT NULL)")),
    ("expression_functions", _mig("ALTER TABLE public.oauth_access_tokens ALTER COLUMN scope "
                                  "SET DEFAULT pg_catalog.set_config('wp009.x', 'y', false)")),
    ("expression_functions", _mig("CREATE INDEX wp009_i ON public.oauth_access_tokens (pg_catalog.upper(client_id))")),
    ("relation_hooks", _mig("CREATE VIEW public.wp009_v AS SELECT 1 AS x")),
    ("relation_hooks", _mig("CREATE VIEW public.wp009_vi WITH (security_invoker = true) AS SELECT 1 AS x")),
    ("relation_hooks", _mig("CREATE MATERIALIZED VIEW public.wp009_mv AS SELECT 1 AS x")),
    ("relation_hooks", _mig("CREATE POLICY wp009_p ON public.schema_migrations USING (true)")),
    ("relation_hooks", _mig("ALTER TABLE public.oauth_access_tokens ENABLE ROW LEVEL SECURITY")),
    ("relation_hooks", _mig("ALTER TABLE public.oauth_access_tokens FORCE ROW LEVEL SECURITY")),  # without ENABLE
    ("relation_hooks", _mig("CREATE RULE wp009_r AS ON INSERT TO public.sxbet_rate_attempts DO ALSO NOTIFY wp009")),
    ("relation_hooks", _mig("CREATE TABLE public.wp009_child () INHERITS (public.runtime_state)")),
    ("relation_hooks", _mig("CREATE TABLE public.wp009_part (k integer) PARTITION BY RANGE (k)")),
    # Superuser-only objects: planted by the test superuser (Aiven's avnadmin cannot create them).
    ("relation_hooks", _su("CREATE FUNCTION public.wp009_evt() RETURNS event_trigger LANGUAGE plpgsql AS $$BEGIN NULL; END$$",
                           "CREATE EVENT TRIGGER wp009_evt ON ddl_command_start EXECUTE FUNCTION public.wp009_evt()")),
    ("foreign_objects", _mig("CREATE DOMAIN public.wp009_d AS text CHECK (VALUE <> '')")),
    ("foreign_objects", _mig("CREATE OPERATOR public.=== (LEFTARG = text, RIGHTARG = text, FUNCTION = pg_catalog.texteq)")),
    ("foreign_objects", _mig("CREATE TEXT SEARCH CONFIGURATION public.wp009_ts (COPY = pg_catalog.simple)")),
    ("foreign_objects", _mig("CREATE TEXT SEARCH DICTIONARY public.wp009_dict (TEMPLATE = pg_catalog.simple)")),
    ("foreign_objects", _mig("CREATE TYPE public.wp009_e AS ENUM ('a')")),
    ("foreign_objects", _mig("CREATE COLLATION public.wp009_coll FROM \"C\"")),
    ("foreign_objects", _mig("CREATE CONVERSION public.wp009_conv FOR 'LATIN1' TO 'UTF8' FROM iso8859_1_to_utf8")),
    ("foreign_objects", _mig("CREATE TYPE public.wp009_ct AS (a integer)",
                             "CREATE CAST (public.wp009_ct AS text) WITH INOUT")),
    ("foreign_objects", _su("CREATE OPERATOR FAMILY public.wp009_opf USING btree")),
    ("foreign_objects", _su("CREATE OPERATOR CLASS public.wp009_opc FOR TYPE text USING hash AS OPERATOR 1 pg_catalog.=(text, text), "
                            "FUNCTION 1 pg_catalog.hashtext(text)")),
    ("foreign_objects", _su("CREATE TEXT SEARCH TEMPLATE public.wp009_tmpl (LEXIZE = pg_catalog.dsimple_lexize)")),
    ("foreign_objects", _su("CREATE TEXT SEARCH PARSER public.wp009_prs (START = pg_catalog.prsd_start, "
                            "GETTOKEN = pg_catalog.prsd_nexttoken, END = pg_catalog.prsd_end, LEXTYPES = pg_catalog.prsd_lextype)")),
    ("foreign_objects", _plant_extension),
]


@pytest.mark.parametrize("code,plant", PLANTS, ids=[f"{c}-{i}" for i, (c, _) in enumerate(PLANTS)])
def test_each_model_check_catches_its_planted_violation(hardened, code, plant):
    assert hardened.roles_state() == "HARDENED"
    plant(hardened)
    state = hardened.roles_state()
    assert state.startswith("NOT_HARDENED:") and code in state.split(":", 1)[1].split(","), state
    rc, _, err = _enable_tx(hardened)
    assert rc != 0 and "FCB_ROLES_PHASE2_STATE_REFUSED" in err and code in err, err
    assert hardened.roles_state() == state  # nothing committed


@pytest.mark.parametrize("code,plant", [
    ("owner_model", lambda e: e.superuser(
        SQL("ALTER TABLE public.runtime_state OWNER TO {}").format(Identifier("fcb_migrator")))),
    ("unexpected_relation_grant", lambda e: e.superuser(
        SQL("GRANT SELECT ON public.model_snapshots TO {}").format(Identifier(e.extra_role())))),
    ("role_login", _su("ALTER ROLE fcb_app NOLOGIN")),
    ("schema_acl", _su("GRANT USAGE ON SCHEMA public TO fcb_migrator")),
], ids=["owner_model", "unexpected_relation_grant", "role_login", "schema_acl"])
def test_phase1_model_checks_catch_their_violations(phase1, code, plant):
    """Phase-1 variants (administrator owns everything, fcb_migrator owns and may do nothing)."""
    assert phase1.roles_state() == "HARDENED_PHASE1"
    plant(phase1)
    state = phase1.roles_state()
    assert state.startswith("NOT_HARDENED:") and code in state, state
    rc, _, err = _roles_tx(phase1)
    assert rc != 0
    rc, _, err = _enable_tx(phase1)
    assert rc != 0 and "FCB_ROLES_PHASE2_STATE_REFUSED" in err


# ------------------------------------------------------------------ the workflow's own bash, run locally

def _workflow_step(prefix):
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return next(s for s in wf["jobs"]["roles"]["steps"] if s.get("name", "").startswith(prefix))


def _localize(script):
    """The workflow bash minus what a local, non-TLS (and possibly non-18) PostgreSQL cannot satisfy."""
    script = script.replace("/usr/lib/postgresql/18/bin/psql", "psql")
    script = script.replace(_TLS_PG18.replace("$", "\\$"), "")
    script = script.replace('[ "$ver" -ge 180000 ] && [ "$ver" -lt 190000 ] ||', "true ||")
    script = script.replace('test "$ssl" = "t" ||', "true ||")
    script = script.replace(" --require-tls", "")
    if _server_major_raw() != 18:  # the pinned full fingerprint is the PG18 value
        script = script.replace('test "$full_before" = "$EXPECTED_FINGERPRINT" ||', "true ||")
        # WP-009 compatibility: the pinned per-state structure fingerprints are PG18 values too
        script = script.replace('test "$structure_before" = "$(structure_for "$bridge")" ||', "true ||")
    return script


def _run_step(env, prefix, tmp_path, passwords=None, mode="apply"):
    step = _workflow_step(prefix)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if not (bindir / "python").exists():
        (bindir / "python").symlink_to(sys.executable)
    summary = tmp_path / "summary.md"
    run_env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "FCB_"))}
    run_env.update({
        "PATH": f"{bindir}:{os.environ['PATH']}", "FCB_DEFAULTDB_URL": env.uri(), "EXPECTED_DB": env.dbname,
        "EXPECTED_COUNTS": env.counts, "EXPECTED_FINGERPRINT": RESTORE_PINNED_FINGERPRINT,
        # WP-009 compatibility (declared intentional change): the per-state pins of the workflow
        **{k: v for k, v in yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["roles"]["env"].items()
           if k.startswith(("EXPECTED_COUNTS_00", "EXPECTED_STRUCTURE_00"))},
        "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_WORKSPACE": str(ROOT), "PGSSLMODE": "disable",
        "FCB_DB_URL_VAR": "FCB_DEFAULTDB_URL", "MODE": mode, "OBSERVE_SECONDS": "1",
        "PGOPTIONS": (step.get("env") or {}).get("PGOPTIONS", "") + " " + AIVEN_SELF_GRANT,
    })
    run_env.pop("PYTHONPATH", None)
    if passwords is not None:
        run_env.update(passwords)
    proc = subprocess.run(["bash", "-c", _localize(step["run"])], env=run_env, capture_output=True,
                          text=True, timeout=300, cwd=str(ROOT))
    text = proc.stdout + proc.stderr + (summary.read_text() if summary.exists() else "")
    return proc.returncode, text


def _assert_no_secret(env, text):
    assert "SCRAM-SHA-256" not in text
    for secret in (*env.passwords.values(), env.admin_password):
        assert secret not in text


def _passwords(env, mode="enable-migrator"):
    pw = {"FCB_APP_DB_PASSWORD": env.passwords["fcb_app"]}
    if mode == "enable-migrator":
        pw["FCB_MIGRATOR_DB_PASSWORD"] = env.passwords["fcb_migrator"]
    return pw


PRECHECK, PHASE1, PHASE2 = "Prechecks and roles report", "Phase 1 -", "Phase 2 -"


def test_workflow_bash_both_phases_and_redispatch_without_leaking_a_secret(fresh, tmp_path):
    outs = []
    rc, out = _run_step(fresh, PRECHECK, tmp_path, mode="diagnose")
    assert rc == 0 and "phase 1 allowed" in out, out[-3000:]
    outs.append(out)
    rc, out = _run_step(fresh, PRECHECK, tmp_path, mode="enable-migrator")
    assert rc != 0 and "roles refused" in out  # phase 2 before phase 1
    rc, out = _run_step(fresh, PHASE1, tmp_path, _passwords(fresh, "apply"))
    assert rc == 0, out[-3000:]
    assert "## Phase 1: PASS" in out and "model after: HARDENED_PHASE1" in out
    assert "login fcb_migrator: refused (phase 1: NOLOGIN, no password)" in out
    outs.append(out)
    rc, out = _run_step(fresh, PHASE1, tmp_path, _passwords(fresh, "apply"))  # re-dispatch
    assert rc == 0 and "## Phase 1: PASS" in out, out[-3000:]
    outs.append(out)
    rc, out = _run_step(fresh, PRECHECK, tmp_path, mode="enable-migrator")
    assert rc == 0 and "HARDENED_PHASE1 accepted for enable-migrator" in out
    with fresh.connect("admin") as other:  # Render still on the administrator: refused
        other.execute("SELECT 1")
        rc, out = _run_step(fresh, PHASE2, tmp_path, _passwords(fresh))
        assert rc != 0 and fresh.roles_state() == "HARDENED_PHASE1"
        outs.append(out)
    rc, out = _run_step(fresh, PHASE2, tmp_path, _passwords(fresh))
    assert rc == 0, out[-3000:]
    assert "## Phase 2: PASS" in out and "model after: HARDENED" in out
    assert "admin: bridge access denied=True inherits_migrator=False can_set_migrator=False" in out
    outs.append(out)
    rc, out = _run_step(fresh, PHASE2, tmp_path, _passwords(fresh))  # re-dispatch (password rotation)
    assert rc == 0 and "## Phase 2: PASS" in out, out[-3000:]
    outs.append(out)
    rc, out = _run_step(fresh, PRECHECK, tmp_path, mode="apply")
    assert rc != 0 and "roles refused" in out  # phase 1 after phase 2
    rc, out = _run_step(fresh, PRECHECK, tmp_path, mode="diagnose")
    assert rc == 0 and "Roles model: HARDENED" in out
    outs.append(out)
    _assert_no_secret(fresh, "".join(outs))
    assert fresh.roles_state() == "HARDENED"


def test_workflow_bash_refuses_planted_code_without_running_it(hardened, tmp_path):
    role = _plant_trap(hardened)
    hardened.superuser(POC_RLS_SQL, as_role="fcb_migrator")
    rc, out = _run_step(hardened, PRECHECK, tmp_path, mode="diagnose")
    assert rc != 0 and "roles refused" in out
    rc, out2 = _run_step(hardened, PHASE2, tmp_path, _passwords(hardened))
    assert rc != 0 and "refusing" in out2
    rc, out3 = _run_step(hardened, PHASE1, tmp_path, _passwords(hardened, "apply"))
    assert rc != 0 and "refusing" in out3
    assert not _role_exists(role) and _trap_log(hardened) == []
    assert _role_flags("fcb_migrator", "fcb_signer") == {"fcb_migrator": (False, True), "fcb_signer": (False, False)}
    _assert_no_secret(hardened, out + out2 + out3)


def test_workflow_bash_refuses_preexisting_roles_on_a_first_apply(fresh, tmp_path):
    fresh.ok("-c", "CREATE ROLE fcb_signer NOLOGIN")
    rc, out = _run_step(fresh, PRECHECK, tmp_path, mode="diagnose")
    # A bridge role exists: the tracked precheck (it would read schema_migrations) is refused.
    assert rc != 0 and "tracked precheck refused once bridge roles exist" in out
    rc, out = _run_step(fresh, PHASE1, tmp_path, _passwords(fresh, "apply"))
    assert rc != 0 and "refusing" in out
    assert fresh.state() == "BRIDGE_0001_0008"


@pytest.mark.parametrize("prefix,mode,bad", [
    ("Fail closed if the fcb_app password", "apply", {"FCB_APP_DB_PASSWORD": "short"}),
    ("Fail closed if the fcb_app password", "apply", {"FCB_APP_DB_PASSWORD": ""}),
    ("Fail closed if a role password", "enable-migrator", {"FCB_MIGRATOR_DB_PASSWORD": "short"}),
    ("Fail closed if a role password", "enable-migrator",
     {"FCB_MIGRATOR_DB_PASSWORD": "same-" + "x" * 40, "FCB_APP_DB_PASSWORD": "same-" + "x" * 40}),
])
def test_workflow_bash_password_gate_fails_closed_without_echoing(fresh, tmp_path, prefix, mode, bad):
    pw = {**_passwords(fresh), **bad}
    rc, out = _run_step(fresh, prefix, tmp_path, pw, mode=mode)
    assert rc != 0 and "role passwords refused" in out
    for v in pw.values():
        if len(v) >= 6:
            assert v not in out
    step = PHASE1 if mode == "apply" else PHASE2
    if mode == "enable-migrator":
        rc, _ = _run_step(fresh, PHASE1, tmp_path, _passwords(fresh, "apply"))
        assert rc == 0
    rc, out = _run_step(fresh, step, tmp_path, pw)  # the write step re-checks via `verifier`
    assert rc != 0
    assert fresh.roles_state() == ("ROLES_ABSENT" if mode == "apply" else "HARDENED_PHASE1")


# ---------------------------------------------------------------- PATH A2 (owner GO): fcb_cert_runner
# PATH A2 adds to defaultdb only the exact quota-coordinator grant of the plain role fcb_cert_runner.
# The WP-009 verify model accepts exactly that grant (and nothing more) in every phase; the restore
# precheck and fingerprints accept it on the 0001-0008 status quo.

def _a2_install(env):
    sys.path.insert(0, str(ROOT / "scripts"))
    from fcb_db_roles import scram_verifier
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname IN ('fcb_cert_runner', 'fcb_cert_owner')").fetchone():
            pytest.skip("PATH A2 roles already exist on this server; not touching them")
    env.extra_roles += ["fcb_cert_runner", "fcb_cert_owner"]
    env.superuser(SQL("ALTER ROLE {} CREATEDB BYPASSRLS").format(Identifier(env.admin_role)))  # Aiven's avnadmin shape
    sql = (AIVEN / "apifootball_cert_path_a_roles.sql").read_text(encoding="utf-8")
    assert sql.count("'defaultdb'::pg_catalog.name") == 1
    sql = sql.replace("'defaultdb'::pg_catalog.name", f"'{env.dbname}'::pg_catalog.name")
    with psycopg.connect(env.admin_dsn, options=ADMIN_OPTIONS) as conn:
        conn.execute("SELECT pg_catalog.set_config('fcb_cert.runner_verifier', %s, true)",
                     (scram_verifier(secrets.token_urlsafe(32)),))
        conn.execute(sql)


def _a2_runner_privileges(env):
    with psycopg.connect(_dsn_for(env.dbname)) as conn:
        return conn.execute(
            "SELECT count(*) FILTER (WHERE a.attname IS NULL), count(*) FILTER (WHERE a.attname IS NOT NULL), "
            "bool_and(pg_get_userbyid(x.grantor) = pg_get_userbyid(c.relowner)) FROM pg_class c "
            "LEFT JOIN LATERAL aclexplode(c.relacl) x ON true LEFT JOIN pg_attribute a ON false "
            "WHERE x.grantee = 'fcb_cert_runner'::regrole "
            "UNION ALL SELECT 0, count(*), true FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid, "
            "LATERAL aclexplode(a.attacl) x WHERE x.grantee = 'fcb_cert_runner'::regrole").fetchall()


def test_a2_status_quo_restore_checks_and_both_wp009_phases_accept_exactly_the_runner_grant(fresh):
    assert fresh.roles_state() == "ROLES_ABSENT"  # WITHOUT A2: unchanged
    _a2_install(fresh)
    # restore precheck, full and structure fingerprints: unchanged by the exact runner grant
    assert fresh.state() == "BRIDGE_0001_0008"
    assert fresh.full_fingerprint() == fresh.full_before and fresh.structure_fingerprint() == fresh.structure_before
    if _server_major_raw() == 18:
        assert fresh.full_fingerprint() == RESTORE_PINNED_FINGERPRINT
    rc, _, err = _roles_tx(fresh)  # WP-009 phase 1 through the workflow's transaction
    assert rc == 0, err
    assert fresh.roles_state() == "HARDENED_PHASE1"
    assert fresh.structure_fingerprint() == fresh.structure_before
    rc, _, err = _enable_tx(fresh)  # phase 2: ownership to fcb_migrator (the runner's grantor follows)
    assert rc == 0, err
    assert fresh.roles_state() == "HARDENED"
    assert fresh.structure_fingerprint() == fresh.structure_before
    assert _a2_runner_privileges(fresh) == [(8, 0, True), (0, 1, True)]
    # documented residual: phase 1 revokes PUBLIC's USAGE on public, so the A2 runner can no longer
    # reach the coordinator (fail closed: no lease, no request) until the owner decides otherwise
    with psycopg.connect(_dsn_for(fresh.dbname)) as conn:
        assert conn.execute("SELECT has_schema_privilege('fcb_cert_runner', 'public', 'USAGE')").fetchone()[0] is False


@pytest.mark.parametrize("tamper,code", [
    ("GRANT TRUNCATE ON public.api_football_quota_counters TO fcb_cert_runner", "unexpected_relation_grant"),
    ("GRANT SELECT ON public.oauth_access_tokens TO fcb_cert_runner", "unexpected_relation_grant"),
    ("GRANT UPDATE (bucket) ON public.api_football_quota_counters TO fcb_cert_runner", "column_grant"),
    ("REVOKE DELETE ON public.api_football_quota_leases FROM fcb_cert_runner", "missing_runtime_grant"),
    ("ALTER ROLE fcb_cert_runner CREATEDB", "unexpected_relation_grant"),
    # review P3 F3: a role that inherits from / can SET to the runner (dropped with the env)
    ("GRANT fcb_cert_runner TO {x} WITH INHERIT TRUE, SET FALSE", "unexpected_relation_grant"),
    ("GRANT fcb_cert_runner TO {x} WITH INHERIT FALSE, SET TRUE", "unexpected_relation_grant"),
])
def test_a2_wp009_verify_still_refuses_anything_beyond_the_exact_runner_grant(fresh, tamper, code):
    _a2_install(fresh)
    rc, _, err = _roles_tx(fresh)
    assert rc == 0, err
    if "{x}" in tamper:
        tamper = tamper.format(x=fresh.extra_role())
    fresh.superuser(tamper)
    state = fresh.roles_state()
    assert state.startswith("NOT_HARDENED") and code in state, state
