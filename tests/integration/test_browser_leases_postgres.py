import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from unittest.mock import AsyncMock
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL
import pytest

from app.migrations import apply_migrations
from bridge import session_leases
from bridge.browser_use import BrowserUseBridge


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL required for real PostgreSQL integration')
    schema = 'browser_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f'-c search_path={schema}')

    apply_migrations(connect)
    monkeypatch.setattr(session_leases, 'connect', connect)
    return connect


def expire(database, session_id):
    with database() as conn:
        conn.execute('''UPDATE browser_session_leases
                        SET lease_until=NOW()-INTERVAL '1 second',last_activity_at=NOW()-INTERVAL '10 minutes'
                        WHERE session_id=%s''', (session_id,))


def state(database, session_id):
    with database() as conn:
        return conn.execute('SELECT * FROM browser_session_leases WHERE session_id=%s', (session_id,)).fetchone()


def test_only_registered_idle_expired_leases_are_claimed(database):
    for sid in ['idle', 'recent', 'in-flight', 'excluded']:
        session_leases.register(sid, 'old-worker')
    for sid in ['idle', 'excluded']:
        expire(database, sid)
    with database() as conn:
        conn.execute("UPDATE browser_session_leases SET last_activity_at=NOW()-INTERVAL '10 minutes' WHERE session_id='in-flight'")
    assert session_leases.claim_stale('reaper', exclude=['excluded']) == ['idle']
    assert state(database, 'idle')['owner_id'] == 'reaper'
    assert state(database, 'recent')['state'] == state(database, 'in-flight')['state'] == 'ACTIVE'
    assert state(database, 'excluded')['state'] == 'ACTIVE'
    assert session_leases.claim_stale('second-reaper') == ['excluded']


def test_concurrent_reapers_claim_one_session_once(database):
    session_leases.register('orphan', 'dead-worker')
    expire(database, 'orphan')
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda i: session_leases.claim_stale('reaper-' + str(i)), range(8)))
    assert sum(len(ids) for ids in claims) == 1
    assert state(database, 'orphan')['state'] == 'STOPPING'


def test_renewed_lease_prevents_reclaim_and_old_owner_cannot_renew(database):
    session_leases.register('owned', 'worker')
    expire(database, 'owned')
    session_leases.renew('owned', 'worker')
    assert session_leases.claim_stale('reaper') == []
    expire(database, 'owned')
    assert session_leases.claim_stale('reaper') == ['owned']
    with pytest.raises(RuntimeError, match='no longer active'):
        session_leases.renew('owned', 'worker')
    with pytest.raises(RuntimeError, match='ownership changed'):
        session_leases.finish('owned', 'worker', True)


def test_failed_remote_stop_is_durable_and_retryable_after_restart(database, monkeypatch):
    session_leases.register('orphan', 'dead-worker')
    expire(database, 'orphan')
    first = BrowserUseBridge()
    monkeypatch.setattr(first, '_stop_remote', AsyncMock(side_effect=RuntimeError('provider unavailable')))
    assert asyncio.run(first.reap())['failed']
    failed = state(database, 'orphan')
    assert failed['state'] == 'STOP_FAILED' and failed['stop_error'] == 'RuntimeError'
    assert session_leases.claim_stale('too-soon') == []
    expire(database, 'orphan')
    restarted = BrowserUseBridge()
    stop = AsyncMock()
    monkeypatch.setattr(restarted, '_stop_remote', stop)
    assert asyncio.run(restarted.reap()) == {'stopped': ['orphan'], 'failed': []}
    stop.assert_awaited_once_with('orphan')
    assert state(database, 'orphan')['state'] == 'STOPPED'
    assert session_leases.claim_stale('later') == []


def test_inventory_and_status_queries_use_only_durable_bridge_rows(database):
    session_leases.register('browser-use-owned', 'worker-a')
    session_leases.register('cloak-owned', 'worker-b')
    with database() as conn:
        conn.execute("UPDATE browser_session_leases SET state='STOPPED' WHERE session_id='browser-use-owned'")

    open_rows = session_leases.list_sessions()
    all_rows = session_leases.list_sessions(include_stopped=True)
    cloak = session_leases.get_session('cloak-owned')

    assert [row['session_id'] for row in open_rows] == ['cloak-owned']
    assert {row['session_id'] for row in all_rows} == {'browser-use-owned', 'cloak-owned'}
    assert cloak['state'] == 'ACTIVE'
    assert 'owner_id' not in cloak
    assert session_leases.get_session('missing') is None
    assert session_leases.healthcheck() is True
    summary = session_leases.state_summary()
    assert summary['ACTIVE'] == 1
    assert summary['STOPPED'] == 1
