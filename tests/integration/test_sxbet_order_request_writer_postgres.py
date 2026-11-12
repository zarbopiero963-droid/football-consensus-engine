"""S7 (issue #187): the MCP-side writer of the order-request queue on a THROWAWAY PostgreSQL only.

The MCP role (INSERT + SELECT on signer_order_requests, SELECT on the events) can queue a request idempotently
and read the signer's events, and can never write an event, update or delete anything. Roles are created here on
the throwaway database only (production roles are a WP-009 step).
"""
import json
import os
import pathlib

import pytest

from app import migrations
from bridge.sxbet import order_requests as oreq

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not URL, reason="requires dedicated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(bool(URL) and URL == os.environ.get("DATABASE_URL"),
                       reason="TEST_DATABASE_URL must not be the production DATABASE_URL"),
]
DRAFT = (pathlib.Path(__file__).resolve().parents[2] / "sql" / "drafts" / "signer" / "0002_signer_order_requests.sql")
MCP, SIG = "fce_it_s7_mcp", "fce_it_s7_signer"


def _connect(**kw):
    import psycopg
    return psycopg.connect(URL, connect_timeout=5, **kw)


def as_role(role):
    def connect(**kw):
        conn = _connect(**kw)
        conn.execute(f"SET ROLE {role}")
        conn.commit()
        return conn
    return connect


@pytest.fixture(scope="module", autouse=True)
def schema_and_roles():
    migrations.apply_migrations(_connect, series=migrations.SIGNER)
    with _connect() as conn:
        conn.execute(DRAFT.read_text())
    with _connect(autocommit=True) as conn:
        for role in (MCP, SIG):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
            conn.execute(f"CREATE ROLE {role} NOLOGIN")
        conn.execute(f"GRANT SELECT, INSERT ON signer_order_requests TO {MCP}")
        conn.execute(f"GRANT SELECT ON signer_order_request_events TO {MCP}")
        conn.execute(f"GRANT SELECT ON signer_order_requests TO {SIG}")
        conn.execute(f"GRANT SELECT, INSERT ON signer_order_request_events TO {SIG}")
    yield
    with _connect(autocommit=True) as conn:
        for role in (MCP, SIG):
            conn.execute(f"REVOKE ALL ON signer_order_requests, signer_order_request_events FROM {role}")
            conn.execute(f"DROP ROLE {role}")


def _cancel(key, text="Cancel all orders", now=1_800_000_000):
    return oreq.build_cancel_request(network="testnet", scope="ALL", key=key, command_text=text, now=now)


def test_mcp_role_queues_idempotently_and_reads_events():
    from signer.request_queue import PostgresRequestQueue
    w = oreq.PostgresRequestWriter(as_role(MCP))
    p = _cancel("s7-int-key-0001")
    assert w.submit(p) == ("INSERTED", None)
    outcome, stored = w.submit(_cancel("s7-int-key-0001", now=1_800_000_005))   # retry: new id/time, same order
    assert outcome == "EXISTS" and stored == p and oreq.same_request(stored, _cancel("s7-int-key-0001"))
    outcome, stored = w.submit(_cancel("s7-int-key-0001", text="Cancella tutti gli ordini"))
    assert outcome == "EXISTS" and not oreq.same_request(stored, _cancel("s7-int-key-0001",
                                                                         text="Cancella tutti gli ordini"))
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), max(payload_sha256) FROM signer_order_requests WHERE request_id = %s",
                    (p["request_id"],))
        assert cur.fetchone() == (1, oreq.canonical_sha256(p))
    q = PostgresRequestQueue(as_role(SIG))
    assert q.claim(p["request_id"], "sig-s7")
    q.append_event(p["request_id"], "DONE", {"scope": "ALL", "sent": True, "accepted": True}, "sig-s7")
    events = w.events(p["request_id"])
    assert [e["state"] for e in events] == ["CLAIMED", "DONE"]
    assert oreq.queue_view("CANCEL", events)["state"] == "ACCEPTED"
    assert w.request("f" * 32) is None and w.events("f" * 32) == []


def test_mcp_role_cannot_write_events_or_mutate_requests():
    import psycopg
    w = oreq.PostgresRequestWriter(as_role(MCP))
    p = _cancel("s7-int-key-0002")
    w.submit(p)
    for sql, params in (
            ("INSERT INTO signer_order_request_events (request_id, seq, state, signer_id) VALUES (%s, 1, 'DONE', 'x')",
             (p["request_id"],)),
            ("UPDATE signer_order_requests SET network = 'mainnet' WHERE request_id = %s", (p["request_id"],)),
            ("DELETE FROM signer_order_requests WHERE request_id = %s", (p["request_id"],))):
        with as_role(MCP)() as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(sql, params)


def test_a_failing_queue_never_leaks_the_dsn():
    def broken():
        raise RuntimeError("postgresql://user:secret@host/db")
    w = oreq.PostgresRequestWriter(broken)
    with pytest.raises(RuntimeError):
        w.submit(_cancel("s7-int-key-0003"))
    # The tool layer maps any writer exception to fixed codes (tests/test_sxbet_order_request_tools.py).
    assert "secret" not in repr(oreq.OrderRequestSettings(enabled=True, queue_dsn="postgresql://u:secret@h/d",
                                                         maker="0x" + "a" * 40))
    assert json.dumps(oreq.queue_view("PLACE", []))
