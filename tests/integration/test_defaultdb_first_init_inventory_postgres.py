"""WP-005 first-install inventory on a real disposable PostgreSQL database."""
from __future__ import annotations

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
        conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


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
