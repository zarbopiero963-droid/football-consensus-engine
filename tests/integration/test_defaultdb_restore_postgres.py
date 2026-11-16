"""FCB-WP-005 on real PostgreSQL: the exact files the owner-gated defaultdb restore runs.

Runs psql (as the workflow does) with -1 / ON_ERROR_STOP against the `public` schema of a throwaway
DATABASE per test (audit of 2026-11-09: the files now read `public` explicitly, with every catalog
name schema-qualified, so a per-test schema in a shared database is no longer a faithful harness).
On PostgreSQL 18 (CI job postgres-18) it also checks the object counts against the pinned
values in the workflow, and the 0001-0007 counts against the ones observed on Aiven (run 14).
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.sql import SQL, Identifier, Literal  # noqa: E402

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


class Target(str):
    """The `public` schema of one isolated database: str() is "public" (the schema name used in
    SQL text), .dsn connects to that database (as the administrator or as its owner)."""

    def __new__(cls, dsn, dbname):
        obj = super().__new__(cls, "public")
        obj.dsn, obj.dbname = dsn, dbname
        return obj


def _dsn_for(dbname, **override):
    params = psycopg.conninfo.conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
    params["dbname"] = dbname
    params.update(override)
    return psycopg.conninfo.make_conninfo(**params)


def _create_db(prefix):
    name = prefix + uuid.uuid4().hex[:12]
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(SQL("CREATE DATABASE {}").format(Identifier(name)))
    return Target(_dsn_for(name), name)


def _drop_db(target):
    with psycopg.connect(target.dsn, autocommit=True) as conn:  # these block DROP DATABASE
        for (sub,) in conn.execute("SELECT s.subname FROM pg_catalog.pg_subscription s JOIN pg_catalog.pg_database d "
                                   "ON d.oid = s.subdbid WHERE d.datname = pg_catalog.current_database()").fetchall():
            conn.execute(SQL("DROP SUBSCRIPTION {}").format(Identifier(sub)))
        for (gid,) in conn.execute("SELECT x.gid FROM pg_catalog.pg_prepared_xacts x "
                                   "WHERE x.database = pg_catalog.current_database()").fetchall():
            conn.execute(SQL("ROLLBACK PREPARED {}").format(Literal(gid)))
        for (slot,) in conn.execute("SELECT x.slot_name FROM pg_catalog.pg_replication_slots x "
                                    "WHERE x.database = pg_catalog.current_database()").fetchall():
            conn.execute("SELECT pg_catalog.pg_drop_replication_slot(%s)", (slot,))
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(SQL("DROP DATABASE {} WITH (FORCE)").format(Identifier(target.dbname)))


@pytest.fixture
def schema():
    """An isolated database; the restore files work on its `public` schema."""
    target = _create_db("wp005_")
    yield target
    _drop_db(target)


def _exec(dsn, *statements):
    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


@contextlib.contextmanager
def _owner(target):
    """A non-superuser LOGIN role that owns the database, like Aiven's avnadmin. It is created
    WITH a random password (CI authenticates with passwords); yields a Target connecting as it."""
    role = "wp005_owner_" + uuid.uuid4().hex[:8]
    password = uuid.uuid4().hex
    _exec(target.dsn, SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(Identifier(role), Literal(password)),
          SQL("ALTER DATABASE {} OWNER TO {}").format(Identifier(target.dbname), Identifier(role)))
    owner = Target(_dsn_for(target.dbname, user=role, password=password), target.dbname)
    owner.role = role
    try:
        yield owner
    finally:
        _exec(target.dsn, SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(Identifier(target.dbname)),
              SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(Identifier(role)),
              SQL("DROP OWNED BY {}").format(Identifier(role)),
              SQL("DROP ROLE {}").format(Identifier(role)))


@pytest.fixture
def owned(schema):
    """`schema` plus a session of its non-superuser owner (Aiven's avnadmin shape)."""
    with _owner(schema) as owner:
        yield schema, owner


def _psql(target, *args, pgoptions="-c search_path=public"):
    env = {**os.environ, "PGOPTIONS": pgoptions}
    out = subprocess.run(["psql", target.dsn, "-X", "-q", "-A", "-t",
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
    with psycopg.connect(schema_name.dsn, options="-c search_path=public") as conn:
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
    # The restore is exactly 0001-0008 (0009+ has its own owner-gated workflow).
    expected_rows = [f"{m.version}={m.name}={m.checksum}" for m in migrations.load_migrations()[:8]]
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
    rc, _, _ = _psql(schema, "-1", "-f", str(broken))
    assert rc != 0
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


def _clean_reference():
    """Fingerprint and counts of a clean restore on THIS server (PG16 locally, PG18 in CI)."""
    if "fp" not in _CLEAN:
        target = _create_db("wp005_ref_")
        try:
            rc, _, err = _apply(target)
            assert rc == 0, err
            _CLEAN["fp"] = _fingerprint(target)
            _CLEAN["counts"] = _counts(target)[0]
        finally:
            _drop_db(target)
    return _CLEAN


def _clean_fingerprint():
    return _clean_reference()["fp"]


def _pinned_fingerprint():
    found = re.search(r"EXPECTED_FINGERPRINT: '([0-9a-f]{32})'", WORKFLOW.read_text(encoding="utf-8"))
    assert found, "workflow must pin EXPECTED_FINGERPRINT"
    return found.group(1)


def _clean_counts():
    """Counts of a clean restore on THIS server (PG16 locally, PG18 in CI)."""
    return _clean_reference()["counts"]


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


@pytest.mark.parametrize("path", ["pg_catalog", "wp005_absent"])
def test_a_current_schema_other_than_public_is_refused(schema, path):
    """The files read `public` explicitly; a session whose current schema is not public is refused."""
    rc, out, err = _psql(schema, "-f", str(AIVEN / "defaultdb_precheck.sql"), pgoptions=f"-c search_path={path}")
    assert rc == 0 and out == "REFUSE_UNSAFE_SEARCH_PATH", err


def test_search_path_that_puts_the_target_schema_before_pg_catalog_is_refused(schema):
    rc, out, err = _psql(schema, "-f", str(AIVEN / "defaultdb_precheck.sql"), pgoptions=f"-c search_path={schema},pg_catalog")
    assert rc == 0, err
    assert out == "REFUSE_UNSAFE_SEARCH_PATH"


def _workflow_guard():
    import yaml
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    restore = next(s for s in wf["jobs"]["restore"]["steps"] if "Restore bridge" in s.get("name", ""))["run"]
    line = next(l.strip() for l in restore.splitlines() if l.strip().startswith('GUARD="DO'))
    return line[len('GUARD="'):-1].replace("\\$", "$").replace('\\"', '"')  # bash double-quote escapes (R1 embeds "char")


def test_the_workflow_target_guard_itself(schema):
    """Runs the exact $GUARD text from the workflow (re-review: tests rebuilt it instead)."""
    guard = _workflow_guard().replace("$EXPECTED_DB", schema.dbname)
    # The connection is not TLS and (locally) not PostgreSQL 18: must refuse.
    proc = _guard_psql(schema, guard)
    assert proc.returncode != 0 and "FCB_TARGET_GUARD_FAILED" in proc.stderr
    # A wrong database name is refused too.
    proc = _guard_psql(schema, _workflow_guard().replace("$EXPECTED_DB", "defaultdb_not_this_one"))
    assert proc.returncode != 0 and "FCB_TARGET_GUARD_FAILED" in proc.stderr


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


@pytest.mark.parametrize("tamper,state", [
    # a no-op immutability function: same name, same count, different body. Audit F2 (declared
    # behaviour change): the pinned body md5 now refuses it already in the precheck.
    ("CREATE OR REPLACE FUNCTION canonical_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS "
     "$$ BEGIN RETURN NEW; END $$", "REFUSE_UNTRACKED_OBJECTS"),
    # likewise: a pinned bridge trigger must be enabled
    ("ALTER TABLE canonical_entities DISABLE TRIGGER canonical_entities_immutable", "REFUSE_UNTRACKED_OBJECTS"),
    ("ALTER TABLE canonical_entities ADD COLUMN backdoor text", "BRIDGE_0001_0007"),
])
def test_tampered_0001_0007_objects_abort_the_restore_before_commit(schema, tamper, state):
    """Round-3 P2: tracked states were certified on checksums and counts only."""
    rc, _, err = _apply(schema, "aiven_migrations_0001_0007.sql")
    assert rc == 0, err
    rc, _, err = _psql(schema, "-c", tamper)
    assert rc == 0, err
    assert _state(schema) == state  # history alone cannot see a column; the precheck sees the rest
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc != 0 and ("FCB_RESTORE_FINGERPRINT_MISMATCH" in err or "FCB_RESTORE_COUNTS_MISMATCH" in err
                        or f"FCB_RESTORE_STATE_REFUSED {state}" in err)
    assert _state(schema) == state  # nothing was committed


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


# ---------------------------------------------------------------- round-4 finding (PR #169)

@pytest.mark.parametrize("obj", [
    "CREATE AGGREGATE count(integer) (sfunc = int4larger, stype = int4)",
    "CREATE PROCEDURE p() LANGUAGE sql AS 'SELECT 1'",
])
def test_aggregates_and_procedures_are_refused_in_every_state(schema, obj):
    """Round-4 P2: an aggregate in the target schema shadowed count() and passed tracked states."""
    rc, _, err = _apply(schema)
    assert rc == 0, err
    rc, _, err = _psql(schema, "-c", obj)
    assert rc == 0, err
    assert _state(schema) == "REFUSE_UNTRACKED_OBJECTS"
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc != 0 and "FCB_RESTORE_STATE_REFUSED" in err


def test_publications_are_refused(schema):
    _runtime_tables(schema)
    name = "wp005_pub_" + uuid.uuid4().hex[:6]
    rc, _, err = _psql(schema, "-c", f"CREATE PUBLICATION {name} FOR TABLE oauth_claude_dcr_clients")
    assert rc == 0, err
    try:
        assert _state(schema) == "REFUSE_UNTRACKED_OBJECTS"
    finally:
        _psql(schema, "-c", f"DROP PUBLICATION {name}")


# ---------------------------------------------------------------- audit of 2026-11-09 (F1-F8)
# Repros A-H of the defaultdb restore audit, each on its own database. The write path is the
# workflow's: its exact $GUARD text (minus the PostgreSQL 18 and TLS predicates, which a local
# server cannot meet), the generated state guard, the bridge script, the count assertion and the
# generated post guard, in ONE psql -1 transaction.

PLANTED = "FCB_PLANTED_CODE_RAN"
_TLS_PG18 = (" OR pg_catalog.current_setting('server_version_num')::pg_catalog.int4 OPERATOR(pg_catalog.<) 180000"
             " OR pg_catalog.current_setting('server_version_num')::pg_catalog.int4 OPERATOR(pg_catalog.>) 189999"
             " OR NOT COALESCE((SELECT s.ssl FROM pg_catalog.pg_stat_ssl s WHERE s.pid OPERATOR(pg_catalog.=) "
             "pg_catalog.pg_backend_pid()), false)")


def _guard_psql(target, guard, pgoptions="-c search_path=public"):
    env = {**os.environ, "PGOPTIONS": pgoptions}
    return subprocess.run(["psql", target.dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", guard],
                          env=env, capture_output=True, text=True, timeout=60)


def _local_guard(target):
    """The workflow's exact $GUARD without the PG18/TLS predicates (asserted present)."""
    guard = _workflow_guard()
    assert guard.count(_TLS_PG18) == 1
    return guard.replace(_TLS_PG18, "").replace("$EXPECTED_DB", target.dbname)


def _workflow_tx(target, before_script=(), after_script=(), after_counts=(), guard=True):
    args = ["-v", f"expected_counts={_clean_counts()}", "-v", f"expected_fingerprint={_clean_fingerprint()}", "-1"]
    if guard:
        args += ["-c", _local_guard(target)]
    args += ["-f", str(AIVEN / "defaultdb_restore_guard.sql")]
    for c in before_script:
        args += ["-c", c]
    args += ["-f", str(AIVEN / "aiven_migrations_bridge_0001_0008.sql")]
    for c in after_script:
        args += ["-c", c]
    args += ["-f", str(AIVEN / "assert_bridge_counts.sql")]
    for c in after_counts:
        args += ["-c", c]
    args += ["-f", str(AIVEN / "defaultdb_restore_post_guard.sql")]
    return _psql(target, *args)


def _nothing_written(target):
    rc, out, err = _psql(target, "-c", "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c "
                                       "WHERE c.relname = 'schema_migrations'")
    assert rc == 0, err
    return out == "0"


def _migration_rows(target):
    rc, out, err = _psql(target, "-c", "SELECT pg_catalog.string_agg(m.version::text || m.applied_at::text, ',' "
                                       "ORDER BY m.version) FROM public.schema_migrations m")
    assert rc == 0, err
    return out


def test_owner_restores_through_the_workflow_path_and_a_second_run_is_a_noop(owned):
    """Aiven's avnadmin is no superuser: EMPTY -> BRIDGE_0001_0008, then the production no-op."""
    _, owner = owned
    assert _state(owner) == "EMPTY"
    rc, _, err = _workflow_tx(owner)
    assert rc == 0, err
    assert _state(owner) == "BRIDGE_0001_0008"
    rows = _migration_rows(owner)
    rc, _, err = _workflow_tx(owner)  # production today: recognized, nothing re-applied
    assert rc == 0, err
    assert _migration_rows(owner) == rows
    assert _fingerprint(owner) == _clean_fingerprint()


def test_owner_upgrades_a_0001_0007_database_through_the_workflow_path(owned):
    _, owner = owned
    rc, _, err = _apply(owner, "aiven_migrations_0001_0007.sql")
    assert rc == 0, err
    _runtime_tables(owner)
    assert _state(owner) == "BRIDGE_0001_0007"
    rc, _, err = _workflow_tx(owner)
    assert rc == 0, err
    assert _state(owner) == "BRIDGE_0001_0008"


# --- F1 (repro A): LIKE wildcards and the name-only `_avnadmin_monitor` exclusion

@contextlib.contextmanager
def _monitor_schema(target, owner_name, *extra):
    """`_avnadmin_monitor` owned by `owner_name` (role created if missing, dropped after)."""
    created = False
    with psycopg.connect(target.dsn, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (owner_name,)).fetchone() is None:
            conn.execute(SQL("CREATE ROLE {}").format(Identifier(owner_name)))
            created = True
        conn.execute(SQL("CREATE SCHEMA _avnadmin_monitor AUTHORIZATION {}").format(Identifier(owner_name)))
        conn.execute("CREATE FUNCTION _avnadmin_monitor.probe() RETURNS int LANGUAGE sql AS 'select 1'")
        for q in extra:
            conn.execute(q)
    try:
        yield
    finally:
        with psycopg.connect(target.dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS _avnadmin_monitor CASCADE")
            if created:
                conn.execute(SQL("DROP ROLE {}").format(Identifier(owner_name)))


@pytest.mark.parametrize("name", ["pgxtemp", "pgxtoastxtemp_1", "pg1temp_7"])
def test_lookalike_temp_schemas_are_refused_everywhere(owned, name):
    """LIKE 'pg_temp%' / 'pg_toast_temp%' treat '_' as a wildcard: these used to be invisible."""
    db, owner = owned
    _exec(db.dsn, f"CREATE SCHEMA {name} AUTHORIZATION {owner.role}", f"CREATE TABLE {name}.hidden(id int)")
    assert _state(owner) == "REFUSE_UNEXPECTED_SCHEMAS"
    proc = _guard_psql(owner, _local_guard(owner))
    assert proc.returncode != 0 and "FCB_TARGET_GUARD_FAILED" in proc.stderr
    rc, _, err = _workflow_tx(owner, guard=False)  # the generated guard alone refuses it too
    assert rc != 0 and "FCB_RESTORE_SESSION_GUARD_FAILED" in err
    assert _nothing_written(owner)


@pytest.mark.parametrize("variant", ["session_owner", "other_owner", "session_member", "session_member_noinherit",
                                     "session_admin_only", "session_create"])
def test_avnadmin_monitor_is_excluded_only_in_aivens_real_shape(owned, variant):
    db, owner = owned
    owner_name = {"session_owner": owner.role, "other_owner": "avn_helper_" + uuid.uuid4().hex[:6]}.get(
        variant, "_avnadmin_monitor")
    extra = {"session_member": [SQL("GRANT _avnadmin_monitor TO {}").format(Identifier(owner.role))],
             # membership without inherited privileges (no CREATE seen), or ADMIN only: the session
             # can still SET ROLE / re-grant itself into the owner; only pg_has_role(MEMBER) sees it
             "session_member_noinherit": [SQL("GRANT _avnadmin_monitor TO {} WITH INHERIT FALSE, SET TRUE")
                                          .format(Identifier(owner.role))],
             "session_admin_only": [SQL("GRANT _avnadmin_monitor TO {} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")
                                    .format(Identifier(owner.role))],
             "session_create": [SQL("GRANT CREATE ON SCHEMA _avnadmin_monitor TO {}").format(Identifier(owner.role))]
             }.get(variant, [])
    with _monitor_schema(db, owner_name, *extra):
        try:
            assert _state(owner) == "REFUSE_UNEXPECTED_SCHEMAS"
            proc = _guard_psql(owner, _local_guard(owner))
            assert proc.returncode != 0 and "FCB_TARGET_GUARD_FAILED" in proc.stderr
            rc, _, err = _workflow_tx(owner)
            assert rc != 0 and _nothing_written(owner)
        finally:
            if variant.startswith("session_member") or variant == "session_admin_only":
                _exec(db.dsn, SQL("REVOKE _avnadmin_monitor FROM {}").format(Identifier(owner.role)))


def test_the_real_aiven_monitor_schema_shape_is_accepted(owned):
    """Observed on Aiven piero-1: owner is Aiven's NON-superuser role `_avnadmin_monitor`, avnadmin is
    neither a member nor has CREATE/USAGE on it. That shape must not block the restore."""
    db, owner = owned
    with _monitor_schema(db, "_avnadmin_monitor"):
        assert _state(owner) == "EMPTY"
        proc = _guard_psql(owner, _local_guard(owner))
        assert proc.returncode == 0, proc.stderr
        rc, _, err = _workflow_tx(owner)
        assert rc == 0, err
        assert _state(owner) == "BRIDGE_0001_0008"


# --- F2 / F4 (repros B, E): foreign functions, types, triggers, rules, policies and RLS in
# tracked states, refused before the write transaction AND inside it, before any of it can run

TRACKED_PLANTS = [
    # F4: an exact-match int4 overload of the advisory lock the bridge script calls unqualified
    f"CREATE FUNCTION public.pg_advisory_xact_lock(integer) RETURNS void LANGUAGE plpgsql AS "
    f"$$ BEGIN RAISE NOTICE '{PLANTED}'; END $$",
    "CREATE TYPE public.shell_only",
    "CREATE TYPE public.mood AS ENUM ('a')",
    "CREATE DOMAIN public.dom AS int",
    # a pinned bridge function on a relation where the bridge has no trigger
    "CREATE TRIGGER extra BEFORE INSERT ON public.schema_migrations FOR EACH ROW "
    "EXECUTE FUNCTION public.canonical_reject_mutation()",
    "ALTER TABLE public.canonical_entities ENABLE ALWAYS TRIGGER canonical_entities_immutable",
    "CREATE RULE r AS ON INSERT TO public.schema_migrations DO ALSO NOTIFY fcb",
    "CREATE POLICY p ON public.schema_migrations USING (true)",
    "ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.schema_migrations FORCE ROW LEVEL SECURITY",
    "CREATE TABLE public.child () INHERITS (public.schema_migrations)",
    "ALTER FUNCTION public.seal_pilot_row() SECURITY DEFINER",
    "ALTER FUNCTION public.seal_pilot_row() SET search_path = public",
    # a pinned trigger re-created with a WHEN clause, with arguments, or on a column list
    "DROP TRIGGER canonical_alias_version_insert ON public.canonical_alias_versions; "
    "CREATE TRIGGER canonical_alias_version_insert BEFORE INSERT ON public.canonical_alias_versions "
    "FOR EACH ROW WHEN (true) EXECUTE FUNCTION public.canonical_alias_revision_guard()",
    "DROP TRIGGER canonical_alias_version_insert ON public.canonical_alias_versions; "
    "CREATE TRIGGER canonical_alias_version_insert BEFORE INSERT ON public.canonical_alias_versions "
    "FOR EACH ROW EXECUTE FUNCTION public.canonical_alias_revision_guard('x')",
    "DROP TRIGGER canonical_entities_immutable ON public.canonical_entities; "
    "CREATE TRIGGER canonical_entities_immutable BEFORE DELETE OR UPDATE OF kind OR TRUNCATE "
    "ON public.canonical_entities FOR EACH STATEMENT EXECUTE FUNCTION public.canonical_reject_mutation()",
]


@pytest.mark.parametrize("start", ["aiven_migrations_0001_0007.sql", "aiven_migrations_bridge_0001_0008.sql"])
@pytest.mark.parametrize("plant", TRACKED_PLANTS)
def test_tracked_states_refuse_foreign_code_before_it_can_run(owned, start, plant):
    db, owner = owned
    rc, _, err = _apply(owner, start)
    assert rc == 0, err
    before = _state(owner)
    rows = _migration_rows(owner)
    rc, _, err = _psql(db, "-c", plant)  # planted by whoever can (a shell type needs a superuser)
    assert rc == 0, err
    assert _state(owner) == "REFUSE_UNTRACKED_OBJECTS"
    rc, _, err = _workflow_tx(owner)
    assert rc != 0 and "FCB_RESTORE_STATE_REFUSED REFUSE_UNTRACKED_OBJECTS" in err
    assert PLANTED not in err  # refused before the bridge script called it
    assert _migration_rows(owner) == rows
    assert before in ("BRIDGE_0001_0007", "BRIDGE_0001_0008")


def test_a_0008_database_must_carry_the_pinned_fingerprint_before_the_script(owned):
    """F2: a BRIDGE_0001_0008 database is refused by the state guard itself (pre-script fingerprint)."""
    _, owner = owned
    rc, _, err = _apply(owner)
    assert rc == 0, err
    rc, _, err = _psql(owner, "-c", "ALTER TABLE public.canonical_entities ADD COLUMN backdoor text")
    assert rc == 0, err
    assert _state(owner) == "BRIDGE_0001_0008"  # not visible to the structural precheck ...
    rc, _, err = _workflow_tx(owner)
    assert rc != 0 and "FCB_RESTORE_PRE_FINGERPRINT_MISMATCH" in err  # ... but to the guard


# --- F3 (repro C): code that changes the session inside the transaction

def test_repro_c_pgxtemp_policy_backdoor_is_refused_before_it_runs(owned):
    db, owner = owned
    rc, _, err = _apply(owner)
    assert rc == 0, err
    _exec(db.dsn, f"CREATE SCHEMA pgxtemp AUTHORIZATION {owner.role}")
    rc, _, err = _psql(owner, "-c", f"""
        CREATE FUNCTION pgxtemp.md5(text) RETURNS text LANGUAGE sql AS $$ SELECT '{_clean_fingerprint()}'::text $$;
        CREATE FUNCTION pgxtemp.current_schemas(boolean) RETURNS name[] LANGUAGE sql AS $$ SELECT '{{pg_catalog,public}}'::name[] $$;
        CREATE FUNCTION pgxtemp.evil() RETURNS boolean LANGUAGE plpgsql AS $$
        BEGIN RAISE NOTICE '{PLANTED}'; PERFORM pg_catalog.set_config('search_path', 'public, pgxtemp, pg_catalog', true);
        RETURN true; END $$;
        ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.schema_migrations FORCE ROW LEVEL SECURITY;
        CREATE POLICY backdoor ON public.schema_migrations USING (pgxtemp.evil());""")
    assert rc == 0, err
    rc, out, err = _psql(owner, "-f", str(AIVEN / "defaultdb_precheck.sql"))
    assert rc == 0 and out == "REFUSE_UNEXPECTED_SCHEMAS", err
    rc, _, err = _workflow_tx(owner)
    assert rc != 0 and "FCB_TARGET_GUARD_FAILED" in err and PLANTED not in err
    rc, _, err = _workflow_tx(owner, guard=False)
    assert rc != 0 and "FCB_RESTORE_SESSION_GUARD_FAILED" in err and PLANTED not in err


@pytest.mark.parametrize("where", ["after_script", "after_counts"])
@pytest.mark.parametrize("change", [
    "SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true)",
    # current_schema() and current_schemas(true)[1] unchanged: only the setting itself shows it
    "SELECT pg_catalog.set_config('search_path', 'public, wp005_absent', true)",
    "SELECT pg_catalog.set_config('search_path', 'pg_catalog, public', true)",
    "CREATE SCHEMA evil",
    "SET LOCAL ROLE {intruder}",
])
def test_session_changes_inside_the_transaction_abort_it(owned, where, change):
    """What code reached during the script could do until COMMIT: change search_path or role,
    or create a schema. Re-asserted (qualified) by the count assertion and by the post guard."""
    db, owner = owned
    intruder = "wp005_intruder_" + uuid.uuid4().hex[:6]
    _exec(db.dsn, SQL("CREATE ROLE {}").format(Identifier(intruder)),
          SQL("GRANT {} TO {}").format(Identifier(intruder), Identifier(owner.role)))
    try:
        stmt = change.format(intruder=intruder)
        rc, _, err = _workflow_tx(owner, **{where: [stmt]})
        assert rc != 0 and "FCB_RESTORE_SESSION_GUARD_FAILED" in err, err
        # the FIRST file after the change refuses it (psql prefixes the error with the file name)
        caught_by = "assert_bridge_counts.sql" if where == "after_script" and "SCHEMA" not in stmt \
            else "defaultdb_restore_post_guard.sql"
        assert f"{caught_by}:" in err.split("FCB_RESTORE_SESSION_GUARD_FAILED")[0].splitlines()[-1], err
        assert _nothing_written(owner)
    finally:
        _exec(db.dsn, SQL("DROP ROLE {}").format(Identifier(intruder)))


_SHADOWS = [
    "CREATE FUNCTION evil.boom_text(text, text) RETURNS text LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.boom_bool_name(name, name) RETURNS boolean LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.boom_bool_oid(oid, oid) RETURNS boolean LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.boom_bool_text(text, text) RETURNS boolean LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.boom_bool_char(\"char\", \"char\") RETURNS boolean LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.boom_int8(int8, int8) RETURNS int8 LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE OPERATOR evil.|| (leftarg = text, rightarg = text, function = evil.boom_text)",
    "CREATE OPERATOR evil.= (leftarg = name, rightarg = name, function = evil.boom_bool_name)",
    "CREATE OPERATOR evil.= (leftarg = oid, rightarg = oid, function = evil.boom_bool_oid)",
    "CREATE OPERATOR evil.= (leftarg = text, rightarg = text, function = evil.boom_bool_text)",
    "CREATE OPERATOR evil.= (leftarg = \"char\", rightarg = \"char\", function = evil.boom_bool_char)",
    "CREATE OPERATOR evil.<> (leftarg = name, rightarg = name, function = evil.boom_bool_name)",
    "CREATE OPERATOR evil.+ (leftarg = int8, rightarg = int8, function = evil.boom_int8)",
    "CREATE FUNCTION evil.md5(text) RETURNS text LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.replace(text, text, text) RETURNS text LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.current_schema() RETURNS name LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.current_setting(text) RETURNS text LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.format_type(oid, integer) RETURNS text LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.pg_get_indexdef(oid) RETURNS text LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE FUNCTION evil.pg_get_functiondef(oid) RETURNS text LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'FCB_SHADOW_USED'; END $$",
    "CREATE AGGREGATE evil.count(*) (sfunc = pg_catalog.int8inc, stype = int8, initcond = '0')",
    "CREATE VIEW evil.pg_class AS SELECT * FROM pg_catalog.pg_class WHERE false",
    "CREATE VIEW evil.schema_migrations AS SELECT * FROM public.schema_migrations WHERE false",
]


def test_evidence_files_ignore_shadowing_objects_under_a_hostile_search_path(schema):
    """F3: with `evil` searched BEFORE pg_catalog (what a mid-transaction set_config can do),
    every query of the fingerprint, the evidence and the tracked precheck still reads
    pg_catalog/public only; the session checks refuse before anything else runs."""
    rc, _, err = _apply(schema)
    assert rc == 0, err
    clean = (_fingerprint(schema), _counts(schema), _state(schema))
    _exec(schema.dsn, "CREATE SCHEMA evil", *_SHADOWS)
    hostile = "-c search_path=evil,pg_catalog,public"
    # Every shadow raises if it is ever executed. The fingerprint TEXT legitimately differs here:
    # pg_get_constraintdef() & co. print OPERATOR(pg_catalog.=) when `=` is shadowed in the path,
    # which is why the in-transaction files abort on a changed search_path and then pin it.
    rc, fp, err = _psql(schema, "-f", str(AIVEN / "fingerprint_bridge_schema.sql"), pgoptions=hostile)
    assert rc == 0 and re.fullmatch(r"[0-9a-f]{32}", fp) and "FCB_SHADOW_USED" not in err, err
    rc, out, err = _psql(schema, "-F", "=", "-f", str(AIVEN / "verify_bridge_schema.sql"), pgoptions=hostile)
    assert rc == 0 and out.splitlines()[:7] == clean[1][0].split(" "), err
    rc, out, err = _psql(schema, "-f", str(AIVEN / "defaultdb_precheck_tracked.sql"), pgoptions=hostile)
    assert rc == 0 and out == clean[2], err
    rc, out, err = _psql(schema, "-f", str(AIVEN / "defaultdb_precheck.sql"), pgoptions=hostile)
    assert rc == 0 and out == "REFUSE_UNSAFE_SEARCH_PATH", err
    for name in ("defaultdb_restore_guard.sql", "assert_bridge_counts.sql", "defaultdb_restore_post_guard.sql"):
        rc, _, err = _psql(schema, "-v", "expected_counts=x", "-v", "expected_fingerprint=x", "-1",
                           "-f", str(AIVEN / name), pgoptions=hostile)
        assert rc != 0 and "FCB_RESTORE_SESSION_GUARD_FAILED" in err and "FCB_SHADOW_USED" not in err, (name, err)


# --- F5 (repro D): database/role settings and CURRENT_USER = SESSION_USER

@pytest.mark.parametrize("setting", [
    "ALTER ROLE {owner} IN DATABASE {db} SET role = {intruder}",
    "ALTER ROLE {owner} SET role = {intruder}",
    "ALTER DATABASE {db} SET check_function_bodies = off",
    "ALTER ROLE {owner} IN DATABASE {db} SET work_mem = '5MB'",
    "ALTER ROLE ALL IN DATABASE {db} SET work_mem = '5MB'",
])
def test_database_and_role_settings_are_refused(owned, setting):
    db, owner = owned
    intruder = "wp005_intruder_" + uuid.uuid4().hex[:6]
    _exec(db.dsn, SQL("CREATE ROLE {}").format(Identifier(intruder)),
          SQL("GRANT {} TO {}").format(Identifier(intruder), Identifier(owner.role)),
          SQL("GRANT CREATE ON SCHEMA public TO {}").format(Identifier(intruder)))
    stmt = setting.format(owner=owner.role, db=db.dbname, intruder=intruder)
    try:
        _exec(db.dsn, stmt)
        assert _state(owner) == "REFUSE_UNSAFE_SETTINGS"
        proc = _guard_psql(owner, _local_guard(owner))
        assert proc.returncode != 0 and "FCB_TARGET_GUARD_FAILED" in proc.stderr
        rc, _, err = _workflow_tx(owner, guard=False)
        assert rc != 0 and ("FCB_RESTORE_SESSION_GUARD_FAILED" in err or "FCB_RESTORE_STATE_REFUSED" in err)
        assert _nothing_written(owner)
    finally:
        reset = stmt.split(" SET ")[0] + " RESET ALL"
        _exec(db.dsn, reset)
        _exec(db.dsn, SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(Identifier(intruder)),
              SQL("DROP ROLE {}").format(Identifier(intruder)))


def test_role_setting_for_every_role_is_refused(owned):
    """`ALTER ROLE ALL SET` (setdatabase = 0, setrole = 0) applies to every session."""
    db, owner = owned
    try:
        _exec(db.dsn, "ALTER ROLE ALL SET lock_timeout = '17s'")
        assert _state(owner) == "REFUSE_UNSAFE_SETTINGS"
    finally:
        _exec(db.dsn, "ALTER ROLE ALL RESET lock_timeout")


# --- F6 (repro F): EMPTY means empty in every pg18_occupancy category

@pytest.mark.parametrize("ddl", [
    ["SELECT pg_catalog.lo_create(0)"],
    ["CREATE TYPE public.shell_only"],
    ["BEGIN", "PREPARE TRANSACTION 'wp005_{u}'"],
    ["SELECT pg_catalog.pg_create_logical_replication_slot('wp005_{u}', 'pgoutput')"],
    ["CREATE SUBSCRIPTION wp005_{u} CONNECTION 'host=127.0.0.1 port=1 dbname=x' PUBLICATION p "
     "WITH (connect = false, slot_name = NONE, enabled = false)"],
])
def test_empty_requires_every_occupancy_category_to_be_zero(schema, ddl):
    # Prepared transactions and logical slots need server settings the standard CI PostgreSQL
    # does not enable (max_prepared_transactions = 0, wal_level = replica): skip there, run where
    # the server allows them (verified locally with both enabled).
    with psycopg.connect(schema.dsn) as conn:
        if any("PREPARE TRANSACTION" in d for d in ddl) and int(
                conn.execute("SHOW max_prepared_transactions").fetchone()[0]) == 0:
            pytest.skip("max_prepared_transactions = 0 on this server")
        if any("logical_replication_slot" in d for d in ddl) and \
                conn.execute("SHOW wal_level").fetchone()[0] != "logical":
            pytest.skip("wal_level is not logical on this server")
    u = uuid.uuid4().hex[:8]
    _exec(schema.dsn, *[d.format(u=u) for d in ddl])
    assert _state(schema) == "REFUSE_UNTRACKED_OBJECTS"
    rc, _, err = _workflow_tx(schema, guard=False)
    assert rc != 0 and "FCB_RESTORE_STATE_REFUSED REFUSE_UNTRACKED_OBJECTS" in err
    assert _nothing_written(schema)


def test_an_extension_in_the_aiven_monitor_schema_is_not_empty(owned):
    """Only the pg_extension row is visible when the objects sit in the excluded schema."""
    db, owner = owned
    with _monitor_schema(db, "_avnadmin_monitor", "CREATE EXTENSION pgcrypto SCHEMA _avnadmin_monitor"):
        try:
            assert _state(owner) == "REFUSE_UNTRACKED_OBJECTS"
            rc, _, err = _workflow_tx(owner)
            assert rc != 0 and _nothing_written(owner)
        finally:
            _exec(db.dsn, "DROP EXTENSION pgcrypto")


# --- F7 (repro G): a concurrent commit between the state guard and the script

_SEEDED_TABLE = ("CREATE TABLE oauth_claude_dcr_clients (client_id_hash TEXT PRIMARY KEY, redirect_uri TEXT NOT NULL, "
                 "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ NOT NULL); "
                 "INSERT INTO oauth_claude_dcr_clients (client_id_hash, redirect_uri, expires_at) "
                 "VALUES ('seeded', 'https://attacker.invalid/cb', NOW() + INTERVAL '1 day')")


def test_a_large_object_committed_concurrently_after_the_guard_aborts_an_empty_restore(owned):
    _, owner = owned
    concurrent = (f"\\! PGOPTIONS='-c search_path=public' psql {shlex.quote(owner.dsn)} -X -q -v ON_ERROR_STOP=1 "
                  f"-c {shlex.quote('SELECT pg_catalog.lo_create(0)')}")
    rc, _, err = _workflow_tx(owner, before_script=[concurrent])
    assert rc != 0 and "FCB_RESTORE_FOREIGN_OBJECTS_AFTER_EMPTY" in err, err
    assert _nothing_written(owner)


def test_a_table_committed_concurrently_after_the_guard_aborts_an_empty_restore(owned):
    _, owner = owned
    seeded = _SEEDED_TABLE
    concurrent = (f"\\! PGOPTIONS='-c search_path=public' psql {shlex.quote(owner.dsn)} -X -q -v ON_ERROR_STOP=1 "
                  f"-c {shlex.quote(seeded)}")
    assert _state(owner) == "EMPTY"
    rc, _, err = _workflow_tx(owner, before_script=[concurrent])
    assert rc != 0 and "FCB_RESTORE_FOREIGN_OBJECTS_AFTER_EMPTY" in err, err
    assert _nothing_written(owner)
    rc, out, _ = _psql(owner, "-c", "SELECT pg_catalog.count(*) FROM public.oauth_claude_dcr_clients")
    assert out == "1"  # the concurrent commit happened; the restore did not certify it


def test_the_post_guard_needs_the_state_recorded_by_the_guard(schema):
    rc, _, err = _psql(schema, "-v", f"expected_counts={_clean_counts()}", "-v", f"expected_fingerprint={_clean_fingerprint()}",
                       "-1", "-f", str(AIVEN / "aiven_migrations_bridge_0001_0008.sql"),
                       "-f", str(AIVEN / "assert_bridge_counts.sql"), "-f", str(AIVEN / "defaultdb_restore_post_guard.sql"))
    assert rc != 0 and "FCB_RESTORE_PRIOR_STATE_UNKNOWN" in err
    assert _nothing_written(schema)


# ---------------------------------------------------------------- PATH A2 R1 (FCB-A2-R1-WP009-DECISIONS)
# PATH A2 variant R1 adds to defaultdb two roles and the schema fcb_cert_quota (four SECURITY DEFINER
# functions, steps 1a/1b): fcb_cert_runner holds NO privilege on any relation of public and no entry on
# schema public. The restore precheck, its in-transaction guards, the workflow $GUARD and the catalog
# fingerprint accept fcb_cert_quota ONLY in the EXACT state of sql/aiven/apifootball_cert_quota_state.sql;
# the earlier nine-privilege runner exception is gone (intentional change: those tests now prove refusal).

A2_ROLES = ("fcb_cert_runner", "fcb_cert_owner")
R1_FILES = ("apifootball_cert_path_a_quota_schema.sql", "apifootball_cert_path_a_quota_functions.sql",
            "apifootball_cert_path_a_quota_assert.sql")


def _a2_sql(name, owner):
    sql = (AIVEN / name).read_text(encoding="utf-8")
    assert sql.count("'defaultdb'::pg_catalog.name") == 1, name
    return sql.replace("'defaultdb'::pg_catalog.name", f"'{owner.dbname}'::pg_catalog.name")


def _a2_install_roles(schema_name, owner):
    """The real A2 step 1, 1a and 1b files, run by the database owner (given CREATEROLE like Aiven's
    avnadmin; it owns the restored quota tables, the pre-WP-009 shape of this module); only the
    database-name pin is adapted to this throwaway database."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from fcb_db_roles import scram_verifier
    _exec(schema_name.dsn, SQL("ALTER ROLE {} CREATEROLE CREATEDB BYPASSRLS").format(Identifier(owner.role)))  # Aiven shape
    with psycopg.connect(owner.dsn, options="-c search_path=public -c createrole_self_grant=inherit,set") as conn:
        conn.execute("SELECT pg_catalog.set_config('fcb_cert.runner_verifier', %s, true)",
                     (scram_verifier("a2-" + uuid.uuid4().hex + uuid.uuid4().hex),))
        conn.execute(_a2_sql("apifootball_cert_path_a_roles.sql", owner))
    for name in R1_FILES[:1]:
        with psycopg.connect(owner.dsn) as conn:
            conn.execute(_a2_sql(name, owner))
    with psycopg.connect(owner.dsn) as conn:  # step 1b + assert, one transaction, AS the tables' owner
        # review #201 P2: the one key hash the runner may use, pinned by step 1b
        conn.execute("SELECT pg_catalog.set_config('fcb_cert.pinned_key_hash', %s, true)", ("ab" * 32,))
        for name in R1_FILES[1:]:
            conn.execute(_a2_sql(name, owner))


def _cert_quota_state(target):
    rc, out, err = _psql(target, "-f", str(AIVEN / "apifootball_cert_quota_state.sql"))
    assert rc == 0, err
    return out


def _a2_drop_roles(schema_name):
    with psycopg.connect(schema_name.dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS fcb_cert_quota CASCADE")
        for role in (*A2_ROLES, "fcbpa_other", "fcbpa_group"):
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                conn.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
                conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


@pytest.fixture
def a2_installed(owned):
    schema_name, owner = owned
    with psycopg.connect(schema_name.dsn) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = ANY(%s)", (list(A2_ROLES),)).fetchone():
            pytest.skip("PATH A2 roles already exist on this server; not touching them")
    rc, _, err = _workflow_tx(owner)
    assert rc == 0, err
    # WITHOUT the A2 installation (schema absent): the restore behaves exactly as before
    assert _cert_quota_state(owner) == "ABSENT"
    assert _state(owner) == "BRIDGE_0001_0008" and _fingerprint(owner) == _clean_fingerprint()
    try:
        _a2_install_roles(schema_name, owner)
        yield schema_name, owner
    finally:
        _a2_drop_roles(schema_name)


def _runner_acl_entries(schema_name):
    with psycopg.connect(schema_name.dsn) as conn:
        return conn.execute(
            "SELECT (SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) x WHERE x.grantee = 'fcb_cert_runner'::regrole)"
            " + (SELECT count(*) FROM pg_attribute a, LATERAL aclexplode(a.attacl) x WHERE x.grantee = 'fcb_cert_runner'::regrole)"
            " + (SELECT count(*) FROM pg_namespace n, LATERAL aclexplode(n.nspacl) x WHERE n.nspname = 'public'"
            "    AND x.grantee = 'fcb_cert_runner'::regrole)").fetchone()[0]


def test_a2_r1_exact_install_passes_the_restore_workflow_path_and_changes_nothing(a2_installed):
    schema_name, owner = a2_installed
    assert _cert_quota_state(owner) == "EXACT"
    assert _runner_acl_entries(schema_name) == 0  # no table, column or public-schema entry for the runner
    assert _state(owner) == "BRIDGE_0001_0008"
    assert _fingerprint(owner) == _clean_fingerprint()  # public is untouched by R1
    if _major() == 18:
        assert _fingerprint(owner) == _pinned_fingerprint()  # the production pin still matches
    rows = _migration_rows(owner)
    rc, _, err = _workflow_tx(owner)  # the production no-op restore, through the workflow's $GUARD + generated guards
    assert rc == 0, err
    assert _migration_rows(owner) == rows and _state(owner) == "BRIDGE_0001_0008"
    assert _cert_quota_state(owner) == "EXACT"
    rc, _, err = _workflow_tx(owner, guard=False)  # the generated guards alone accept it too
    assert rc == 0, err


# Every drift of the R1 state, as the superuser (an attacker with the owner's or a third role's powers).
R1_TAMPER = {
    "extra_function": ["CREATE FUNCTION fcb_cert_quota.extra() RETURNS int LANGUAGE sql AS 'select 1'"],
    "altered_body": ["CREATE OR REPLACE FUNCTION fcb_cert_quota.release(p_key_hash text, p_lease_id text) RETURNS void "
                     "LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$BEGIN END$$"],
    "security_invoker": ["ALTER FUNCTION fcb_cert_quota.reserve(text) SECURITY INVOKER"],
    "search_path_missing": ["ALTER FUNCTION fcb_cert_quota.reserve(text) RESET search_path"],
    "execute_public": ["GRANT EXECUTE ON FUNCTION fcb_cert_quota.snapshot(text) TO PUBLIC"],
    "execute_other_grantee": ["CREATE ROLE fcbpa_other NOLOGIN",
                              "GRANT EXECUTE ON FUNCTION fcb_cert_quota.reserve(text) TO fcbpa_other"],
    "extra_object_in_schema": ["CREATE TABLE fcb_cert_quota.t (a int)"],
    "schema_owner_change": ["CREATE ROLE fcbpa_other NOLOGIN", "ALTER SCHEMA fcb_cert_quota OWNER TO fcbpa_other"],
    "function_owner_change": ["CREATE ROLE fcbpa_other NOLOGIN",
                              "ALTER FUNCTION fcb_cert_quota.reserve(text) OWNER TO fcbpa_other"],
    "runner_table_privilege": ["GRANT SELECT ON public.api_football_quota_counters TO fcb_cert_runner"],
    "runner_column_privilege": ["GRANT UPDATE (used) ON public.api_football_quota_counters TO fcb_cert_runner"],
    "runner_legacy_nine_privileges": [
        "GRANT SELECT, INSERT ON public.api_football_quota_counters TO fcb_cert_runner",
        "GRANT UPDATE (used) ON public.api_football_quota_counters TO fcb_cert_runner",
        "GRANT SELECT, INSERT, DELETE ON public.api_football_quota_leases, public.api_football_quota_attempts TO fcb_cert_runner"],
    "runner_schema_create": ["GRANT CREATE ON SCHEMA fcb_cert_quota TO fcb_cert_runner"],
    "runner_not_plain": ["ALTER ROLE fcb_cert_runner CREATEDB"],
    "runner_member": ["CREATE ROLE fcbpa_group NOLOGIN", "GRANT fcbpa_group TO fcb_cert_runner"],
    # review #201 P1: routines of every kind and every other object kind
    "p1_procedure": ["CREATE PROCEDURE fcb_cert_quota.reset_counters() LANGUAGE sql SECURITY DEFINER AS 'DELETE FROM public.api_football_quota_counters'"],
    "p1_aggregate": ['CREATE AGGREGATE fcb_cert_quota.agg(int) (SFUNC = int4pl, STYPE = int)'],
    "p1_overload": ["CREATE FUNCTION fcb_cert_quota.reserve(p_key_hash text, p_x int) RETURNS text LANGUAGE sql AS 'select 1::text'"],
    "p1_extra_view": ['CREATE VIEW fcb_cert_quota.v AS SELECT 1 AS a'],
    "p1_extra_sequence": ['CREATE SEQUENCE fcb_cert_quota.s'],
    "p1_extra_type": ['CREATE TYPE fcb_cert_quota.c AS (a int)'],
}


@pytest.mark.parametrize("tamper", sorted(R1_TAMPER))
def test_a2_r1_any_drift_of_fcb_cert_quota_is_refused_by_precheck_and_guards(a2_installed, tamper):
    schema_name, owner = a2_installed
    _exec(schema_name.dsn, *R1_TAMPER[tamper])
    assert _cert_quota_state(owner) == "DRIFT", tamper
    assert _state(owner) == "REFUSE_UNEXPECTED_SCHEMAS", tamper
    rows = _migration_rows(owner)
    rc, _, err = _workflow_tx(owner)  # the workflow's $GUARD refuses first
    assert rc != 0 and "FCB_TARGET_GUARD_FAILED" in err and _migration_rows(owner) == rows, tamper
    rc, _, err = _workflow_tx(owner, guard=False)  # the generated in-transaction session guard refuses too
    assert rc != 0 and "FCB_RESTORE_SESSION_GUARD_FAILED" in err and _migration_rows(owner) == rows, tamper


def test_a2_r1_a_foreign_schema_named_like_r1_but_empty_is_refused(owned):
    """An fcb_cert_quota that is not the R1 state (here: empty, no runner) is just an unexpected schema."""
    schema_name, owner = owned
    rc, _, err = _workflow_tx(owner)
    assert rc == 0, err
    try:
        _exec(owner.dsn, "CREATE SCHEMA fcb_cert_quota")
        assert _cert_quota_state(owner) == "DRIFT"
        assert _state(owner) == "REFUSE_UNEXPECTED_SCHEMAS"
        rc, _, err = _workflow_tx(owner)
        assert rc != 0 and "FCB_TARGET_GUARD_FAILED" in err
    finally:
        _exec(schema_name.dsn, "DROP SCHEMA IF EXISTS fcb_cert_quota CASCADE")


# Review #201 (0f6e1ce) F1 (kept as regression after the exception's removal): every case below must be
# REFUSE_*, with and without the A2 R1 installation. (superuser_steps, owner_steps)
A2_FAIL_OPEN = {
    "column_grant_public": ((), ["GRANT UPDATE (used) ON public.api_football_quota_counters TO PUBLIC"]),
    "column_grant_other_role": (["CREATE ROLE fcbpa_other NOLOGIN"],
                                ["GRANT UPDATE (used) ON public.api_football_quota_counters TO fcbpa_other"]),
}
NO_A2_FAIL_OPEN = {
    **A2_FAIL_OPEN,
    "table_acl_emptied": ((), ["REVOKE ALL ON public.api_football_quota_leases FROM CURRENT_USER"]),
}


def _tamper(schema_name, owner, steps):
    superuser_steps, owner_steps = steps
    if superuser_steps:
        _exec(schema_name.dsn, *superuser_steps)
    _exec(owner.dsn, *owner_steps)


@pytest.mark.parametrize("tamper", sorted(A2_FAIL_OPEN))
def test_a2_f1_acl_exception_fails_closed_with_the_runner_installed(a2_installed, tamper):
    schema_name, owner = a2_installed
    _tamper(schema_name, owner, A2_FAIL_OPEN[tamper])
    assert _state(owner).startswith("REFUSE_"), tamper
    rows = _migration_rows(owner)
    rc, _, _err = _workflow_tx(owner)
    assert rc != 0 and _migration_rows(owner) == rows


@pytest.mark.parametrize("tamper", sorted(NO_A2_FAIL_OPEN))
def test_a2_f1_acl_exception_fails_closed_without_the_runner(owned, tamper):
    schema_name, owner = owned
    with psycopg.connect(schema_name.dsn) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = ANY(%s)", (list(A2_ROLES),)).fetchone():
            pytest.skip("PATH A2 roles already exist on this server; not touching them")
    rc, _, err = _workflow_tx(owner)
    assert rc == 0, err
    assert _state(owner) == "BRIDGE_0001_0008"
    try:
        _tamper(schema_name, owner, NO_A2_FAIL_OPEN[tamper])
        assert _state(owner).startswith("REFUSE_"), tamper
        rows = _migration_rows(owner)
        rc, _, _err = _workflow_tx(owner)
        assert rc != 0 and _migration_rows(owner) == rows
    finally:
        _a2_drop_roles(schema_name)


# ------------------------------------------------------------------ PostgreSQL 18.7 / 16.16: pg_attver
# Those minors set the internal column option `pg_attver=<decimal>` on every column they create or
# re-type (CREATE TABLE, ADD COLUMN, ALTER COLUMN TYPE); ALTER COLUMN SET/RESET (pg_attver=...) is
# silently ignored, so only the server writes it. The column_props drift check accepts exactly that
# one key, decimal, on those versions; anything else is still REFUSE_RUNTIME_TABLE_DRIFT.

def _server_version_num():
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        return conn.info.server_version


def _attver_server():
    v = _server_version_num()
    return v >= 180007 or 160016 <= v < 170000


def _column_options(target, table="api_football_quota_counters", column="api_key_hash"):
    with psycopg.connect(target.dsn) as conn:
        return conn.execute("SELECT a.attoptions FROM pg_catalog.pg_attribute a WHERE a.attrelid = %s::regclass "
                            "AND a.attname = %s", (f"public.{table}", column)).fetchone()[0]


def test_pg_attver_is_server_managed_on_this_minor(schema):
    """Evidence for the exception: present on every new column only on 18.7+/16.16+, and an
    ALTER COLUMN SET/RESET of it does not change or remove it (18.6/16.15 reject the key)."""
    _runtime_tables(schema)
    before = _column_options(schema)
    if not _attver_server():
        assert before is None
        rc, _, err = _psql(schema, "-c", "ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash SET (pg_attver = 1)")
        assert rc != 0 and 'unrecognized parameter "pg_attver"' in err
        return
    assert len(before) == 1 and re.fullmatch(r"pg_attver=[0-9]+", before[0]), before
    for stmt in ("ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash SET (pg_attver = 1)",
                 "ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash SET (pg_attver = 'abc')",
                 "ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash RESET (pg_attver)"):
        rc, _, err = _psql(schema, "-c", stmt)
        assert rc == 0, err
        assert _column_options(schema) == before, stmt
    with psycopg.connect(schema.dsn) as conn:
        missing = conn.execute(
            "SELECT pg_catalog.count(*) FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND a.attnum > 0 AND NOT a.attisdropped AND a.attoptions IS NULL").fetchone()[0]
    assert missing == 0
    assert _state(schema) == "RUNTIME_ONLY"


@pytest.mark.parametrize("variant", [
    "ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash SET (n_distinct = 5)",
    "ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash SET (n_distinct_inherited = 3)",
    "ALTER TABLE api_football_quota_counters ALTER COLUMN api_key_hash SET (n_distinct = -1, n_distinct_inherited = -0.5)",
])
def test_any_other_column_option_is_still_drift_with_or_without_pg_attver(schema, variant):
    _runtime_tables(schema)
    rc, _, err = _psql(schema, "-c", variant)
    assert rc == 0, err
    assert any(not o.startswith("pg_attver=") for o in _column_options(schema))
    assert _state(schema) == "REFUSE_RUNTIME_TABLE_DRIFT"
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc != 0 and "REFUSE_RUNTIME_TABLE_DRIFT" in err  # the in-transaction guard too


@pytest.mark.parametrize("options", [
    "{pg_attver=abc}", "{pg_attver=}", "{pg_attver=12x}", "{pg_attver=-1}", "{pg_attver=1,pg_attver=2}",
    "{pg_attver=1,n_distinct=5}", "{n_distinct=5,pg_attver=1}", "{PG_ATTVER=1}", "{pg_attver2=1}", "{\"pg_attver=1 \"}", "{\" pg_attver=1\"}",
    # review #202 P3-a: a lower bound other than 1 made (attoptions)[1] NULL and the predicate fail open
    "[0:0]={n_distinct=5}", "[2:2]={n_distinct=5}", "[0:0]={pg_attver=1}", "[2:2]={pg_attver=1}",
])
def test_malformed_or_extra_pg_attver_written_into_the_catalog_is_drift(schema, options):
    """Only a superuser editing pg_attribute directly can produce these; each is refused, or the
    18.7 server itself fails the precheck while parsing it ("could not parse attribute version"):
    fail closed either way, never RUNTIME_ONLY."""
    _runtime_tables(schema)
    _exec(schema.dsn, "UPDATE pg_catalog.pg_attribute SET attoptions = %s::text[] "
                      "WHERE attrelid = 'public.api_football_quota_counters'::regclass AND attname = 'api_key_hash'"
          .replace("%s", "'" + options + "'"))
    rc, out, err = _psql(schema, "-f", str(AIVEN / "defaultdb_precheck.sql"))
    if rc == 0:
        assert out == "REFUSE_RUNTIME_TABLE_DRIFT", (options, out)
    else:
        assert _attver_server() and "could not parse attribute version" in err, (options, err)
    # review #202 P3-d: the generated in-transaction guards refuse it too (never apply)
    rc, _, err = _restore_tx(schema, _clean_counts())
    assert rc != 0 and ("REFUSE_RUNTIME_TABLE_DRIFT" in err
                        or (_attver_server() and "could not parse attribute version" in err)), (options, err)


def test_a_well_formed_pg_attver_is_accepted_only_on_the_minors_that_write_it(schema):
    _runtime_tables(schema)
    _exec(schema.dsn, "UPDATE pg_catalog.pg_attribute SET attoptions = '{pg_attver=4242}'::text[] "
                      "WHERE attrelid = 'public.api_football_quota_counters'::regclass AND attname = 'api_key_hash'")
    assert _state(schema) == ("RUNTIME_ONLY" if _attver_server() else "REFUSE_RUNTIME_TABLE_DRIFT")
