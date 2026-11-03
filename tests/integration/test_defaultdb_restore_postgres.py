"""FCB-WP-005 on real PostgreSQL: the exact files the owner-gated defaultdb restore runs.

Runs psql (as the workflow does) with -1 / ON_ERROR_STOP against a throwaway schema per test.
On PostgreSQL 18 (CI job postgres-18) it also checks the object counts against the pinned
values in the workflow, and the 0001-0007 counts against the ones observed on Aiven (run 14).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.sql import SQL, Identifier  # noqa: E402

from app import migrations  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AIVEN = ROOT / "sql" / "aiven"
WORKFLOW = ROOT / ".github" / "workflows" / "fcb-aiven-defaultdb-restore.yml"
# Observed on Aiven fcb_pg18_test (PostgreSQL 18.6), run 14, after 0001-0007.
AIVEN_0001_0007 = ("tables=29 functions=4 triggers=9 indexes=54 constraints_information_schema=307 "
                   "constraints_pg_constraint=61 schema_migrations_rows=7")

pytestmark = [
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                       reason="requires dedicated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(shutil.which("psql") is None and os.environ.get("FCB_REQUIRE_PSQL") != "1",
                       reason="psql client not installed"),
]


@pytest.fixture
def schema():
    dsn = os.environ["TEST_DATABASE_URL"]
    name = "wp005_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(name)))
    yield name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(name)))


def _psql(schema_name, *args):
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema_name}"}
    out = subprocess.run(["psql", os.environ["TEST_DATABASE_URL"], "-X", "-q", "-A", "-t",
                          "-v", "ON_ERROR_STOP=1", *args], env=env, capture_output=True, text=True, timeout=120)
    return out.returncode, out.stdout.strip(), out.stderr


def _state(schema_name):
    rc, out, err = _psql(schema_name, "-f", str(AIVEN / "defaultdb_precheck.sql"))
    assert rc == 0, err
    if out == "TRACKED":
        rc, out, err = _psql(schema_name, "-f", str(AIVEN / "defaultdb_precheck_tracked.sql"))
        assert rc == 0, err
    return out


def _apply(schema_name, script="aiven_migrations_bridge_0001_0008.sql"):
    return _psql(schema_name, "-1", "-f", str(AIVEN / script))


def _counts(schema_name, verify="verify_bridge_schema.sql"):
    rc, out, err = _psql(schema_name, "-F", "=", "-f", str(AIVEN / verify))
    assert rc == 0, err
    lines = out.splitlines()
    return " ".join(lines[:7]), lines[7:]


def _major():
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        return conn.info.server_version // 10000


def _runtime_tables(schema_name):
    from bridge.api_football.quota import _SCHEMA as quota_schema
    from bridge.sxbet.ratelimit import _SCHEMA as sxbet_schema
    dsn = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(dsn, options=f"-c search_path={schema_name}") as conn:
        for statement in (*sxbet_schema, *quota_schema):
            conn.execute(statement)
        conn.execute("""CREATE TABLE IF NOT EXISTS oauth_claude_dcr_clients (
               client_id_hash TEXT PRIMARY KEY,
               redirect_uri TEXT NOT NULL,
               created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
               expires_at TIMESTAMPTZ NOT NULL
            )""")
        conn.execute("INSERT INTO oauth_claude_dcr_clients (client_id_hash, redirect_uri, expires_at) "
                     "VALUES ('h', 'https://claude.ai/cb', NOW() + INTERVAL '1 day')")


def _pinned_counts():
    text = WORKFLOW.read_text(encoding="utf-8")
    found = re.search(r"EXPECTED_COUNTS: '([^']+)'", text)
    assert found, "workflow must pin EXPECTED_COUNTS"
    return found.group(1)


def test_empty_schema_restores_to_bridge_0008_and_second_run_is_a_noop(schema):
    assert _state(schema) == "EMPTY"
    rc, _, err = _apply(schema)
    assert rc == 0, err
    first = _counts(schema)
    assert _state(schema) == "BRIDGE_0001_0008"
    rc, _, err = _apply(schema)
    assert rc == 0, err
    assert _counts(schema) == first
    expected_rows = [f"{m.version}={m.name}={m.checksum}" for m in migrations.load_migrations()]
    assert first[1] == expected_rows  # recorded names + checksums == repo files
    for table in ("signer_ledger_anchors", "signer_ledger_writer_leases", "signer_schema_migrations"):
        rc, out, _ = _psql(schema, "-c", f"SELECT to_regclass('{table}') IS NULL")
        assert out == "t", table  # no financial schema in the bridge restore
    if _major() == 18:
        assert first[0] == _pinned_counts()


def test_runtime_created_tables_are_accepted_and_kept(schema):
    _runtime_tables(schema)
    assert _state(schema) == "RUNTIME_ONLY"
    rc, _, err = _apply(schema)
    assert rc == 0, err
    assert _state(schema) == "BRIDGE_0001_0008"
    rc, out, _ = _psql(schema, "-c", "SELECT count(*) FROM oauth_claude_dcr_clients")
    assert out == "1"  # runtime data preserved
    if _major() == 18:
        assert _counts(schema)[0] == _pinned_counts()


def test_0001_0007_database_upgrades_and_matches_aiven_evidence(schema):
    rc, _, err = _apply(schema, "aiven_migrations_0001_0007.sql")
    assert rc == 0, err
    if _major() == 18:
        assert _counts(schema)[0] == AIVEN_0001_0007  # harness reproduces the real Aiven run
    assert _state(schema) == "BRIDGE_0001_0007"
    rc, _, err = _apply(schema)
    assert rc == 0, err
    assert _state(schema) == "BRIDGE_0001_0008"


@pytest.mark.parametrize("setup,expected", [
    ("CREATE TABLE stranger (id int)", "REFUSE_UNTRACKED_OBJECTS"),
    ("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'SELECT 1'", "REFUSE_UNTRACKED_OBJECTS"),
    ("CREATE TYPE mood AS ENUM ('a')", "REFUSE_UNTRACKED_OBJECTS"),
    ("CREATE TABLE signer_schema_migrations (version int)", "REFUSE_SIGNER_SERIES_PRESENT"),
])
def test_unexpected_objects_refuse(schema, setup, expected):
    rc, _, err = _psql(schema, "-c", setup)
    assert rc == 0, err
    assert _state(schema) == expected


def test_drifted_or_partial_or_pre_split_history_refuses(schema):
    rc, _, err = _apply(schema)
    assert rc == 0, err
    rc, _, err = _psql(schema, "-c", "UPDATE schema_migrations SET sha256=repeat('0',64) WHERE version=2")
    assert rc == 0, err
    assert _state(schema) == "REFUSE_PARTIAL_OR_DRIFT"
    rc, _, err = _psql(schema, "-c", "DELETE FROM schema_migrations WHERE version >= 2")
    assert rc == 0, err
    assert _state(schema) == "REFUSE_PARTIAL_OR_DRIFT"
    rc, _, err = _psql(schema, "-c", "INSERT INTO schema_migrations(version,name,sha256) VALUES "
                                     "(8,'0008_signer_ledger_anchor.sql','x')")
    assert rc == 0, err
    assert _state(schema) == "REFUSE_PRE_SPLIT_SIGNER_0008"


def test_a_failing_restore_leaves_nothing_behind(schema, tmp_path):
    broken = tmp_path / "broken.sql"
    broken.write_text((AIVEN / "aiven_migrations_bridge_0001_0008.sql").read_text() + "SELECT 1/0;\n")
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema}"}
    out = subprocess.run(["psql", os.environ["TEST_DATABASE_URL"], "-X", "-q", "-v", "ON_ERROR_STOP=1", "-1",
                          "-f", str(broken)], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode != 0
    assert _state(schema) == "EMPTY"  # single transaction: rolled back completely
