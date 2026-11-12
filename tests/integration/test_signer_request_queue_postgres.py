"""S6 (issue #187): signer series migration 0002 and PostgresRequestQueue on a THROWAWAY PostgreSQL only.

Append-only enforced by the database, one claim per request, concurrent claimers, recovery listing, and the full
service path (MCP insert -> signer claim/sign/submit on the simulated exchange -> events) on real PostgreSQL.
"""
import json
import os
import pathlib
import threading

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("cryptography")

from app import migrations  # noqa: E402
from signer.request_queue import PostgresRequestQueue, canonical_sha256  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not URL, reason="requires dedicated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(bool(URL) and URL == os.environ.get("DATABASE_URL"),
                       reason="TEST_DATABASE_URL must not be the production DATABASE_URL"),
]


def _connect(**kw):
    import psycopg
    return psycopg.connect(URL, connect_timeout=5, **kw)


DRAFT = (pathlib.Path(__file__).resolve().parents[2] / "sql" / "drafts" / "signer" / "0002_signer_order_requests.sql")


def _apply_draft():
    # Draft 0002 is not in the signer series yet (WP-009 coordination): applied here to a throwaway DB only.
    with _connect() as conn:
        conn.execute(DRAFT.read_text())


@pytest.fixture(scope="module", autouse=True)
def schema():
    migrations.apply_migrations(_connect, series=migrations.SIGNER)
    _apply_draft()


def _insert(payload):
    """What the MCP side writes (its role: INSERT on requests only)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO signer_order_requests (request_id, kind, payload, payload_sha256, network) "
                    "VALUES (%s, %s, %s::jsonb, %s, %s)", (payload["request_id"], payload["kind"],
                                                          json.dumps(payload), canonical_sha256(payload),
                                                          payload["network"]))


def _payload(rid):
    return {"schema": "sxbet.order_request.v1", "request_id": rid, "kind": "CANCEL", "network": "testnet",
            "created_at": 1, "scope": "ALL", "command": {"command_id": "c", "command_text": "Cancel all orders",
                                                         "command_text_sha256": "x", "received_at": 1,
                                                         "client_ref_sha256": "y"}}


def test_migration_0002_is_idempotent_and_append_only():
    _apply_draft()  # idempotent: IF NOT EXISTS / CREATE OR REPLACE
    rid = "a" * 32
    _insert(_payload(rid))
    q = PostgresRequestQueue(_connect)
    assert q.claim(rid, "sig-it")
    import psycopg
    for sql in ("UPDATE signer_order_requests SET kind = 'PLACE'", "DELETE FROM signer_order_requests",
                "TRUNCATE signer_order_requests CASCADE", "UPDATE signer_order_request_events SET state = 'DONE'",
                "DELETE FROM signer_order_request_events", "TRUNCATE signer_order_request_events"):
        with pytest.raises(psycopg.Error) as e, _connect() as conn:
            conn.execute(sql)
        assert "APPEND_ONLY" in str(e.value)


def test_one_claim_under_concurrency():
    rid = "b" * 32
    _insert(_payload(rid))
    q = PostgresRequestQueue(_connect)
    wins = []
    ts = [threading.Thread(target=lambda n=n: wins.append(q.claim(rid, f"sig-{n}"))) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1
    assert [e["state"] for e in q.events(rid)] == ["CLAIMED"]


def test_pending_unfinished_and_events_order():
    rid = "c" * 32
    _insert(_payload(rid))
    q = PostgresRequestQueue(_connect)
    assert rid in [r["request_id"] for r in q.pending("testnet", 100)]
    assert rid not in [r["request_id"] for r in q.pending("mainnet", 100)]
    assert q.claim(rid, "sig-x")
    assert rid not in [r["request_id"] for r in q.pending("testnet", 100)]
    assert [r["request_id"] for r in q.unfinished("testnet", "sig-x") if r["request_id"] == rid] == [rid]
    assert q.append_event(rid, "SIGNED", {"order_id": "0x" + "1" * 64}, "sig-x") == 2
    assert q.append_event(rid, "DONE", {"code": "X"}, "sig-x") == 3
    assert rid not in [r["request_id"] for r in q.unfinished("testnet", "sig-x")]
    assert [e["state"] for e in q.events(rid)] == ["CLAIMED", "SIGNED", "DONE"]


def test_full_service_path_on_real_postgres(tmp_path):
    from signer.service import SignerService
    from sxbet_e2e_kit import SIGNER_ID, Stack
    from sxbet_queue_kit import place_request
    s = Stack(tmp_path)
    i = s.intent("order-key-PGQ01")
    ctx = s.context(i)
    s.signer.config.market_data.world = ctx
    p = place_request(i, s.command(i, ctx), created_at=int(s.clock()))
    _insert(p)
    q = PostgresRequestQueue(_connect)
    svc = SignerService(q, s.signer, s.gw, s.cx, network="mainnet", signer_id=SIGNER_ID, clock=s.clock)
    out = svc.process_once(limit=50)
    assert [r["event"] for r in out if r["request_id"] == p["request_id"]] == ["SUBMITTED"]
    assert [e["state"] for e in q.events(p["request_id"])] == ["CLAIMED", "SIGNED", "SUBMITTED"]
    assert p["command"]["command_text"] not in json.dumps(q.events(p["request_id"]))


def test_review_events_after_terminal_and_unclaimed_writers_are_refused():
    import psycopg
    rid = "d" * 32
    _insert(_payload(rid))
    q = PostgresRequestQueue(_connect)
    with pytest.raises(psycopg.Error) as e:                    # nobody claimed it: no outcome can be forged
        q.append_event(rid, "DONE", {"code": "FORGED"}, "intruder")
    assert "NOT_CLAIMED_BY_WRITER" in str(e.value)
    assert q.claim(rid, "sig-y")
    with pytest.raises(psycopg.Error):                         # another identity cannot write outcomes either
        q.append_event(rid, "SUBMITTED", {}, "intruder")
    q.append_event(rid, "REFUSED", {"code": "X"}, "sig-y")
    with pytest.raises(psycopg.Error) as e:
        q.append_event(rid, "SUBMITTED", {}, "sig-y")
    assert "ALREADY_TERMINAL" in str(e.value)


def test_review_least_privilege_roles_on_a_throwaway_database():
    """What WP-009 must provide before the draft is promoted (created here on the throwaway DB only)."""
    import psycopg
    with _connect(autocommit=True) as conn:
        for role in ("fce_it_mcp_queue", "fce_it_signer_queue"):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
            conn.execute(f"CREATE ROLE {role} NOLOGIN")
        conn.execute("GRANT INSERT ON signer_order_requests TO fce_it_mcp_queue")
        conn.execute("GRANT SELECT ON signer_order_request_events TO fce_it_mcp_queue")
        conn.execute("GRANT SELECT ON signer_order_requests TO fce_it_signer_queue")
        conn.execute("GRANT SELECT, INSERT ON signer_order_request_events TO fce_it_signer_queue")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SET ROLE fce_it_mcp_queue")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO signer_order_request_events (request_id, seq, state, signer_id) "
                            "VALUES (%s, 1, 'CLAIMED', 'x')", ("a" * 32,))
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SET ROLE fce_it_signer_queue")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO signer_order_requests (request_id, kind, payload, payload_sha256, network) "
                            "VALUES (%s, 'CANCEL', '{}'::jsonb, %s, 'testnet')", ("9" * 32, "sha256:" + "0" * 64))
    finally:
        with _connect(autocommit=True) as conn:
            for role in ("fce_it_mcp_queue", "fce_it_signer_queue"):
                conn.execute(f"REVOKE ALL ON signer_order_requests, signer_order_request_events FROM {role}")
                conn.execute(f"DROP ROLE {role}")


def test_review_67e80eb_p2_a_queue_works_under_the_least_privilege_roles():
    """Positive path as the real roles: MCP inserts and reads events; signer claims, appends, lists, reads."""
    import psycopg
    mcp, sig = "fce_it_mcp_q2", "fce_it_signer_q2"
    with _connect(autocommit=True) as conn:
        for role in (mcp, sig):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
            conn.execute(f"CREATE ROLE {role} NOLOGIN")
        conn.execute(f"GRANT INSERT ON signer_order_requests TO {mcp}")
        conn.execute(f"GRANT SELECT ON signer_order_request_events TO {mcp}")
        conn.execute(f"GRANT SELECT ON signer_order_requests TO {sig}")
        conn.execute(f"GRANT SELECT, INSERT ON signer_order_request_events TO {sig}")

    def as_role(role):
        def connect(**kw):
            conn = _connect(**kw)
            conn.execute(f"SET ROLE {role}")
            conn.commit()
            return conn
        return connect
    try:
        rid = "7" * 32
        p = _payload(rid)
        with as_role(mcp)() as conn:
            conn.execute("INSERT INTO signer_order_requests (request_id, kind, payload, payload_sha256, network) "
                         "VALUES (%s, %s, %s::jsonb, %s, %s)", (rid, p["kind"], json.dumps(p), canonical_sha256(p),
                                                                p["network"]))
        q = PostgresRequestQueue(as_role(sig))
        assert rid in [r["request_id"] for r in q.pending("testnet", 100)]
        assert q.claim(rid, "sig-roles")
        assert q.append_event(rid, "UNKNOWN", {"code": "X"}, "sig-roles") == 2
        assert [r["request_id"] for r in q.unfinished("testnet", "sig-roles") if r["request_id"] == rid] == [rid]
        assert q.append_event(rid, "DONE", {"code": "Y"}, "sig-roles") == 3
        assert q.claim(rid, "sig-other") is False              # late claim on a finished request: False, no raise
        with as_role(mcp)() as conn, conn.cursor() as cur:
            cur.execute("SELECT state FROM signer_order_request_events WHERE request_id = %s ORDER BY seq", (rid,))
            assert [r[0] for r in cur.fetchall()] == ["CLAIMED", "UNKNOWN", "DONE"]
    finally:
        with _connect(autocommit=True) as conn:
            for role in (mcp, sig):
                conn.execute(f"REVOKE ALL ON signer_order_requests, signer_order_request_events FROM {role}")
                conn.execute(f"DROP ROLE {role}")
