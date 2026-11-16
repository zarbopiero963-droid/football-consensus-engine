"""#187 / CLAUDE.md §5: signer migrations 0002/0003 on an ALREADY INITIALISED signer database (0001 applied, with
data), on a THROWAWAY PostgreSQL only. Each test runs in its own schema (search_path), so it always runs, also on
the shared CI test database (review a046ad9 P2-1). Additive, idempotent, tracked, no data lost, failures visible."""
import os
import shutil
import uuid
os.environ.setdefault("SXBET_SIGNER_PROCESS", "1")

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
    for missing in ({"network": "testnet"}, {"kind": "PLACE"}):           # review d81c500 P3-4: NULL not allowed
        with pytest.raises(psycopg.Error):
            with connect() as conn:
                conn.execute("INSERT INTO signer_order_requests (request_id, kind, payload, payload_sha256, network) "
                             "VALUES (%s, 'PLACE', %s::jsonb, %s, 'testnet')",
                             ("e" * 32, json.dumps(missing), "sha256:" + "0" * 64))
    with connect() as conn:                                                # a consistent row is accepted
        conn.execute("INSERT INTO signer_order_requests (request_id, kind, payload, payload_sha256, network) "
                     "VALUES (%s, 'PLACE', %s::jsonb, %s, 'testnet')", ("f" * 32, json.dumps(body), "sha256:" + "0" * 64))


def test_queue_seq_is_assigned_by_the_database_and_the_watermark_is_above_every_insert(connect):
    """Owner decision 16-11-2026: clock-free ordering. The writer cannot choose queue_seq (overwritten by trigger)
    and cannot change it (append-only); a release watermark is greater than every request inserted before."""
    import json
    import psycopg
    from signer.request_queue import PostgresRequestQueue
    migrations.apply_migrations(connect, series=migrations.SIGNER)

    def ins(rid, forced_seq=None):
        body = {"kind": "PLACE", "network": "testnet"}
        with connect() as conn:
            if forced_seq is None:
                conn.execute("INSERT INTO signer_order_requests (request_id, kind, payload, payload_sha256, network) "
                             "VALUES (%s, 'PLACE', %s::jsonb, %s, 'testnet')", (rid, json.dumps(body), "sha256:" + "0" * 64))
            else:
                conn.execute("INSERT INTO signer_order_requests (request_id, queue_seq, kind, payload, payload_sha256, "
                             "network) VALUES (%s, %s, 'PLACE', %s::jsonb, %s, 'testnet')",
                             (rid, forced_seq, json.dumps(body), "sha256:" + "0" * 64))
            return conn.execute("SELECT queue_seq FROM signer_order_requests WHERE request_id = %s", (rid,)).fetchone()[0]

    a = ins("1" * 32)
    b = ins("2" * 32, forced_seq=10 ** 15)                       # a forged, "newer" value is overwritten
    assert b == a + 1
    wm = PostgresRequestQueue(connect).release_watermark()
    assert wm > b
    assert ins("3" * 32) > wm
    with pytest.raises(psycopg.Error):
        with connect() as conn:
            conn.execute("UPDATE signer_order_requests SET queue_seq = 1")


def test_watermark_refused_unless_the_sequence_is_strict(connect):
    """Review dccb44a P3-3: per-session caching would let a pre-release request get a larger queue_seq."""
    from signer.request_queue import PostgresRequestQueue, QueueError
    migrations.apply_migrations(connect, series=migrations.SIGNER)
    q = PostgresRequestQueue(connect)
    assert q.release_watermark() > 0                                  # CACHE 1, NO CYCLE as shipped
    with connect() as conn:
        conn.execute("ALTER SEQUENCE signer_order_requests_queue_seq CACHE 20")
    with pytest.raises(QueueError) as e:
        q.release_watermark()
    assert e.value.code == "QUEUE_SEQUENCE_NOT_STRICT"
