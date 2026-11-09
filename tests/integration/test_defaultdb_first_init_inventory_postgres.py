"""WP-005 first-install inventory on a real disposable PostgreSQL database."""
from __future__ import annotations

import contextlib
import os
import shutil
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402

from scripts.defaultdb_first_init_inventory import collect_inventory  # noqa: E402

pytestmark = [
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires dedicated TEST_DATABASE_URL"),
    pytest.mark.skipif(shutil.which("psql") is None and os.environ.get("FCB_REQUIRE_PSQL") != "1",
                       reason="psql client not installed"),
]


@pytest.fixture
def isolated_database():
    """Give the proof tests their own database, avoiding parallel-suite schemas."""
    admin_dsn = os.environ["TEST_DATABASE_URL"]
    name = "wp005_inv_" + uuid.uuid4().hex[:12]
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    params["dbname"] = name
    dsn = psycopg.conninfo.make_conninfo(**params)
    yield dsn
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_empty_database_inventory_proves_first_initialization_without_writes(isolated_database):
    report = collect_inventory(isolated_database, source_sha="test-sha")
    assert report["transaction_read_only"] is True
    assert report["precheck_state"] == "EMPTY"
    assert report["unexpected_schemas"] == []
    assert report["objects"] == []
    assert report["row_counts"] == {}
    assert report["first_time_initialization_proven"] is True
    with psycopg.connect(isolated_database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchone()[0] == 0


def test_nonempty_database_reports_counts_and_never_reads_or_prints_contents(isolated_database):
    secret_marker = "DO_NOT_PRINT_THIS_VALUE"
    with psycopg.connect(isolated_database, autocommit=True) as conn:
        conn.execute("CREATE TABLE public.oauth_access_tokens (token text)")
        conn.execute(
            "INSERT INTO public.oauth_access_tokens(token) VALUES (%s), (%s)",
            (secret_marker, "second-secret"),
        )
    report = collect_inventory(isolated_database, source_sha="test-sha")
    rendered = str(report)
    assert report["precheck_state"] == "REFUSE_UNTRACKED_OBJECTS"
    assert report["row_counts"] == {"oauth_access_tokens": 2}
    assert report["application_rows_total"] == 2
    assert report["first_time_initialization_proven"] is False
    assert secret_marker not in rendered and "second-secret" not in rendered


def test_inventory_transaction_rejects_writes(isolated_database):
    # The production collector checks transaction_read_only=on.  Prove the same
    # transaction property rejects DDL/DML on the dedicated PostgreSQL server.
    with psycopg.connect(isolated_database, autocommit=True) as conn:
        conn.execute("BEGIN TRANSACTION READ ONLY")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE public.forbidden_write(id int)")
        conn.execute("ROLLBACK")


# ---------------------------------------------------------------- audit of 2026-11-09 (F1, F6, F8)

def _exec(dsn, *statements):
    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


@contextlib.contextmanager
def _owner(dsn):
    """A non-superuser LOGIN role (random password: CI uses password auth) owning the database,
    like Aiven's avnadmin. Superusers bypass row security, so F8 needs this session."""
    role = "wp005_inv_owner_" + uuid.uuid4().hex[:8]
    password = uuid.uuid4().hex
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    _exec(dsn, sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)),
          sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(params["dbname"]), sql.Identifier(role)))
    params["user"], params["password"] = role, password
    try:
        yield role, psycopg.conninfo.make_conninfo(**params)
    finally:
        _exec(dsn, sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(sql.Identifier(params["dbname"])),
              sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(sql.Identifier(role)),
              sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)),
              sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_lookalike_temp_schema_is_reported_and_blocks_the_proof(isolated_database):
    """F1: LIKE 'pg_temp%' matched 'pgxtemp' and hid it from unexpected_schemas."""
    _exec(isolated_database, "CREATE SCHEMA pgxtemp", "CREATE TABLE pgxtemp.hidden(id int)")
    report = collect_inventory(isolated_database)
    assert "pgxtemp" in report["schemas"] and report["unexpected_schemas"] == ["pgxtemp"]
    assert report["precheck_state"] == "REFUSE_UNEXPECTED_SCHEMAS"
    assert report["first_time_initialization_proven"] is False


@pytest.mark.parametrize("aiven_shape,grant", [(True, None), (False, None),
                                               (False, "WITH INHERIT FALSE, SET TRUE"),
                                               (False, "WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")])
def test_avnadmin_monitor_is_excluded_only_in_aivens_shape_and_listed(isolated_database, aiven_shape, grant):
    created = False
    with psycopg.connect(isolated_database, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '_avnadmin_monitor'").fetchone() is None:
            conn.execute("CREATE ROLE _avnadmin_monitor")
            created = True
    try:
        with _owner(isolated_database) as (role, owner_dsn):
            schema_owner = "_avnadmin_monitor" if aiven_shape or grant else role
            _exec(isolated_database,
                  sql.SQL("CREATE SCHEMA _avnadmin_monitor AUTHORIZATION {}").format(sql.Identifier(schema_owner)),
                  "CREATE FUNCTION _avnadmin_monitor.probe() RETURNS int LANGUAGE sql AS 'select 1'")
            if grant:  # a membership without inherited CREATE: only pg_has_role(MEMBER) sees it
                _exec(isolated_database, sql.SQL("GRANT _avnadmin_monitor TO {} " + grant).format(sql.Identifier(role)))
            report = collect_inventory(owner_dsn)
            if aiven_shape:
                assert report["excluded_schemas"] == ["_avnadmin_monitor"]
                assert report["unexpected_schemas"] == []
                assert report["first_time_initialization_proven"] is True
            else:
                assert report["excluded_schemas"] == []
                assert report["unexpected_schemas"] == ["_avnadmin_monitor"]
                assert report["first_time_initialization_proven"] is False
            _exec(isolated_database, "DROP SCHEMA _avnadmin_monitor CASCADE")
            if grant:
                _exec(isolated_database, sql.SQL("REVOKE _avnadmin_monitor FROM {}").format(sql.Identifier(role)))
    finally:
        if created:
            _exec(isolated_database, "DROP ROLE _avnadmin_monitor")


@pytest.mark.parametrize("ddl,category", [
    ("SELECT pg_catalog.lo_create(0)", "other_objects"),
    ("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC", "default_privileges"),
    ("CREATE TYPE public.shell_only", "types"),
])
def test_empty_proof_requires_every_occupancy_category(isolated_database, ddl, category):
    """F6: these used to leave first_time_initialization_proven = true."""
    _exec(isolated_database, ddl)
    report = collect_inventory(isolated_database)
    assert report["occupancy"][category] > 0
    assert report["first_time_initialization_proven"] is False


def test_row_security_and_foreign_tables_are_never_counted(isolated_database):
    """F8: count(*) executed FORCE-RLS policies (arbitrary functions) and foreign-table wrappers."""
    _exec(isolated_database,
          "CREATE FOREIGN DATA WRAPPER wp005_nohandler",
          "CREATE SERVER wp005_srv FOREIGN DATA WRAPPER wp005_nohandler",
          "CREATE FOREIGN TABLE public.remote (x int) SERVER wp005_srv")
    with _owner(isolated_database) as (role, owner_dsn):
        _exec(owner_dsn,
              "CREATE FUNCTION public.boom() RETURNS boolean LANGUAGE plpgsql AS "
              "$$ BEGIN RAISE EXCEPTION 'FCB_POLICY_RAN'; END $$",
              "CREATE TABLE public.guarded (x int)",
              "INSERT INTO public.guarded VALUES (1)",
              "ALTER TABLE public.guarded ENABLE ROW LEVEL SECURITY",
              "ALTER TABLE public.guarded FORCE ROW LEVEL SECURITY",
              "CREATE POLICY p ON public.guarded USING (public.boom())",
              "CREATE TABLE public.schema_migrations (version int, name text, sha256 text)",
              "ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY",
              "ALTER TABLE public.schema_migrations FORCE ROW LEVEL SECURITY",
              "CREATE POLICY p ON public.schema_migrations USING (public.boom())",
              "CREATE TABLE public.parent (x int)",
              "CREATE TABLE public.kid () INHERITS (public.parent)",
              "INSERT INTO public.kid VALUES (1), (2)")
        report = collect_inventory(owner_dsn)
    assert report["row_counts"]["guarded"] == "UNKNOWN_ROW_SECURITY"
    assert report["row_counts"]["schema_migrations"] == "UNKNOWN_ROW_SECURITY"
    assert report["migration_rows"] == "UNKNOWN_ROW_SECURITY"
    assert report["row_counts"]["remote"] == "UNKNOWN_FOREIGN_TABLE"
    assert report["row_counts"]["parent"] == 0 and report["row_counts"]["kid"] == 2  # ONLY: no double count
    assert report["first_time_initialization_proven"] is False
