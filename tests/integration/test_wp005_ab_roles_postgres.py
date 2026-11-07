"""FCB-WP-005/008/009: reproducible Path A/B role and ownership comparison.

The CI PostgreSQL 18 service is isolated and contains no production credentials.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg import sql  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AIVEN = ROOT / "sql" / "aiven"
BRIDGE = AIVEN / "aiven_migrations_bridge_0001_0008.sql"
PREPARE = AIVEN / "prepare_bridge_roles.sql"
FINALIZE = AIVEN / "finalize_bridge_privileges.sql"

pytestmark = [
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires isolated TEST_DATABASE_URL"),
    pytest.mark.skipif(os.environ.get("FCB_REQUIRE_PSQL") != "1", reason="requires real psql in CI"),
]


def _dsn(dbname: str) -> str:
    values = conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
    values["dbname"] = dbname
    return make_conninfo(**values)


def _run(dbname: str, *args: str):
    return subprocess.run(
        ["psql", _dsn(dbname), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", *args],
        capture_output=True, text=True, timeout=120,
    )


@pytest.fixture(scope="module")
def ab_cluster():
    suffix = uuid.uuid4().hex[:8]
    db_a, db_b = f"wp005_a_{suffix}", f"wp005_b_{suffix}"
    admin = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(admin, autocommit=True) as conn:
        for role in ("fcb_app", "fcb_signer", "fcb_migrator"):
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        conn.execute("CREATE ROLE fcb_migrator NOLOGIN")
        conn.execute("CREATE ROLE fcb_app NOLOGIN")
        conn.execute("CREATE ROLE fcb_signer NOLOGIN")
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_a)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_b)))
    try:
        yield db_a, db_b
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            for dbname in (db_a, db_b):
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
            for role in ("fcb_app", "fcb_signer", "fcb_migrator"):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _prepare(dbname: str):
    result = _run(dbname, "-f", str(PREPARE))
    assert result.returncode == 0, result.stderr


def _apply(dbname: str, *, as_migrator: bool):
    args = []
    if as_migrator:
        args += ["-c", "SET ROLE fcb_migrator"]
    args += ["-1", "-f", str(BRIDGE)]
    result = _run(dbname, *args)
    assert result.returncode == 0, result.stderr


def _finalize(dbname: str):
    result = _run(dbname, "-f", str(FINALIZE))
    assert result.returncode == 0, result.stderr


def _fingerprint(dbname: str) -> str:
    result = _run(dbname, "-f", str(AIVEN / "fingerprint_bridge_schema.sql"))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _assert_final_security(dbname: str):
    def app_connect():
        conn = psycopg.connect(_dsn(dbname))
        conn.execute("SET ROLE fcb_app")
        return conn

    with psycopg.connect(_dsn(dbname)) as conn:
        owners = conn.execute("""SELECT DISTINCT r.rolname FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner
            WHERE n.nspname='public' AND c.relkind IN ('r','p','S','v','m')""").fetchall()
        assert owners == [("fcb_migrator",)]

        conn.execute("SET ROLE fcb_app")
        conn.execute("INSERT INTO oauth_claude_dcr_clients(client_id_hash,redirect_uri,expires_at) "
                     "VALUES ('ab','https://example.invalid/cb',now()+interval '1 hour')")
        assert conn.execute("SELECT count(*) FROM oauth_claude_dcr_clients").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE forbidden(id integer)")
        conn.rollback()
        conn.execute("SET ROLE fcb_app")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM model_snapshots")
        conn.rollback()
        conn.execute("SET ROLE fcb_signer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            # Qualify the relation so this proves denial by the schema ACL. An
            # unqualified lookup is intentionally hidden as UndefinedTable when
            # the signer has no USAGE on public.
            conn.execute("SELECT * FROM public.oauth_access_tokens")

    # Exercise the actual runtime readiness and both shared coordinators using
    # the least-privileged identity. These checks are read-only and must not DDL.
    from bridge import schema_readiness, oauth_store
    from bridge.api_football.quota import PostgresQuota, QuotaLimits
    from bridge.sxbet.ratelimit import PostgresRateCoordinator

    assert schema_readiness.evaluate(app_connect)["state"] == "READY"
    PostgresQuota(QuotaLimits(8, 80, 1), connect=app_connect)._ensure_schema()
    PostgresRateCoordinator(connect=app_connect)._ensure_schema()

    original_conn = oauth_store._conn
    oauth_store._conn = app_connect
    try:
        client_id = oauth_store.register_claude_dcr_client("https://claude.ai/api/mcp/auth_callback")
        assert oauth_store.valid_claude_dcr_client(
            client_id, "https://claude.ai/api/mcp/auth_callback"
        )
    finally:
        oauth_store._conn = original_conn


def test_path_a_schema_then_hardening(ab_cluster):
    db_a, _ = ab_cluster
    _apply(db_a, as_migrator=False)
    _prepare(db_a)
    _finalize(db_a)
    _assert_final_security(db_a)


def test_path_b_hardening_then_schema(ab_cluster):
    _, db_b = ab_cluster
    _prepare(db_b)
    _apply(db_b, as_migrator=True)
    _finalize(db_b)
    _assert_final_security(db_b)


def test_paths_end_with_identical_catalog_and_idempotent_bridge(ab_cluster):
    db_a, db_b = ab_cluster
    assert _fingerprint(db_a) == _fingerprint(db_b)
    before = _fingerprint(db_b)
    _apply(db_b, as_migrator=True)
    assert _fingerprint(db_b) == before


def test_privilege_finalize_can_be_rolled_back(ab_cluster):
    db_a, _ = ab_cluster
    result = _run(db_a, "-c", "BEGIN", "-f", str(FINALIZE), "-c", "ROLLBACK")
    assert result.returncode == 0, result.stderr
    _assert_final_security(db_a)
