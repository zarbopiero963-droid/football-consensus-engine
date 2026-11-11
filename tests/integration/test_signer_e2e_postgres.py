"""Real PostgreSQL (THROWAWAY TEST_DATABASE_URL only): signer migration series 0001 (WP-004) + anchored ledger under the SIMULATED e2e engine.

Never production credentials: the module is skipped without TEST_DATABASE_URL and refuses a URL equal to
DATABASE_URL. Covers: idempotent migrate (twice), concurrent writers (lease + fencing), a real process killed
between the local fsync and the anchor write, duplicate prevention across hosts, and reconciliation through
SimExchange after the anchor database dropped every session (and, when TEST_PG_RESTART_CMD is set, after a real
server restart). The exchange is SIMULATED (tests/sxbet_sim_exchange.py); keys are throwaway.
"""
from __future__ import annotations

import multiprocessing
import os
import shlex
import shutil
import subprocess
import threading

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from app import migrations  # noqa: E402
from signer import anchor as an  # noqa: E402
from signer import orders as od  # noqa: E402
from signer.orders import LedgerError, OrderLedger  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not URL, reason="requires dedicated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(bool(URL) and URL == os.environ.get("DATABASE_URL"),
                       reason="TEST_DATABASE_URL must not be the production DATABASE_URL"),
]

NOW = 1_800_000_000


def _connect(**kw):
    import psycopg
    return psycopg.connect(URL, connect_timeout=5, **kw)


@pytest.fixture(scope="module", autouse=True)
def schema():
    # WP-004: bridge and signer are independent series; the signer series is applied explicitly
    # (throwaway test database only, never by the bridge runner).
    migrations.apply_migrations(_connect)
    migrations.apply_migrations(_connect, series=migrations.SIGNER)


def _store():
    return an.PostgresAnchorStore(_connect, holder="itest-e2e")


def _kill_lease_sessions(ledger_id):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = %s",
                    ("fce-signer-lease:" + ledger_id[:40],))
        return cur.fetchone()[0]


def _ack(owner, led_id, anchored, local_seq, local_head, now=NOW):
    msg = an.ledger_head_ack_message(ledger_id=led_id, anchored_seq=anchored.seq, anchored_head_hash=anchored.head_hash,
                                     local_seq=local_seq, local_head_hash=local_head, issued_at=now, expires_at=now + 60)
    return an.LedgerHeadAck(led_id, anchored.seq, anchored.head_hash, local_seq, local_head, now, now + 60,
                            owner.sign(msg))


def _local_head(path):
    import hashlib
    lines = open(path, encoding="ascii").read().splitlines()
    return len(lines), "sha256:" + hashlib.sha256(lines[-1].encode("ascii")).hexdigest()


def test_migrate_twice_is_idempotent_and_tracked():
    for _ in range(2):
        migrations.apply_migrations(_connect)
        migrations.apply_migrations(_connect, series=migrations.SIGNER)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT version, name, sha256 FROM signer_schema_migrations ORDER BY version")
        signer_rows = cur.fetchall()
        cur.execute("SELECT count(*) FROM schema_migrations WHERE name LIKE '%%signer%%'")
        signer_in_bridge = cur.fetchone()[0]
    expected = [(m.version, m.name, m.checksum) for m in migrations.load_migrations(migrations.SIGNER.directory)]
    assert signer_rows == expected and signer_rows[-1][1] == "0001_signer_ledger_anchor.sql"
    assert signer_in_bridge == 0     # signer DDL is never tracked in the bridge series


def test_concurrent_writers_exactly_one_lease_then_fenced_takeover(tmp_path):
    from sxbet_e2e_kit import Stack
    s = Stack(tmp_path, anchor=_store())
    s.place("order-key-PG01")
    led_id, token = s.ledger.ledger_id, s.ledger.fencing_token
    hosts = []
    for n in range(6):                                    # six other "hosts", each with its own copy of the file
        d = tmp_path / f"host{n}"
        d.mkdir()
        shutil.copy(tmp_path / "ledger.jsonl", d / "ledger.jsonl")
        hosts.append(d)
    barrier = threading.Barrier(len(hosts))
    out = []

    def go(d):
        barrier.wait()
        try:
            led = OrderLedger.open(str(d / "ledger.jsonl"), signer_id=s.ledger.signer_id,
                                   signer_address=s.ledger.signer_address, clock=s.clock, anchor=_store())
            out.append(("OPEN", led))
        except LedgerError as exc:
            out.append((exc.code, None))

    ts = [threading.Thread(target=go, args=(d,)) for d in hosts]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert [c for c, _ in out] == ["LEDGER_LOCKED"] * 6    # the live writer keeps the lease
    assert _kill_lease_sessions(led_id) == 1               # its database session dies
    winners = []

    def go2(d):
        barrier.wait()
        try:
            winners.append(OrderLedger.open(str(d / "ledger.jsonl"), signer_id=s.ledger.signer_id,
                                            signer_address=s.ledger.signer_address, clock=s.clock, anchor=_store()))
        except LedgerError as exc:
            assert exc.code == "LEDGER_LOCKED"

    ts = [threading.Thread(target=go2, args=(d,)) for d in hosts]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert len(winners) == 1 and winners[0].fencing_token == token + 1
    with pytest.raises(Exception) as e:                    # the stale writer is fenced at its next append
        s.place("order-key-PG02")
    assert getattr(e.value, "code", None) == "LEDGER_FENCED"   # SignerRefused(LEDGER_FENCED): nothing signed
    assert s.ledger.poisoned and s.sim.accepted_count("order-key-PG02") == 0
    winners[0].close()
    s.ledger.close()


def _crash_child(tmp, signer_id, address):
    """A real signer process that is KILLED (os._exit) after the local fsync, before the anchor advance."""
    store = _store()
    led = OrderLedger.open(str(tmp / "ledger.jsonl"), signer_id=signer_id, signer_address=address,
                           clock=lambda: NOW, anchor=store)

    def die(*a, **kw):
        os._exit(9)

    store.advance = die
    led.authorize(client_order_id="order-key-PG10", intent_hash="sha256:" + "1" * 64, order_id="0x" + "5a" * 32,
                  market_hash="0x" + "ab" * 32, event_id="L1", stake=3_000_000, expiry=NOW + 3600,
                  approval_nonce="nonce-pg-crash-000001")


def test_process_killed_mid_append_restart_needs_owner_ack_no_lost_reservation(tmp_path):
    from sxbet_e2e_kit import Stack
    s = Stack(tmp_path, anchor=_store())
    s.place("order-key-PG03")
    sid, addr = s.ledger.signer_id, s.ledger.signer_address
    s.ledger.close()
    p = multiprocessing.get_context("fork").Process(target=_crash_child, args=(tmp_path, sid, addr))
    p.start()
    p.join(30)
    assert p.exitcode == 9
    with pytest.raises(LedgerError) as e:
        s.restart()
    assert e.value.code == "LEDGER_LOCAL_AHEAD_OF_ANCHOR"     # the killed process left one unanchored line
    anchored = _store().head(f"{sid}:{addr}")
    seq, head = _local_head(tmp_path / "ledger.jsonl")
    assert seq == anchored.seq + 1
    s.restart(owner_ack=_ack(s.owner, f"{sid}:{addr}", anchored, seq, head))  # owner reviewed the extra line
    assert s.state("order-key-PG10") == od.AUTHORIZED and s.state("order-key-PG03") == od.SUBMITTED
    assert s.exposure().total == 5_000_000 + 3_000_000           # nothing lost, nothing released
    assert _store().head(s.ledger.ledger_id).seq == seq
    s.ledger.close()


def test_duplicate_prevention_stale_copy_cannot_reaccept(tmp_path):
    from sxbet_e2e_kit import Stack
    s = Stack(tmp_path, anchor=_store())
    stale = tmp_path / "stale"
    stale.mkdir()
    shutil.copy(tmp_path / "ledger.jsonl", stale / "ledger.jsonl")    # a host with an old copy of the ledger
    i, sg, _ = s.place("order-key-PG04")
    s.ledger.close()
    with pytest.raises(LedgerError) as e:                              # it can never replay from the old copy
        OrderLedger.open(str(stale / "ledger.jsonl"), signer_id=s.ledger.signer_id,
                         signer_address=s.ledger.signer_address, clock=s.clock, anchor=_store())
    assert e.value.code == "LEDGER_ANCHOR_AHEAD_OF_LOCAL"
    with pytest.raises(LedgerError) as e:
        OrderLedger.create(str(stale / "fresh.jsonl"), signer_id=s.ledger.signer_id,
                           signer_address=s.ledger.signer_address, clock=s.clock, anchor=_store())
    assert e.value.code == "LEDGER_ANCHOR_EXISTS"
    import psycopg
    with pytest.raises(psycopg.Error, match="SIGNER_LEDGER_ANCHOR_SEQ_NOT_MONOTONIC"):
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO signer_ledger_anchors (ledger_id, seq, head_hash, fencing_token) "
                        "SELECT ledger_id, 2, %s, fencing_token FROM signer_ledger_writer_leases WHERE ledger_id = %s",
                        ("sha256:" + "9" * 64, s.ledger.ledger_id))
    s.restart()
    assert s.gw.submit(sg, i)["sent"] is False and s.sim.accepted_count("order-key-PG04") == 1
    s.ledger.close()


def test_reconciliation_after_anchor_database_restart(tmp_path):
    from sxbet_e2e_kit import Stack
    s = Stack(tmp_path, anchor=_store())
    i = s.intent("order-key-PG05")
    sg = s.sign(i)
    s.sim.inject("POST", "/orders-v3", "timeout_after")             # accepted, reply lost
    assert s.gw.submit(sg, i)["state"] == od.UNKNOWN
    cmd = os.environ.get("TEST_PG_RESTART_CMD")
    if cmd:
        subprocess.run(shlex.split(cmd), check=True, timeout=60)     # a real server restart (owner's test box)
    else:
        assert _kill_lease_sessions(s.ledger.ledger_id) == 1          # every session of the signer dropped
    s.restart()                                                      # new lease (token + 1), head verified
    assert s.state("order-key-PG05") == od.UNKNOWN and s.exposure().total == 5_000_000
    assert s.gw.reconcile("order-key-PG05")["state"] == od.OPEN       # found on the exchange
    assert s.gw.submit(sg, i)["sent"] is False and s.sim.accepted_count("order-key-PG05") == 1
    s.sim.fill_maker_order(sg["order_id"], 5_000_000)
    s.gw.reconcile("order-key-PG05")
    s.sim.settle("0x" + "ab" * 32, 1)
    s.gw.reconcile("order-key-PG05")
    assert s.ledger.get("order-key-PG05").realized_pnl == 7_500_000
    seq, head = _local_head(tmp_path / "ledger.jsonl")
    assert _store().head(s.ledger.ledger_id) == an.AnchorHead(seq, head, s.ledger.fencing_token)
    s.ledger.close()
