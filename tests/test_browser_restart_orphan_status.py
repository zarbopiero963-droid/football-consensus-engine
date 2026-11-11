"""A browser session interrupted by a worker restart is never presented as usable.

Render Free spins the service down after ~15 min idle and every deploy replaces the
worker: the in-memory CDP capability of a Browser Use session dies with the process,
while its durable lease row can still read ACTIVE (after a hard kill, until the reaper
claims it). Status and inventory must say explicitly that such a session is NOT usable
by this worker, and any operation on it must fail with a fixed code, never "recover".
"""
import asyncio

import pytest

from bridge import session_leases
from bridge.browser_use import BrowserUseBridge, Session, browser_error_code


def run(coroutine):
    return asyncio.run(coroutine)


def _row(session_id, state):
    return {
        'session_id': session_id,
        'state': state,
        'created_at': None,
        'last_activity_at': None,
        'lease_until': None,
        'updated_at': None,
        'stop_error': '',
    }


def test_active_lease_from_previous_worker_is_reported_not_usable(monkeypatch):
    # A fresh bridge = the restarted worker: no in-memory sessions.
    restarted = BrowserUseBridge()
    row = _row('bu-from-dead-worker', 'ACTIVE')
    monkeypatch.setattr(session_leases, 'get_session', lambda *args: row)
    monkeypatch.setattr(session_leases, 'list_sessions', lambda *args: [row])

    status = run(restarted.session_status('bu-from-dead-worker'))
    inventory = run(restarted.list_sessions())

    assert status['state'] == 'ACTIVE'  # the durable record is reported as it is
    assert status['local'] is False
    assert status['usable'] is False
    assert status['usability'] == 'NOT_USABLE_NOT_IN_THIS_WORKER'
    assert inventory['sessions'][0]['usable'] is False
    assert inventory['sessions'][0]['usability'] == 'NOT_USABLE_NOT_IN_THIS_WORKER'


def test_operation_on_interrupted_session_fails_with_fixed_code(monkeypatch):
    monkeypatch.setenv('BROWSER_TOOLS_ENABLED', '1')
    restarted = BrowserUseBridge()
    with pytest.raises(KeyError) as excinfo:
        run(restarted.dom_text('bu-from-dead-worker'))
    assert browser_error_code(excinfo.value) == 'SESSION_NOT_FOUND'


def test_live_local_session_is_usable_and_stopped_or_closing_is_not(monkeypatch):
    bridge = BrowserUseBridge()
    bridge._sessions['live'] = Session('live', 'memory-only-capability', None)
    bridge._sessions['closing'] = Session('closing', 'memory-only-capability', None)
    bridge._sessions['closing'].closing = True
    rows = {
        'live': _row('live', 'ACTIVE'),
        'closing': _row('closing', 'STOPPING'),
        'gone': _row('gone', 'STOPPED'),
        'failed': _row('failed', 'STOP_FAILED'),
    }
    monkeypatch.setattr(session_leases, 'get_session', lambda sid: rows[sid])

    live = run(bridge.session_status('live'))
    closing = run(bridge.session_status('closing'))
    gone = run(bridge.session_status('gone'))
    failed = run(bridge.session_status('failed'))

    assert (live['usable'], live['usability']) == (True, 'USABLE')
    assert (closing['usable'], closing['usability']) == (False, 'NOT_USABLE_STOPPING')
    assert (gone['usable'], gone['usability']) == (False, 'NOT_USABLE_STOPPED')
    assert (failed['usable'], failed['usability']) == (False, 'NOT_USABLE_STOP_FAILED')
    assert 'memory-only-capability' not in repr([live, closing, gone, failed])
