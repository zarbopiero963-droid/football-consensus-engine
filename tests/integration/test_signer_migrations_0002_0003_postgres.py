"""#187 / CLAUDE.md §5: signer migrations 0002/0003 on an ALREADY INITIALISED signer database (0001 applied, with
data), on a THROWAWAY PostgreSQL only. Each test runs in its own schema (search_path), so it always runs, also on
the shared CI test database (review a046ad9 P2-1). Additive, idempotent, tracked, no data lost, failures visible."""
import os
import shutil
import uuid

import pytest

from app import migrations

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not URL, reason="requires dedicated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(bool(URL) and URL == os.environ.get("DATABASE_URL"),
                       reason="TEST_DATABASE_URL must not be the production DATABASE_URL"),
]


@pytest.fixture
def connect():
    import psycopg
    from psycopg.sql import SQL, Identifier
    name = "s0203_" + uuid.uuid4().hex[:12]
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(name)))

    def _connect(**kw):
        return psycopg.connect(URL, connect_timeout=5, options=f"-c search_path={name}", **kw)
    yield _connect
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(name)))


def _tracked(connect):
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT version FROM signer_schema_migrations ORDER BY version")]


def _only_0001(tmp_path):
    d = tmp_path / "signer"
    d.mkdir()
    shutil.copy(migrations.SIGNER.directory / "0001_signer_ledger_anchor.sql", d)
    return d


def test_upgrade_from_0001_keeps_data_and_is_idempotent(tmp_path, connect):
    migrations.apply_migrations(connect, _only_0001(tmp_path), series=migrations.SIGNER)   # the state today
    assert _tracked(connect) == [1]
    with connect() as conn:
        conn.execute("INSERT INTO signer_ledger_writer_leases (ledger_id, fencing_token, holder) "
                     "VALUES ('it-0002:ledger', 1, 'it')")
    migrations.apply_migrations(connect, series=migrations.SIGNER)                         # upgrade: 0002 + 0003
    assert _tracked(connect) == [1, 2, 3]
    migrations.apply_migrations(connect, series=migrations.SIGNER)                         # idempotent re-run
    assert _tracked(connect) == [1, 2, 3]
    with connect() as conn:
        assert conn.execute("SELECT fencing_token FROM signer_ledger_writer_leases "
                            "WHERE ledger_id = 'it-0002:ledger'").fetchone()[0] == 1
        for t in ("signer_order_requests", "signer_order_request_events", "signer_status"):
            assert conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is not None, t


def test_a_changed_applied_migration_fails_visibly(tmp_path, connect):
    import psycopg
    migrations.apply_migrations(connect, series=migrations.SIGNER)
    tampered = tmp_path / "tampered"
    shutil.copytree(migrations.SIGNER.directory, tampered)
    p = tampered / "0003_signer_status.sql"
    p.write_text(p.read_text() + "\n-- changed after apply\n")
    with pytest.raises(psycopg.Error):
        migrations.apply_migrations(connect, tampered, series=migrations.SIGNER)


def test_row_kind_and_network_must_match_the_payload(connect):
    import json
    import psycopg
    migrations.apply_migrations(connect, series=migrations.SIGNER)
    body = {"kind": "PLACE", "network": "testnet"}
    for kind, network in (("CANCEL", "testnet"), ("PLACE", "mainnet")):
        with pytest.raises(psycopg.Error):
            with connect() as conn:
                conn.execute("INSERT INTO signer_order_requests (request_id, kind, payload, payload_sha256, network) "
                             "VALUES (%s, %s, %s::jsonb, %s, %s)",
                             ("c" * 32, kind, json.dumps(body), "sha256:" + "0" * 64, network))
