"""Real PostgreSQL: ledger head anchoring + writer lease with fencing (H1/H2), migration 0008 (governance §2.6, §5).

Requires a dedicated, throwaway TEST_DATABASE_URL; never production credentials.
"""
from __future__ import annotations

import os
import secrets

import pytest

pytest.importorskip("cryptography")

from app import migrations  # noqa: E402
from signer import anchor as an  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.orders import LedgerError, OrderLedger  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires dedicated TEST_DATABASE_URL; no production credentials",
)

ADDR = "0x" + "aa" * 20
MKT = "0x" + "ab" * 32
NOW = 1_800_000_000


def _connect(**kwargs):
    import psycopg
    return psycopg.connect(os.environ["TEST_DATABASE_URL"], connect_timeout=5, **kwargs)


def _dead_connect(**kwargs):
    import psycopg
    return psycopg.connect("postgresql://nobody@127.0.0.1:1/none", connect_timeout=2, **kwargs)


@pytest.fixture(scope="module", autouse=True)
def schema():
    migrations.apply_migrations(_connect)
    migrations.apply_migrations(_connect)  # idempotent on an initialized database


@pytest.fixture
def signer_id():
    return "itest-" + secrets.token_hex(6)


def _store():
    return an.PostgresAnchorStore(_connect, holder="itest")


def _ledger(tmp_path, signer_id, store, create=False, **kw):
    fn = OrderLedger.create if create else OrderLedger.open
    return fn(str(tmp_path / "ledger.jsonl"), signer_id=signer_id, signer_address=ADDR, clock=lambda: NOW,
              anchor=store, **kw)


def _authorize(led, n):
    return led.authorize(client_order_id=f"order-key-{n:04d}", intent_hash="sha256:" + "1" * 64,
                         order_id="0x" + format(n, "064x"), market_hash=MKT, event_id="L1", stake=1_000_000,
                         expiry=NOW + 3600, approval_nonce=f"nonce-{n:016d}")


def _rows(ledger_id):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT seq, head_hash, fencing_token FROM signer_ledger_anchors WHERE ledger_id=%s ORDER BY seq",
                    (ledger_id,))
        return cur.fetchall()


def test_migration_0008_applied_once_and_tracked():
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT version, name FROM schema_migrations WHERE version=8")
        assert cur.fetchall() == [(8, "0008_signer_ledger_anchor.sql")]
        cur.execute("SELECT count(*) FROM schema_migrations")
        assert cur.fetchone()[0] == len(migrations.load_migrations())
        cur.execute("SELECT to_regclass('signer_ledger_anchors'), to_regclass('signer_ledger_writer_leases')")
        assert all(cur.fetchone())


def test_anchor_is_monotonic_and_append_only(tmp_path, signer_id):
    store = _store()
    led = _ledger(tmp_path, signer_id, store, create=True)
    for n in (1, 2, 3):
        _authorize(led, n)
    rows = _rows(led.ledger_id)
    assert [r[0] for r in rows] == [1, 2, 3, 4] and rows[-1][1] == led.head_hash
    assert {r[2] for r in rows} == {led.fencing_token}
    with pytest.raises(an.AnchorConflict):  # stale expectation: never overwrites, never goes back
        store.advance(led.ledger_id, token=led.fencing_token, expected_seq=2, seq=3, head_hash="sha256:" + "2" * 64)
    import psycopg
    for sql in ("UPDATE signer_ledger_anchors SET head_hash = head_hash WHERE ledger_id = %s",
                "DELETE FROM signer_ledger_anchors WHERE ledger_id = %s"):
        with pytest.raises(psycopg.Error, match="SIGNER_LEDGER_ANCHOR_APPEND_ONLY"):
            with _connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (led.ledger_id,))
    with pytest.raises(psycopg.Error, match="SIGNER_LEDGER_ANCHOR_APPEND_ONLY"):
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE signer_ledger_anchors")
    with pytest.raises(psycopg.Error, match="SIGNER_LEDGER_ANCHOR_SEQ_NOT_MONOTONIC"):
        with _connect() as conn, conn.cursor() as cur:  # even a direct insert behind the signer's back
            cur.execute("INSERT INTO signer_ledger_anchors (ledger_id, seq, head_hash, fencing_token) "
                        "VALUES (%s, 2, %s, %s)", (led.ledger_id, "sha256:" + "3" * 64, led.fencing_token))
    assert len(_rows(led.ledger_id)) == 4
    led.close()


def test_truncation_and_rollback_detected(tmp_path, signer_id):
    store = _store()
    led = _ledger(tmp_path, signer_id, store, create=True)
    _authorize(led, 1)
    led.transition("order-key-0001", od.SIGNED, reason="SIGNED")
    led.close()
    p = tmp_path / "ledger.jsonl"
    p.write_text("\n".join(p.read_text().splitlines()[:-1]) + "\n")
    with pytest.raises(LedgerError) as e:
        _ledger(tmp_path, signer_id, store)
    assert e.value.code == "LEDGER_ANCHOR_AHEAD_OF_LOCAL"
    p.unlink()
    with pytest.raises(LedgerError) as e:
        _ledger(tmp_path, signer_id, store, create=True)
    assert e.value.code == "LEDGER_ANCHOR_EXISTS" and not p.exists()


def test_two_writers_second_refused_and_stale_writer_fenced(tmp_path, signer_id):
    first = _ledger(tmp_path, signer_id, _store(), create=True)
    _authorize(first, 1)
    with pytest.raises(LedgerError) as e:
        _ledger(tmp_path, signer_id, _store())
    assert e.value.code == "LEDGER_LOCKED"
    # The first writer's database session dies (network partition, idle kill): its advisory lock goes away.
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = %s",
                    ("fce-signer-lease:" + first.ledger_id[:40],))
        assert cur.fetchone()[0] is True
    second = _ledger(tmp_path, signer_id, _store())
    assert second.fencing_token > first.fencing_token
    # The stale writer still believes it is the writer: its next anchored append is fenced, and poisons it.
    with pytest.raises(LedgerError) as e:
        _authorize(first, 2)
    assert e.value.code == "LEDGER_FENCED" and first.poisoned
    rows = _rows(second.ledger_id)
    assert rows[-1][0] == 2 and rows[-1][2] == first.fencing_token  # nothing anchored by the stale writer
    first.close()
    second.close()


def test_unreachable_database_fails_closed(tmp_path, signer_id):
    with pytest.raises(LedgerError) as e:
        _ledger(tmp_path, signer_id, an.PostgresAnchorStore(_dead_connect), create=True)
    assert e.value.code == "LEDGER_ANCHOR_UNREACHABLE" and not (tmp_path / "ledger.jsonl").exists()
    led = _ledger(tmp_path, signer_id, _store(), create=True)
    led.close()
    with pytest.raises(LedgerError) as e:
        _ledger(tmp_path, signer_id, an.PostgresAnchorStore(_dead_connect))
    assert e.value.code == "LEDGER_ANCHOR_UNREACHABLE"


def test_anchor_write_failure_after_local_fsync_poisons(tmp_path, signer_id):
    store = _store()
    led = _ledger(tmp_path, signer_id, store, create=True)
    store._connect = _dead_connect  # the database goes away after start
    with pytest.raises(LedgerError) as e:
        _authorize(led, 1)
    assert e.value.code == "LEDGER_ANCHOR_WRITE_FAILED" and led.poisoned
    led.close()
    with pytest.raises(LedgerError) as e:
        _ledger(tmp_path, signer_id, _store())
    assert e.value.code == "LEDGER_LOCAL_AHEAD_OF_ANCHOR"
