"""FCB-WP-006 on real PostgreSQL: every readiness state, one throwaway schema per test."""
from __future__ import annotations

import os
import shutil
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402
from psycopg.sql import SQL, Identifier  # noqa: E402

from app import migrations  # noqa: E402
from bridge.schema_readiness import evaluate  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"),
                                reason="requires dedicated TEST_DATABASE_URL; no production credentials")


@pytest.fixture
def connect():
    dsn = os.environ["TEST_DATABASE_URL"]
    name = "wp006_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(name)))

    def _connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={name}")
    yield _connect
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(name)))


def test_empty_then_ready(connect):
    assert evaluate(connect)["state"] == "EMPTY"
    migrations.apply_migrations(connect)
    result = evaluate(connect)
    # Intentional change (OAuth persistent throttle): 9 bridge versions; deferred_versions added.
    assert result == {"state": "READY", "missing_tables": [], "bridge_versions": 9, "deferred_versions": []}


def test_outdated_database(connect, tmp_path):
    for p in sorted(migrations.BRIDGE.directory.glob("000[1-7]_*.sql")):
        shutil.copyfile(p, tmp_path / p.name)
    migrations.apply_migrations(connect, tmp_path)
    assert evaluate(connect)["state"] == "OUTDATED"


def test_drift_ahead_and_missing_tables(connect):
    migrations.apply_migrations(connect)
    with connect() as conn:
        conn.execute("DROP TABLE oauth_claude_dcr_clients")
    result = evaluate(connect)
    assert result["state"] == "MISSING_TABLES" and result["missing_tables"] == ["oauth_claude_dcr_clients"]
    with connect() as conn:
        conn.execute("UPDATE schema_migrations SET sha256 = repeat('0', 64) WHERE version = 2")
    assert evaluate(connect)["state"] == "DRIFT"
    with connect() as conn:
        conn.execute("UPDATE schema_migrations SET sha256 = %s WHERE version = 2",
                     (migrations.load_migrations()[1].checksum,))
        conn.execute("INSERT INTO schema_migrations(version, name, sha256) VALUES (99, '0099_x.sql', 'x')")
    assert evaluate(connect)["state"] == "AHEAD"


def test_drift_wins_over_ahead_and_a_view_is_not_a_table(connect):
    """Review of #171: the most serious state is reported; a same-named view does not count."""
    migrations.apply_migrations(connect)
    with connect() as conn:
        conn.execute("UPDATE schema_migrations SET sha256 = repeat('0', 64) WHERE version = 2")
        conn.execute("INSERT INTO schema_migrations(version, name, sha256) VALUES (99, '0099_x.sql', 'x')")
    assert evaluate(connect)["state"] == "DRIFT"
    with connect() as conn:
        conn.execute("UPDATE schema_migrations SET sha256 = %s WHERE version = 2",
                     (migrations.load_migrations()[1].checksum,))
        conn.execute("DELETE FROM schema_migrations WHERE version = 99")
        conn.execute("DROP TABLE oauth_claude_dcr_clients")
        conn.execute("CREATE VIEW oauth_claude_dcr_clients AS SELECT 1 AS x")
    result = evaluate(connect)
    assert result["state"] == "MISSING_TABLES" and result["missing_tables"] == ["oauth_claude_dcr_clients"]
