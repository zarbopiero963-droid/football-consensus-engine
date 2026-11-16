"""#187 / CLAUDE.md §5: signer migrations 0002/0003 on an ALREADY INITIALISED signer database (0001 applied, with
data), on a THROWAWAY PostgreSQL only. Additive, idempotent, tracked, no data lost, failures visible."""
import os
import shutil

import pytest

from app import migrations

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not URL, reason="requires dedicated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(bool(URL) and URL == os.environ.get("DATABASE_URL"),
                       reason="TEST_DATABASE_URL must not be the production DATABASE_URL"),
]


def _connect(**kw):
    import psycopg
    return psycopg.connect(URL, connect_timeout=5, **kw)


def _tracked():
    with _connect() as conn:
        return [r[0] for r in conn.execute("SELECT version FROM signer_schema_migrations ORDER BY version")]


def test_upgrade_from_0001_keeps_data_and_is_idempotent(tmp_path):
    with _connect() as conn:
        exists = conn.execute("SELECT to_regclass('signer_schema_migrations')").fetchone()[0]
    if exists is not None:
        pytest.skip("needs a fresh throwaway database (the signer series is already applied here)")
    only_0001 = tmp_path / "signer"
    only_0001.mkdir()
    shutil.copy(migrations.SIGNER.directory / "0001_signer_ledger_anchor.sql", only_0001)
    migrations.apply_migrations(_connect, only_0001, series=migrations.SIGNER)      # the live state today
    assert _tracked() == [1]
    with _connect() as conn:
        conn.execute("INSERT INTO signer_ledger_writer_leases (ledger_id, fencing_token, holder) "
                     "VALUES ('it-0002:ledger', 1, 'it')")
    migrations.apply_migrations(_connect, series=migrations.SIGNER)                 # upgrade: 0002 + 0003
    assert _tracked() == [1, 2, 3]
    migrations.apply_migrations(_connect, series=migrations.SIGNER)                 # idempotent re-run
    assert _tracked() == [1, 2, 3]
    with _connect() as conn:
        assert conn.execute("SELECT fencing_token FROM signer_ledger_writer_leases "
                            "WHERE ledger_id = 'it-0002:ledger'").fetchone()[0] == 1
        for t in ("signer_order_requests", "signer_order_request_events", "signer_status"):
            assert conn.execute(f"SELECT to_regclass('{t}')").fetchone()[0] is not None, t


def test_a_changed_applied_migration_fails_visibly(tmp_path):
    import psycopg
    if _tracked() != [1, 2, 3]:
        pytest.skip("runs after the upgrade test on the same throwaway database")
    tampered = tmp_path / "signer"
    shutil.copytree(migrations.SIGNER.directory, tampered)
    p = tampered / "0003_signer_status.sql"
    p.write_text(p.read_text() + "\n-- changed after apply\n")
    with pytest.raises(psycopg.Error):
        migrations.apply_migrations(_connect, tampered, series=migrations.SIGNER)
