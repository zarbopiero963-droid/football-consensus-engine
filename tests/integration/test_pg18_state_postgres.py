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
    yield name, psycopg.conninfo.make_conninfo(**params)
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
])
def test_any_user_object_or_schema_is_never_empty(db, ddl, expected):
    _exec(db[1], *ddl)
    assert _occupancy(db[1]) == expected


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
