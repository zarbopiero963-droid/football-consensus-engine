"""FCB-WP-004 on real PostgreSQL (CI: 16 and 18): bridge/signer series split + runtime tables.

Each test runs in its own schema (search_path), so the shared test database is never
reset and tests stay independent. No financial migration is applied except in the explicit
signer-series isolation test, on a throwaway schema.
"""
from __future__ import annotations

import os
import shutil
import threading
import uuid
from hashlib import sha256

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402
from psycopg.sql import SQL, Identifier  # noqa: E402

from app import migrations  # noqa: E402
from bridge.api_football import quota as quota_mod  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires dedicated TEST_DATABASE_URL; no production credentials",
)

RUNTIME_TABLES = ("api_football_quota_counters", "api_football_quota_leases",
                  "api_football_quota_attempts", "oauth_claude_dcr_clients")
SIGNER_TABLES = ("signer_ledger_anchors", "signer_ledger_writer_leases")
# Byte checksums of the pre-split files sql/migrations/000N_*.sql on main df76738.
LEGACY = {
    1: ("0001_baseline.sql", "a6cd3bda6f1bfb8f029d4039b2c50d0ed8d444bc12470b28a382faac068f7287"),
    2: ("0002_decision_requests.sql", "a4a889aaf87ef74a40fb8453b96cbea451b6673ab5dda3eed2e127d906b21012"),
    3: ("0003_browser_session_leases.sql", "4cb5e76853a41fad80ca8f5cb11f5415e3f7dec70a7e351548e00b51bd3e172e"),
    4: ("0004_canonical_entities.sql", "483b2391d9b4af2823f3d74784562777a16e41c4e11c0d9415aea2cafa715a8f"),
    5: ("0005_pilot_server_sealing.sql", "ee7ad29edc404f38608404216fd55da795eba597fcd6d6eddb7e7f80001fbd56"),
    6: ("0006_evaluation_protocols.sql", "08253596be53ccc1273a7f956793b6c20ae28333c5dc7553c67d8d530acb13fb"),
    7: ("0007_sxbet_rate_limits.sql", "3f3f06d34cf7e83e01a096bd7366eeeecac2edd5d05b1039c491ea64183b74e4"),
}
LEGACY_SIGNER_0008 = ("0008_signer_ledger_anchor.sql",
                      "8b193c18f9c7f6009dfab3b4a5af165e3ec3cfcc399fefbc205f5ac33ee3c566")


@pytest.fixture
def schema():
    dsn = os.environ["TEST_DATABASE_URL"]
    name = "wp004_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(name)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={name}")
    yield name, connect
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(name)))


def _rows(connect, table="schema_migrations"):
    with connect() as conn:
        return conn.execute(SQL("SELECT version, name, sha256, applied_at FROM {} ORDER BY version")
                            .format(Identifier(table))).fetchall()


def _exists(connect, schema_name, table):
    with connect() as conn:
        return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema_name}.{table}",)).fetchone()["ok"]


def _legacy_dir(tmp_path, versions):
    d = tmp_path / "legacy"
    d.mkdir()
    for v in versions:
        name, digest = LEGACY[v]
        src = migrations.BRIDGE.directory / name
        assert sha256(src.read_bytes()).hexdigest() == digest  # byte-identical move
        shutil.copyfile(src, d / name)
    return d


def test_empty_database_gets_bridge_0001_0008_and_no_signer_schema(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    rows = _rows(connect)
    assert [r["version"] for r in rows] == list(range(1, 9))
    for r in rows[:7]:
        assert (r["name"], r["sha256"]) == LEGACY[r["version"]]  # checksums preserved
    assert rows[7]["name"] == "0008_runtime_tables.sql"
    assert all(_exists(connect, name, t) for t in RUNTIME_TABLES)
    assert not any(_exists(connect, name, t) for t in SIGNER_TABLES)  # no financial migration
    assert not _exists(connect, name, "signer_schema_migrations")


def test_database_initialized_with_0001_0007_upgrades_without_touching_history(schema, tmp_path):
    name, connect = schema
    migrations.apply_migrations(connect, _legacy_dir(tmp_path, range(1, 8)))
    before = _rows(connect)
    assert len(before) == 7
    migrations.apply_migrations(connect)
    after = _rows(connect)
    assert after[:7] == before  # same names, checksums AND applied_at: nothing re-applied
    assert after[7]["version"] == 8 and after[7]["name"] == "0008_runtime_tables.sql"


def test_runtime_created_tables_and_data_survive_the_migration(schema, tmp_path):
    """Live databases already have these tables (created by the running code)."""
    name, connect = schema
    migrations.apply_migrations(connect, _legacy_dir(tmp_path, range(1, 8)))
    with connect() as conn:
        for statement in quota_mod._SCHEMA:
            conn.execute(statement)
        conn.execute("""CREATE TABLE IF NOT EXISTS oauth_claude_dcr_clients (
               client_id_hash TEXT PRIMARY KEY,
               redirect_uri TEXT NOT NULL,
               created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
               expires_at TIMESTAMPTZ NOT NULL
            )""")
        conn.execute("INSERT INTO api_football_quota_counters VALUES ('h','day',1,5)")
        conn.execute("INSERT INTO oauth_claude_dcr_clients (client_id_hash, redirect_uri, expires_at) "
                     "VALUES ('c', 'https://claude.ai/cb', NOW() + INTERVAL '1 day')")
    migrations.apply_migrations(connect)
    with connect() as conn:
        assert conn.execute("SELECT used FROM api_football_quota_counters").fetchone()["used"] == 5
        assert conn.execute("SELECT count(*) AS n FROM oauth_claude_dcr_clients").fetchone()["n"] == 1
    assert len(_rows(connect)) == 8


def _columns(connect, schema_name):
    with connect() as conn:
        return conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema=%s AND table_name = ANY(%s) "
            "ORDER BY table_name, ordinal_position", (schema_name, list(RUNTIME_TABLES))).fetchall()


def _indexes(connect, schema_name):
    with connect() as conn:
        return conn.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname=%s "
            "AND tablename = ANY(%s) ORDER BY indexname", (schema_name, list(RUNTIME_TABLES))).fetchall()


def test_migration_ddl_is_identical_to_the_runtime_ddl(schema):
    """Migration-created and runtime-created tables must be indistinguishable (no drift)."""
    name, connect = schema
    migrations.apply_migrations(connect)
    via_migration = (_columns(connect, name), [
        {**i, "indexdef": i["indexdef"].replace(name + ".", "")} for i in _indexes(connect, name)])
    dsn = os.environ["TEST_DATABASE_URL"]
    other = "wp004_rt_" + uuid.uuid4().hex[:10]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(other)))
    try:
        def connect_rt():
            return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={other}")
        with connect_rt() as conn:
            for statement in quota_mod._SCHEMA:
                conn.execute(statement)
            conn.execute("""CREATE TABLE IF NOT EXISTS oauth_claude_dcr_clients (
               client_id_hash TEXT PRIMARY KEY,
               redirect_uri TEXT NOT NULL,
               created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
               expires_at TIMESTAMPTZ NOT NULL
            )""")
        via_runtime = (_columns(connect_rt, other), [
            {**i, "indexdef": i["indexdef"].replace(other + ".", "")} for i in _indexes(connect_rt, other)])
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(other)))
    assert via_migration == via_runtime
    assert len(via_migration[0]) == 14 and len(via_migration[1]) == 6  # 4 PKs + 2 indexes


def test_repeated_apply_is_idempotent(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    first = _rows(connect)
    for _ in range(3):
        migrations.apply_migrations(connect)
    assert _rows(connect) == first


def test_failing_migration_rolls_back_the_whole_upgrade(schema, tmp_path):
    name, connect = schema
    d = _legacy_dir(tmp_path, range(1, 8))
    (d / "0008_broken.sql").write_text("CREATE TABLE wp004_half (id INT);\nSELECT 1/0;\n")
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrations.apply_migrations(connect, d)
    assert not _exists(connect, name, "wp004_half")
    assert not _exists(connect, name, "schema_migrations")  # nothing at all was committed
    assert not _exists(connect, name, "model_snapshots")


def test_concurrent_appliers_serialize_on_the_advisory_lock(schema):
    name, connect = schema
    errors = []

    def run():
        try:
            migrations.apply_migrations(connect)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(type(exc).__name__)
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert [r["version"] for r in _rows(connect)] == list(range(1, 9))


def test_checksum_drift_is_refused_and_nothing_changes(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    with connect() as conn:
        conn.execute("UPDATE schema_migrations SET sha256=repeat('0',64) WHERE version=3")
    before = _rows(connect)
    with pytest.raises(psycopg.errors.RaiseException, match="Migration 3 checksum/name drift"):
        migrations.apply_migrations(connect)
    assert _rows(connect) == before


def test_pre_split_database_with_signer_0008_is_refused_not_rewritten(schema, tmp_path):
    """A database migrated by the old single series has version 8 = signer anchor. The new
    bridge 0008 must not silently coexist with it: the run fails closed, history untouched."""
    name, connect = schema
    d = _legacy_dir(tmp_path, range(1, 8))
    shutil.copyfile(migrations.SIGNER.directory / "0001_signer_ledger_anchor.sql", d / LEGACY_SIGNER_0008[0])
    migrations.apply_migrations(connect, d)
    before = _rows(connect)
    assert (before[7]["name"], before[7]["sha256"]) == LEGACY_SIGNER_0008
    with pytest.raises(psycopg.errors.RaiseException, match="PRE_SPLIT_SIGNER_0008"):
        migrations.apply_migrations(connect)
    assert _rows(connect) == before
    assert not _exists(connect, name, "api_football_quota_counters")


def test_unknown_newer_version_blocks_an_older_checkout(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    with connect() as conn:
        conn.execute("INSERT INTO schema_migrations(version,name,sha256) VALUES (99,'0099_future.sql','x')")
    with pytest.raises(psycopg.errors.RaiseException, match="absent from this checkout"):
        migrations.apply_migrations(connect)


def test_signer_series_is_tracked_separately_and_never_by_the_bridge(schema):
    name, connect = schema
    migrations.apply_migrations(connect)
    bridge_rows = _rows(connect)
    migrations.apply_migrations(connect, series=migrations.SIGNER)
    signer_rows = _rows(connect, "signer_schema_migrations")
    assert [(r["version"], r["name"], r["sha256"]) for r in signer_rows] == [
        (1, "0001_signer_ledger_anchor.sql", LEGACY_SIGNER_0008[1])]  # same bytes as former 0008
    assert _rows(connect) == bridge_rows  # bridge history untouched
    assert all(_exists(connect, name, t) for t in SIGNER_TABLES)
    migrations.apply_migrations(connect, series=migrations.SIGNER)
    assert _rows(connect, "signer_schema_migrations") == signer_rows  # idempotent
