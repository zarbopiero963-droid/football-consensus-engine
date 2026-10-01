import asyncio
from unittest.mock import AsyncMock

import pytest
import httpx

from bridge import session_leases
from bridge.browser_use import BrowserUseBridge, Session


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture(autouse=True)
def claim_heartbeat(monkeypatch):
    monkeypatch.setattr(session_leases, 'renew_stop', lambda *args: None)


def test_stop_failure_preserves_owned_session_and_retries(monkeypatch):
    bridge = BrowserUseBridge()
    bridge._sessions['owned'] = Session('owned', 'memory-only-capability', None)
    monkeypatch.setattr(session_leases, 'claim_stop', lambda *args: True)
    outcomes = []
    monkeypatch.setattr(session_leases, 'finish', lambda *args: outcomes.append(args))
    remote = AsyncMock(side_effect=RuntimeError('provider failed'))
    monkeypatch.setattr(bridge, '_stop_remote', remote)
    with pytest.raises(RuntimeError, match='browser stop failed'):
        run(bridge.stop('owned'))
    assert 'owned' in bridge._sessions and bridge._sessions['owned'].closing
    assert outcomes[-1][2] is False
    remote.side_effect = None
    assert run(bridge.stop('owned'))['status'] == 'stopped'
    assert not bridge._sessions and outcomes[-1][2] is True


def test_busy_session_is_excluded_and_only_claimed_ids_are_stopped(monkeypatch):
    bridge = BrowserUseBridge()
    session = Session('busy', 'memory-only', None)
    bridge._sessions['busy'] = session
    claims = []
    monkeypatch.setattr(session_leases, 'claim_stale', lambda owner, seconds, excluded: claims.append(excluded) or ['orphan'])
    monkeypatch.setattr(session_leases, 'finish', lambda *args: None)
    remote = AsyncMock()
    monkeypatch.setattr(bridge, '_stop_remote', remote)

    async def check():
        async with session.operation_lock:
            result = await bridge.reap()
        assert result == {'stopped': ['orphan'], 'failed': []}

    run(check())
    assert claims == [['busy']]
    remote.assert_awaited_once_with('orphan')


def test_durable_claim_can_reap_after_worker_restart(monkeypatch):
    bridge = BrowserUseBridge()
    assert not bridge._sessions
    monkeypatch.setattr(session_leases, 'claim_stale', lambda *args: ['old-worker-session'])
    monkeypatch.setattr(session_leases, 'finish', lambda *args: None)
    remote = AsyncMock()
    monkeypatch.setattr(bridge, '_stop_remote', remote)
    assert run(bridge.reap())['stopped'] == ['old-worker-session']
    remote.assert_awaited_once_with('old-worker-session')


def test_operation_renews_before_and_after_and_preserves_busy_lock(monkeypatch):
    bridge = BrowserUseBridge()
    bridge._sessions['owned'] = Session('owned', 'memory-only', None)
    renewals = []
    monkeypatch.setattr(session_leases, 'renew', lambda *args: renewals.append(args))

    async def check():
        async with bridge._operation('owned') as session:
            assert session.operation_lock.locked()
            raise ValueError('operation failed')

    with pytest.raises(ValueError):
        run(check())
    assert len(renewals) == 2
    assert not bridge._sessions['owned'].operation_lock.locked()


def test_stolen_lease_fails_before_page_operation(monkeypatch):
    bridge = BrowserUseBridge()
    bridge._sessions['owned'] = Session('owned', 'memory-only', None)
    monkeypatch.setattr(session_leases, 'renew', lambda *args: (_ for _ in ()).throw(RuntimeError('ownership lost')))
    with pytest.raises(RuntimeError, match='ownership lost'):
        run(bridge.network_responses('owned'))


@pytest.mark.parametrize('seconds', [0, 59, 21601])
def test_reaper_rejects_unsafe_idle_threshold(seconds):
    with pytest.raises(ValueError):
        run(BrowserUseBridge().reap(seconds))


def test_cleanup_failure_is_visible_and_drops_capability_references(monkeypatch):
    bridge = BrowserUseBridge()
    session = Session('owned', 'memory-only', None)
    session.playwright = AsyncMock()
    session.playwright.stop.side_effect = RuntimeError('cleanup failed')
    bridge._sessions['owned'] = session
    monkeypatch.setattr(session_leases, 'claim_stop', lambda *args: True)
    monkeypatch.setattr(session_leases, 'finish', lambda *args: None)
    monkeypatch.setattr(bridge, '_stop_remote', AsyncMock())
    with pytest.raises(RuntimeError, match='cleanup failed'):
        run(bridge.stop('owned'))
    assert session.playwright is session.browser is session.context is session.page is None
    assert 'owned' not in bridge._sessions


@pytest.mark.parametrize('status,terminal,expected_error', [(200, 'stopped', False), (200, 'active', True),
                                                         (404, 'active', False), (401, 'active', True)])
def test_http_stop_requires_terminal_confirmation(monkeypatch, status, terminal, expected_error):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={'status': terminal}))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(transport=transport, **kwargs))
    bridge = BrowserUseBridge()
    monkeypatch.setattr(bridge, '_headers', lambda: {})
    if expected_error:
        with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
            run(bridge._stop_remote('owned'))
    else:
        run(bridge._stop_remote('owned'))


def test_reclaimed_stop_never_contacts_provider(monkeypatch):
    bridge = BrowserUseBridge()
    monkeypatch.setattr(session_leases, 'renew_stop', lambda *args: (_ for _ in ()).throw(RuntimeError('ownership lost')))
    remote = AsyncMock()
    monkeypatch.setattr(bridge, '_stop_remote', remote)
    with pytest.raises(RuntimeError, match='ownership lost'):
        run(bridge._stop_claimed('owned'))
    remote.assert_not_awaited()
