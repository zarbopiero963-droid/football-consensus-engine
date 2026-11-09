"""Review of #178 (second round) on REAL PostgreSQL: the PG18 test workflow's EMPTY decision.

Each test gets its own database, read through the real psql client with exactly the workflow's
flags (-X -A -t -F '=', PGOPTIONS search_path=public) and classified by scripts/pg18_state.py.
RED controls: a `pgxtemp` schema (LIKE 'pg_temp%' matched it) and catalog-shadowing views
behind `ALTER DATABASE ... SET search_path = public, pg_catalog` both used to read as EMPTY.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402

from scripts import pg18_state  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AIVEN = ROOT / "sql" / "aiven"
WORKFLOW_PGOPTIONS = "-c search_path=public"

pytestmark = [
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="requires dedicated TEST_DATABASE_URL"),
    pytest.mark.skipif(shutil.which("psql") is None and os.environ.get("FCB_REQUIRE_PSQL") != "1",
                       reason="psql client not installed"),
]


@pytest.fixture
def db():
    admin = os.environ["TEST_DATABASE_URL"]
    name = "pg18_state_" + uuid.uuid4().hex[:12]
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = psycopg.conninfo.conninfo_to_dict(admin)
    params["dbname"] = name
    dsn = psycopg.conninfo.make_conninfo(**params)
    yield name, dsn
    with psycopg.connect(dsn, autocommit=True) as conn:  # subscriptions block DROP DATABASE
        for (sub,) in conn.execute("SELECT subname FROM pg_catalog.pg_subscription s JOIN pg_catalog.pg_database d "
                                   "ON d.oid = s.subdbid WHERE d.datname = current_database()").fetchall():
            conn.execute(sql.SQL("DROP SUBSCRIPTION {}").format(sql.Identifier(sub)))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _exec(dsn, *statements):
    with psycopg.connect(dsn, autocommit=True) as conn:
        for s in statements:
            conn.execute(s)


def _psql(dsn, *args, pgoptions=WORKFLOW_PGOPTIONS):
    env = {**os.environ, "PGOPTIONS": pgoptions} if pgoptions is not None else \
        {k: v for k, v in os.environ.items() if k != "PGOPTIONS"}
    proc = subprocess.run(["psql", dsn, "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", *args],
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def _occupancy(dsn, **kw):
    return pg18_state.occupancy(_psql(dsn, "-F", "=", "-f", str(AIVEN / "pg18_occupancy.sql"), **kw))


def _expected_rows():
    return "\n".join(
        f"{int(f.name[:4])}={f.name}={hashlib.sha256(f.read_bytes()).hexdigest()}"
        for f in sorted((ROOT / "sql" / "migrations" / "bridge").glob("000[1-7]_*.sql")))


def test_a_fresh_database_is_empty(db):
    assert _occupancy(db[1]) == "EMPTY"


@pytest.mark.parametrize("ddl,expected", [
    (["CREATE SCHEMA pgxtemp", "CREATE TABLE pgxtemp.hidden(id int)"], "UNEXPECTED_SCHEMAS"),
    (["CREATE TABLE public.t(id int)"], "NOT_EMPTY"),
    (["CREATE VIEW public.v AS SELECT 1 AS x"], "NOT_EMPTY"),
    (["CREATE SEQUENCE public.s"], "NOT_EMPTY"),
    (["CREATE FUNCTION public.f() RETURNS int LANGUAGE sql AS 'select 1'"], "NOT_EMPTY"),
    (["CREATE TYPE public.e AS ENUM ('a')"], "NOT_EMPTY"),
    (["CREATE DOMAIN public.d AS int"], "NOT_EMPTY"),
    (["CREATE TYPE public.c AS (a int)"], "NOT_EMPTY"),
    (["CREATE COLLATION public.coll (provider = libc, locale = 'C')"], "NOT_EMPTY"),
    (["CREATE TEXT SEARCH CONFIGURATION public.ts (COPY = pg_catalog.simple)"], "NOT_EMPTY"),
    (["CREATE TEXT SEARCH DICTIONARY public.d (TEMPLATE = pg_catalog.simple)"], "NOT_EMPTY"),
    (["CREATE FUNCTION public.eq(int, int) RETURNS bool LANGUAGE sql AS 'select $1 = $2'",
      "CREATE OPERATOR public.=== (LEFTARG = int, RIGHTARG = int, FUNCTION = public.eq)"], "NOT_EMPTY"),
    (["CREATE CONVERSION public.cv FOR 'LATIN1' TO 'UTF8' FROM pg_catalog.iso8859_1_to_utf8"], "NOT_EMPTY"),
    (["CREATE EXTENSION pgcrypto"], "NOT_EMPTY"),
    (["SELECT pg_catalog.lo_create(0)"], "NOT_EMPTY"),
    (["CREATE PUBLICATION pub"], "NOT_EMPTY"),
    (["ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO PUBLIC"], "NOT_EMPTY"),
    (["ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"], "NOT_EMPTY"),
    (["CREATE SCHEMA _avnadmin_monitor", "CREATE TABLE _avnadmin_monitor.hidden(id int)"], "UNEXPECTED_SCHEMAS"),
    (["CREATE SUBSCRIPTION sub CONNECTION 'host=127.0.0.1 port=1 dbname=x' PUBLICATION p "
      "WITH (connect = false, slot_name = NONE, enabled = false)"], "NOT_EMPTY"),
])
def test_any_user_object_or_schema_is_never_empty(db, ddl, expected):
    _exec(db[1], *ddl)
    assert _occupancy(db[1]) == expected


def test_database_settings_count_except_search_path(db):
    name, dsn = db
    _exec(dsn, sql.SQL("ALTER DATABASE {} SET search_path = public, pg_catalog").format(sql.Identifier(name)))
    assert _occupancy(dsn) == "EMPTY"  # overridden by the workflow's startup option
    _exec(dsn, sql.SQL("ALTER DATABASE {} SET check_function_bodies = off").format(sql.Identifier(name)))
    assert _occupancy(dsn) == "NOT_EMPTY"


@pytest.mark.parametrize("superuser,expected", [(True, "EMPTY"), (False, "UNEXPECTED_SCHEMAS")])
def test_aiven_monitor_schema_is_excluded_only_when_another_superuser_owns_it(db, superuser, expected):
    """Round 4 P3-a: a non-superuser helper role cannot hide objects in `_avnadmin_monitor`."""
    name, dsn = db
    role = "avn_mon_" + uuid.uuid4().hex[:8]
    _exec(dsn, sql.SQL("CREATE ROLE {} " + ("SUPERUSER" if superuser else "")).format(sql.Identifier(role)),
          sql.SQL("CREATE SCHEMA _avnadmin_monitor AUTHORIZATION {}").format(sql.Identifier(role)),
          "CREATE TABLE _avnadmin_monitor.hidden(id int)")
    try:
        assert _occupancy(dsn) == expected
    finally:
        _exec(dsn, "DROP SCHEMA _avnadmin_monitor CASCADE", sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_occupancy_runs_as_a_non_superuser_database_owner(db):
    """Aiven's avnadmin is not a superuser: every catalog the occupancy query reads must be readable."""
    name, dsn = db
    role = "occ_owner_" + uuid.uuid4().hex[:8]
    password = uuid.uuid4().hex  # CI authenticates with passwords; local clusters may trust
    _exec(dsn, sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)),
          sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(name), sql.Identifier(role)))
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    params["user"], params["password"] = role, password
    try:
        assert _occupancy(psycopg.conninfo.make_conninfo(**params)) == "EMPTY"
    finally:
        _exec(dsn, sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(sql.Identifier(name)),
              sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_role_wide_settings_of_the_connecting_role_count(db):
    """Round 4 P3-b: ALTER ROLE <self> SET ... (not search_path) applies to the workflow session."""
    name, dsn = db
    _exec(dsn, "ALTER ROLE CURRENT_USER SET transform_null_equals = on")
    try:
        assert _occupancy(dsn) == "NOT_EMPTY"
    finally:
        _exec(dsn, "ALTER ROLE CURRENT_USER RESET transform_null_equals")
    assert _occupancy(dsn) == "EMPTY"


def _guard_empty() -> str:
    """GUARD_EMPTY exactly as the workflow's apply step builds it (evaluated by bash)."""
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "fcb-aiven-pg18-test.yml").read_text())
    run = next(s["run"] for s in wf["jobs"]["pg18-test"]["steps"]
               if s.get("name", "").startswith("Apply 0001-0007"))
    lines = [l for l in run.splitlines() if l.strip().startswith(("OCC=", "GUARD_EMPTY="))]
    assert len(lines) == 2
    proc = subprocess.run(["bash", "-c", "set -euo pipefail\n" + "\n".join(lines) + '\nprintf %s "$GUARD_EMPTY"'],
                          cwd=ROOT, capture_output=True, text=True, check=True)
    return proc.stdout


def _workflow_var(var: str) -> str:
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "fcb-aiven-pg18-test.yml").read_text())
    run = next(s["run"] for s in wf["jobs"]["pg18-test"]["steps"]
               if s.get("name", "").startswith("Apply 0001-0007"))
    line = next(l for l in run.splitlines() if l.strip().startswith(var + "="))
    return subprocess.run(["bash", "-c", line + f'\nprintf %s "${var}"'], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout


def _first_apply(dsn):
    """The workflow's first apply minus the TLS/PG18 target GUARD (the local server has neither):
    same transaction, advisory lock, emptiness re-check, migrations, post-apply guard."""
    env = {**os.environ, "PGOPTIONS": WORKFLOW_PGOPTIONS}
    return subprocess.run(["psql", dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-1",
                           "-c", _workflow_var("LOCK"), "-c", _guard_empty(),
                           "-f", str(AIVEN / "aiven_migrations_0001_0007.sql"),
                           "-f", str(AIVEN / "pg18_post_apply_guard.sql")],
                          env=env, capture_output=True, text=True, timeout=120)


def _migrated(dsn) -> bool:
    return _psql(dsn, "-c", "select pg_catalog.to_regclass('public.schema_migrations') is not null").strip() == "t"


def test_the_first_apply_commits_on_a_truly_empty_database(db):
    proc = _first_apply(db[1])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _migrated(db[1]) and _occupancy(db[1]) == "NOT_EMPTY"


def test_a_planted_comparison_operator_cannot_defeat_the_emptiness_recheck(db):
    """Round 4 P1-1 (CVE-2018-1058 pattern): public.<>(numeric, integer) returning false."""
    _exec(db[1], "CREATE TABLE public.loot(x int)",
          "CREATE FUNCTION public.ne(numeric, integer) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'select false'",
          "CREATE OPERATOR public.<> (LEFTARG = numeric, RIGHTARG = integer, FUNCTION = public.ne)",
          "CREATE FUNCTION public.ne8(bigint, bigint) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'select false'",
          "CREATE OPERATOR public.<> (LEFTARG = bigint, RIGHTARG = bigint, FUNCTION = public.ne8)")
    proc = _first_apply(db[1])
    assert proc.returncode != 0 and "FCB_DATABASE_NOT_EMPTY" in proc.stderr
    assert not _migrated(db[1])


def test_holding_the_advisory_lock_to_plant_objects_after_the_check_fails_the_apply(db):
    """Round 4 P1-2: the lock is taken BEFORE the re-check, so planting while we wait is seen."""
    import threading
    import time
    dsn = db[1]
    locked = threading.Event()

    def attacker():
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("SELECT pg_catalog.pg_advisory_lock(460244120)")
            locked.set()
            time.sleep(3)
            conn.execute("CREATE SCHEMA evil")
            conn.execute("CREATE TABLE evil.loot(x int)")
            conn.execute("SELECT pg_catalog.pg_advisory_unlock(460244120)")

    t = threading.Thread(target=attacker)
    t.start()
    assert locked.wait(10)
    proc = _first_apply(dsn)
    t.join()
    assert proc.returncode != 0
    assert "FCB_DATABASE_NOT_EMPTY" in proc.stderr or "FCB_FOREIGN_OBJECTS_AFTER_APPLY" in proc.stderr
    assert not _migrated(dsn)


def test_the_post_apply_guard_refuses_objects_this_transaction_did_not_create(db):
    """Round 4 P1-2, second half: a pre-existing table that IF NOT EXISTS would silently skip, or
    any object committed concurrently, is not this transaction's: nothing is committed."""
    dsn = db[1]
    _exec(dsn, "CREATE TABLE public.decision_requests_shadow(x int)")
    env = {**os.environ, "PGOPTIONS": WORKFLOW_PGOPTIONS}
    proc = subprocess.run(["psql", dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-1",
                           "-f", str(AIVEN / "aiven_migrations_0001_0007.sql"),
                           "-f", str(AIVEN / "pg18_post_apply_guard.sql")],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0 and "FCB_FOREIGN_OBJECTS_AFTER_APPLY" in proc.stderr
    assert not _migrated(dsn)


def test_the_apply_transaction_rechecks_emptiness_and_writes_nothing_on_a_race(db):
    """Review of #178 (third round) P2-1: an object planted after the precheck aborts the apply."""
    name, dsn = db
    guard = _guard_empty()
    _exec(dsn, "CREATE TABLE public.planted(id int)")
    env = {**os.environ, "PGOPTIONS": WORKFLOW_PGOPTIONS}
    proc = subprocess.run(["psql", dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-1", "-c", guard,
                           "-f", str(AIVEN / "aiven_migrations_0001_0007.sql")],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0 and "FCB_DATABASE_NOT_EMPTY" in proc.stderr
    tables = _psql(dsn, "-c", "select count(*) from pg_catalog.pg_tables where schemaname = 'public'")
    assert tables.strip() == "1"  # only the planted table: nothing from the migrations committed
    _exec(dsn, "DROP TABLE public.planted")
    _psql(dsn, "-q", "-1", "-c", guard, "-f", str(AIVEN / "aiven_migrations_0001_0007.sql"))
    assert _occupancy(dsn) == "NOT_EMPTY"


def test_catalog_shadowing_through_search_path_is_defeated(db):
    name, dsn = db
    _exec(dsn, "CREATE TABLE public.victim(id int)",
          "CREATE VIEW public.pg_class AS SELECT * FROM pg_catalog.pg_class WHERE false",
          "CREATE VIEW public.pg_proc AS SELECT * FROM pg_catalog.pg_proc WHERE false",
          "CREATE VIEW public.pg_namespace AS SELECT * FROM pg_catalog.pg_namespace WHERE false",
          "CREATE VIEW public.pg_indexes AS SELECT * FROM pg_catalog.pg_indexes WHERE false",
          sql.SQL("ALTER DATABASE {} SET search_path = public, pg_catalog").format(sql.Identifier(name)))
    # RED control: an unqualified count read through the poisoned default path sees nothing.
    shadowed = _psql(dsn, "-c", "select count(*) from pg_class c join pg_namespace n "
                                "on n.oid = c.relnamespace where n.nspname = 'public'", pgoptions=None)
    assert shadowed.strip() == "0"
    # The workflow's startup option puts pg_catalog first again, and the SQL is qualified anyway.
    assert _psql(dsn, "-c", "select (pg_catalog.current_schemas(true))[1]").strip() == "pg_catalog"
    assert _occupancy(dsn) == "NOT_EMPTY"
    assert _occupancy(dsn, pgoptions=None) == "NOT_EMPTY"


def test_the_applied_0001_0007_series_is_recognised_and_survives_shadowing(db, tmp_path):
    name, dsn = db
    _psql(dsn, "-q", "-1", "-f", str(AIVEN / "aiven_migrations_0001_0007.sql"))
    assert _occupancy(dsn) == "NOT_EMPTY"
    evidence = _psql(dsn, "-F", "=", "-f", str(AIVEN / "verify_schema.sql"))
    rows = evidence.splitlines()[7:]
    assert "\n".join(rows) == _expected_rows()
    major = int(_psql(dsn, "-c", "show server_version_num").strip()) // 10000
    if major == 18:  # the pinned counts are the Aiven PG18 ones (constraint catalogs differ by major)
        assert pg18_state.classify(evidence, _expected_rows()) == "BRIDGE_0001_0007"
    # Shadowing a catalog used by the evidence does not change it.
    _exec(dsn, "CREATE VIEW public.pg_indexes AS SELECT * FROM pg_catalog.pg_indexes WHERE false",
          sql.SQL("ALTER DATABASE {} SET search_path = public, pg_catalog").format(sql.Identifier(name)))
    again = _psql(dsn, "-F", "=", "-f", str(AIVEN / "verify_schema.sql"), pgoptions=None)
    assert again.splitlines()[3] == evidence.splitlines()[3]  # indexes=...
    # A forged single row is refused even when every count matches.
    forged = "\n".join(evidence.splitlines()[:7] + [_expected_rows().replace("\n", " ")])
    assert pg18_state.classify(forged, _expected_rows()) != "BRIDGE_0001_0007"
