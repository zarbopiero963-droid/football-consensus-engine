"""P5 (issue #187): signer_status on a THROWAWAY PostgreSQL only. The signer role publishes (upsert), the MCP role
can only read it; age is measured on the database clock. Roles are created here only (production: WP-009)."""
import os
import pathlib

import pytest

from app import migrations
from bridge.sxbet import order_requests as oreq
from bridge.sxbet import trading_mode as tm

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not URL, reason="requires dedicated TEST_DATABASE_URL; no production credentials"),
    pytest.mark.skipif(bool(URL) and URL == os.environ.get("DATABASE_URL"),
                       reason="TEST_DATABASE_URL must not be the production DATABASE_URL"),
]
DRAFTS = pathlib.Path(__file__).resolve().parents[2] / "sql" / "drafts" / "signer"
MCP, SIG = "fce_it_p5_mcp", "fce_it_p5_signer"


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
        conn.execute((DRAFTS / "0002_signer_order_requests.sql").read_text())
        conn.execute((DRAFTS / "0003_signer_status.sql").read_text())
        conn.execute((DRAFTS / "0003_signer_status.sql").read_text())          # idempotent
        conn.execute("DELETE FROM signer_status")
    with _connect(autocommit=True) as conn:
        for role in (MCP, SIG):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
            conn.execute(f"CREATE ROLE {role} NOLOGIN")
        conn.execute(f"GRANT SELECT ON signer_status TO {MCP}")
        conn.execute(f"GRANT SELECT, INSERT, UPDATE ON signer_status TO {SIG}")
    yield
    with _connect(autocommit=True) as conn:
        conn.execute("DELETE FROM signer_status")
        for role in (MCP, SIG):
            conn.execute(f"REVOKE ALL ON signer_status FROM {role}")
            conn.execute(f"DROP ROLE {role}")


def _signer_store():
    import os as _os
    _os.environ.setdefault("SXBET_SIGNER_PROCESS", "1")
    from signer.status import PostgresSignerStatus
    return PostgresSignerStatus(as_role(SIG))


def test_publish_then_mcp_reads_fresh_status_and_precheck_passes():
    store = _signer_store()
    store.publish("sig-a", "testnet", {"kill_switch": "DISENGAGED", "live_trading": "AUTHORIZED",
                                       "live_reason": None, "last_cycle_ok": True})
    rows = oreq.PostgresRequestWriter(as_role(MCP)).signer_status("testnet")
    assert len(rows) == 1 and 0 <= rows[0]["age_seconds"] < 30
    assert tm.precheck(rows, max_age_seconds=120, kind="PLACE") is None
    store.publish("sig-a", "testnet", {"kill_switch": "ENGAGED", "live_trading": "AUTHORIZED",
                                       "live_reason": None, "last_cycle_ok": True})       # upsert, one row
    rows = oreq.PostgresRequestWriter(as_role(MCP)).signer_status("testnet")
    assert len(rows) == 1 and tm.precheck(rows, max_age_seconds=120, kind="PLACE") == "KILL_SWITCH_ENGAGED"


def test_stale_status_is_unreachable():
    with _connect() as conn:
        conn.execute("UPDATE signer_status SET updated_at = clock_timestamp() - interval '1 hour' "
                     "WHERE signer_id = 'sig-a'")
    rows = oreq.PostgresRequestWriter(as_role(MCP)).signer_status("testnet")
    assert tm.precheck(rows, max_age_seconds=120, kind="CANCEL") == "SIGNER_UNREACHABLE"


def test_mcp_role_cannot_write_and_constraints_hold():
    import psycopg
    with pytest.raises(psycopg.Error):
        with as_role(MCP)() as conn:
            conn.execute("UPDATE signer_status SET kill_switch = 'DISENGAGED'")
    with pytest.raises(psycopg.Error):
        with as_role(MCP)() as conn:
            conn.execute("INSERT INTO signer_status (signer_id, network, kill_switch, live_trading, last_cycle_ok) "
                         "VALUES ('x', 'testnet', 'DISENGAGED', 'AUTHORIZED', true)")
    with pytest.raises(psycopg.Error):        # AUTHORIZED with a reason is inconsistent
        with _connect() as conn:
            conn.execute("INSERT INTO signer_status (signer_id, network, kill_switch, live_trading, live_reason, "
                         "last_cycle_ok) VALUES ('y', 'testnet', 'DISENGAGED', 'AUTHORIZED', 'X', true)")
    with pytest.raises(psycopg.Error):
        with _connect() as conn:
            conn.execute("INSERT INTO signer_status (signer_id, network, kill_switch, live_trading, last_cycle_ok) "
                         "VALUES ('z', 'testnet', 'MAYBE', 'NOT_AUTHORIZED', true)")
