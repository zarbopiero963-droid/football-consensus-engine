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


# ---------------------------------------------------------------- review findings (PR #169)

def _restore_tx(schema_name, expected_counts, expected_fingerprint=None):
    """Exactly the workflow's write path minus the defaultdb target guard: one transaction with
    the generated state guard, the bridge script, the count assertion and the post guard
    (end state + catalog fingerprint)."""
    fp = expected_fingerprint or _clean_fingerprint()
    return _psql(schema_name, "-v", f"expected_counts={expected_counts}", "-v", f"expected_fingerprint={fp}", "-1",
                 "-f", str(AIVEN / "defaultdb_restore_guard.sql"),
                 "-f", str(AIVEN / "aiven_migrations_bridge_0001_0008.sql"),
                 "-f", str(AIVEN / "assert_bridge_counts.sql"),
                 "-f", str(AIVEN / "defaultdb_restore_post_guard.sql"))


def _fingerprint(schema_name):
    rc, out, err = _psql(schema_name, "-f", str(AIVEN / "fingerprint_bridge_schema.sql"))
    assert rc == 0, err
    return out


_CLEAN = {}


def _clean_fingerprint():
    if "fp" not in _CLEAN:
        dsn = os.environ["TEST_DATABASE_URL"]
        name = "wp005_fp_" + uuid.uuid4().hex[:10]
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(name)))
        try:
            rc, _, err = _apply(name)
            assert rc == 0, err
            _CLEAN["fp"] = _fingerprint(name)
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(name)))
    return _CLEAN["fp"]


def _pinned_fingerprint():
    found = re.search(r"EXPECTED_FINGERPRINT: '([0-9a-f]{32})'", WORKFLOW.read_text(encoding="utf-8"))
    assert found, "workflow must pin EXPECTED_FINGERPRINT"
    return found.group(1)


def _clean_counts():
    """Counts of a clean restore on THIS server (PG16 locally, PG18 in CI)."""
    dsn = os.environ["TEST_DATABASE_URL"]
    name = "wp005_ref_" + uuid.uuid4().hex[:10]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(name)))
    try:
        rc, _, err = _apply(name)
        assert rc == 0, err
        return _counts(name)[0]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(name)))


@pytest.mark.parametrize("drift", [
    "ALTER TABLE sxbet_rate_cooldowns ADD COLUMN evil text",
    "CREATE RULE r AS ON INSERT TO sxbet_rate_cooldowns DO INSTEAD NOTHING",  # review P2 repro
    "ALTER TABLE api_football_quota_counters ALTER COLUMN used DROP NOT NULL",
    "ALTER TABLE api_football_quota_attempts ALTER COLUMN created_at SET DEFAULT now()",
    "ALTER TABLE oauth_claude_dcr_clients ENABLE ROW LEVEL SECURITY",
    "CREATE POLICY p ON oauth_claude_dcr_clients USING (true)",
    "CREATE TRIGGER t BEFORE UPDATE ON api_football_quota_leases FOR EACH ROW "
    "EXECUTE FUNCTION suppress_redundant_updates_trigger()",
    "ALTER TABLE sxbet_rate_attempts DROP CONSTRAINT sxbet_rate_attempts_bucket_key_check",
    "CREATE INDEX idx_extra ON api_football_quota_leases(expires_at)",
])
def test_runtime_table_with_drifted_ddl_is_refused(schema, drift):
    _runtime_tables(schema)
    assert _state(schema) == "RUNTIME_ONLY"
    rc, _, err = _psql(schema, "-c", drift)
    assert rc == 0, err
    assert _state(schema) in ("REFUSE_RUNTIME_TABLE_DRIFT", "REFUSE_UNTRACKED_OBJECTS")
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc != 0 and "FCB_RESTORE_STATE_REFUSED" in err
    rc, out, _ = _psql(schema, "-c", "SELECT to_regclass('schema_migrations') IS NULL")
    assert out == "t"  # nothing was written


def test_a_runtime_table_missing_an_index_is_still_accepted_and_completed(schema):
    _runtime_tables(schema)
    rc, _, err = _psql(schema, "-c", "DROP INDEX idx_api_football_lease_expires")
    assert rc == 0, err
    assert _state(schema) == "RUNTIME_ONLY"  # indexes may be a subset: 0008 creates the missing one
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc == 0, err
    assert _state(schema) == "BRIDGE_0001_0008"


def test_duplicate_tracking_rows_are_refused(schema):
    rc, _, err = _psql(schema, "-c", "CREATE TABLE schema_migrations (version int, name text, sha256 text, "
                                     "applied_at timestamptz DEFAULT now())")
    assert rc == 0, err
    for _ in range(7):
        rc, _, err = _psql(schema, "-c", "INSERT INTO schema_migrations(version,name,sha256) VALUES "
                                         "(1,'0001_baseline.sql','a6cd3bda6f1bfb8f029d4039b2c50d0ed8d444bc12470b28a382faac068f7287')")
        assert rc == 0, err
    assert _state(schema) == "REFUSE_PARTIAL_OR_DRIFT"


def test_in_transaction_guard_catches_a_state_change_after_the_precheck(schema):
    assert _state(schema) == "EMPTY"  # the workflow's precheck passed ...
    rc, _, err = _psql(schema, "-c", "CREATE TABLE stranger (id int)")  # ... then the DB changed
    assert rc == 0, err
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc != 0 and "FCB_RESTORE_STATE_REFUSED REFUSE_UNTRACKED_OBJECTS" in err
    rc, out, _ = _psql(schema, "-c", "SELECT to_regclass('schema_migrations') IS NULL")
    assert out == "t"


def test_count_mismatch_aborts_before_commit(schema):
    rc, _, err = _restore_tx(schema, "tables=0 functions=0 triggers=0 indexes=0 constraints_information_schema=0 "
                                     "constraints_pg_constraint=0 schema_migrations_rows=0")
    assert rc != 0 and "FCB_RESTORE_COUNTS_MISMATCH" in err
    assert _state(schema) == "EMPTY"  # review P3: nothing committed on a count mismatch


def test_full_transaction_path_succeeds_twice_with_the_right_counts(schema):
    expected = _clean_counts()
    if _major() == 18:
        assert expected == _pinned_counts()
    for _ in range(2):
        rc, _, err = _restore_tx(schema, expected)
        assert rc == 0, err
    assert _state(schema) == "BRIDGE_0001_0008"


# ---------------------------------------------------------------- re-review findings (PR #169)

def test_drifted_runtime_table_on_a_0001_0007_database_is_refused(schema):
    """Re-review P1: the signature check used to be skipped once schema_migrations existed."""
    rc, _, err = _apply(schema, "aiven_migrations_0001_0007.sql")
    assert rc == 0, err
    rc, _, err = _psql(schema, "-c", "CREATE UNLOGGED TABLE api_football_quota_counters (api_key_hash TEXT NOT NULL, "
                                     "window_kind TEXT NOT NULL, bucket BIGINT NOT NULL, used BIGINT NOT NULL, "
                                     "backdoor TEXT, PRIMARY KEY (api_key_hash, window_kind, bucket))")
    assert rc == 0, err
    assert _state(schema) == "REFUSE_RUNTIME_TABLE_DRIFT"
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc != 0 and "REFUSE_RUNTIME_TABLE_DRIFT" in err


def test_clean_runtime_table_on_a_0001_0007_database_is_accepted(schema):
    rc, _, err = _apply(schema, "aiven_migrations_0001_0007.sql")
    assert rc == 0, err
    _runtime_tables(schema)
    assert _state(schema) == "BRIDGE_0001_0007"
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc == 0, err
    assert _state(schema) == "BRIDGE_0001_0008"


@pytest.mark.parametrize("variant", [
    "ALTER TABLE api_football_quota_counters SET UNLOGGED",
    "ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash TYPE text COLLATE \"C\"",
    "ALTER TABLE api_football_quota_counters SET (fillfactor = 10)",
    "ALTER TABLE api_football_quota_counters SET (autovacuum_enabled = false)",
    "ALTER TABLE api_football_quota_counters REPLICA IDENTITY FULL",
    "GRANT ALL ON api_football_quota_counters TO PUBLIC",
    "GRANT SELECT (api_key_hash) ON api_football_quota_counters TO PUBLIC",
    "ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash SET (n_distinct = 5)",
])
def test_signature_refuses_storage_privilege_and_collation_variants(schema, variant):
    """Re-review P2: these variants used to pass as RUNTIME_ONLY."""
    _runtime_tables(schema)
    rc, _, err = _psql(schema, "-c", variant)
    assert rc == 0, err
    assert _state(schema) == "REFUSE_RUNTIME_TABLE_DRIFT"


def test_table_owned_by_another_role_is_refused(schema):
    _runtime_tables(schema)
    role = "wp005_evil_" + uuid.uuid4().hex[:6]
    rc, _, err = _psql(schema, "-c", f"CREATE ROLE {role}")
    assert rc == 0, err
    try:
        rc, _, err = _psql(schema, "-c", f"ALTER TABLE oauth_claude_dcr_clients OWNER TO {role}")
        assert rc == 0, err
        assert _state(schema) == "REFUSE_RUNTIME_TABLE_DRIFT"
    finally:
        _psql(schema, "-c", f"ALTER TABLE oauth_claude_dcr_clients OWNER TO CURRENT_USER")
        _psql(schema, "-c", f"DROP ROLE {role}")


def test_search_path_that_puts_the_target_schema_before_pg_catalog_is_refused(schema):
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema},pg_catalog"}
    out = subprocess.run(["psql", os.environ["TEST_DATABASE_URL"], "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                          "-f", str(AIVEN / "defaultdb_precheck.sql")], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "REFUSE_UNSAFE_SEARCH_PATH"


def _workflow_guard():
    import yaml
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    restore = next(s for s in wf["jobs"]["restore"]["steps"] if "Restore bridge" in s.get("name", ""))["run"]
    line = next(l.strip() for l in restore.splitlines() if l.strip().startswith('GUARD="DO'))
    return line[len('GUARD="'):-1].replace("\\$", "$")


def test_the_workflow_target_guard_itself(schema):
    """Runs the exact $GUARD text from the workflow (re-review: tests rebuilt it instead)."""
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        db = conn.execute("SELECT current_database()").fetchone()[0]
    guard = _workflow_guard().replace("$EXPECTED_DB", db)
    # In the shared test DB other schemas exist and the connection is not TLS: must refuse.
    env = {**os.environ, "PGOPTIONS": "-c search_path=public"}
    out = subprocess.run(["psql", os.environ["TEST_DATABASE_URL"], "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", guard],
                         env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "FCB_TARGET_GUARD_FAILED" in out.stderr
    # A wrong database name is refused too.
    out = subprocess.run(["psql", os.environ["TEST_DATABASE_URL"], "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
                          _workflow_guard().replace("$EXPECTED_DB", "defaultdb_not_this_one")],
                         env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "FCB_TARGET_GUARD_FAILED" in out.stderr


# ---------------------------------------------------------------- round-3 findings (PR #169)

def test_every_route_ends_in_the_same_catalog_fingerprint(schema):
    """Empty, runtime-only and 0001-0007 routes must produce one identical end state."""
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc == 0, err
    assert _fingerprint(schema) == _clean_fingerprint()
    if _major() == 18:
        fp = _clean_fingerprint()
        assert fp == _pinned_fingerprint(), f"PG18_FINGERPRINT={fp}"


def test_runtime_and_0007_routes_end_in_the_clean_fingerprint(schema):
    rc, _, err = _apply(schema, "aiven_migrations_0001_0007.sql")
    assert rc == 0, err
    _runtime_tables(schema)
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc == 0, err
    assert _fingerprint(schema) == _clean_fingerprint()


@pytest.mark.parametrize("tamper", [
    # a no-op immutability function: same name, same count, different body
    "CREATE OR REPLACE FUNCTION canonical_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS "
    "$$ BEGIN RETURN NEW; END $$",
    "ALTER TABLE canonical_entities DISABLE TRIGGER canonical_entities_immutable",
    "ALTER TABLE canonical_entities ADD COLUMN backdoor text",
])
def test_tampered_0001_0007_objects_abort_the_restore_before_commit(schema, tamper):
    """Round-3 P2: tracked states were certified on checksums and counts only."""
    rc, _, err = _apply(schema, "aiven_migrations_0001_0007.sql")
    assert rc == 0, err
    rc, _, err = _psql(schema, "-c", tamper)
    assert rc == 0, err
    assert _state(schema) == "BRIDGE_0001_0007"  # history alone cannot see it ...
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc != 0 and ("FCB_RESTORE_FINGERPRINT_MISMATCH" in err or "FCB_RESTORE_COUNTS_MISMATCH" in err)
    assert _state(schema) == "BRIDGE_0001_0007"  # ... but nothing was committed


def test_public_operators_and_search_path_settings_are_refused(schema):
    """Round-3 P1: shadowing operators in the target schema."""
    rc, _, err = _psql(schema, "-c", f"CREATE OPERATOR {schema}.<> (leftarg = name, rightarg = name, "
                                     "function = pg_catalog.namelt)")
    assert rc == 0, err
    assert _state(schema) == "REFUSE_UNTRACKED_OBJECTS"


def test_role_search_path_setting_is_refused(schema):
    rc, _, err = _psql(schema, "-c", f"ALTER ROLE CURRENT_USER SET search_path = {schema}, pg_catalog")
    assert rc == 0, err
    try:
        assert _state(schema) == "REFUSE_UNSAFE_SEARCH_PATH"
    finally:
        _psql(schema, "-c", "ALTER ROLE CURRENT_USER RESET search_path")


def test_default_privileges_are_refused_before_any_write(schema):
    """Round-3 P2-A: the restore used to commit 33 tables granted to another role first."""
    role = "wp005_dp_" + uuid.uuid4().hex[:6]
    rc, _, err = _psql(schema, "-c", f"CREATE ROLE {role}")
    assert rc == 0, err
    try:
        rc, _, err = _psql(schema, "-c", f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT ALL ON TABLES TO {role}")
        assert rc == 0, err
        assert _state(schema) == "REFUSE_DEFAULT_PRIVILEGES"
        rc, _, err = _restore_tx(schema, _clean_counts())
        assert rc != 0
        rc, out, _ = _psql(schema, "-c", "SELECT to_regclass('schema_migrations') IS NULL")
        assert out == "t"
    finally:
        _psql(schema, "-c", f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM {role}")
        _psql(schema, "-c", f"DROP ROLE {role}")


def test_enabled_event_triggers_are_refused(schema):
    name = "wp005_evt_" + uuid.uuid4().hex[:6]
    rc, _, err = _psql(schema, "-c", f"CREATE FUNCTION {name}() RETURNS event_trigger LANGUAGE plpgsql "
                                     f"AS $$ BEGIN END $$; CREATE EVENT TRIGGER {name} ON ddl_command_end "
                                     f"EXECUTE FUNCTION {name}()")
    assert rc == 0, err
    try:
        assert _state(schema) == "REFUSE_EVENT_TRIGGERS"
    finally:
        _psql(schema, "-c", f"DROP EVENT TRIGGER {name}; DROP FUNCTION {name}()")


def test_lookalike_collation_named_default_is_refused(schema):
    """Round-3 P3: collation was matched by name only."""
    _runtime_tables(schema)
    rc, _, err = _psql(schema, "-c", f"CREATE COLLATION {schema}.\"default\" FROM \"C\"; "
                                     f"ALTER TABLE oauth_claude_dcr_clients ALTER COLUMN redirect_uri TYPE text "
                                     f"COLLATE {schema}.\"default\"")
    assert rc == 0, err
    assert _state(schema) in ("REFUSE_UNTRACKED_OBJECTS", "REFUSE_RUNTIME_TABLE_DRIFT")


def test_invalid_runtime_index_is_refused(schema):
    _runtime_tables(schema)
    rc, _, err = _psql(schema, "-c", "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
                                     "'idx_api_football_lease_expires'::regclass")
    assert rc == 0, err
    assert _state(schema) == "REFUSE_RUNTIME_TABLE_DRIFT"
