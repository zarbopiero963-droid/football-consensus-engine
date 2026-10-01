from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import api
from app.decision_requests import request_fingerprint, request_lock_id, validate_key


@pytest.mark.parametrize('key', ['', ' ', 'a b', 'a\n', 'é', 'x' * 129, None])
def test_invalid_keys_are_rejected_before_database_access(monkeypatch, key):
    connect = MagicMock()
    monkeypatch.setattr(api, 'connect', connect)
    with pytest.raises(HTTPException) as exc:
        api.create_decision_batch(date(2026, 10, 1), api.DecisionRequest(), idempotency_key=key)
    assert exc.value.status_code == 422
    connect.assert_not_called()


def test_key_and_payload_identity():
    assert validate_key('daily:2026-10-01_A.1')
    assert request_lock_id('a') == request_lock_id('a') != request_lock_id('b')
    day = date(2026, 10, 1)
    a = request_fingerprint(day, api.DecisionRequest(label='daily', strategies=['S2']))
    assert a == request_fingerprint(day, api.DecisionRequest(strategies=['S2'], label='daily'))
    for request in [api.DecisionRequest(label='other', strategies=['S2']),
                    api.DecisionRequest(label='daily', strategies=['S4']),
                    api.DecisionRequest(label='daily', mode='signals', strategies=['S2'])]:
        assert a != request_fingerprint(day, request)
    assert a != request_fingerprint(date(2026, 10, 2), api.DecisionRequest(label='daily', strategies=['S2']))


def test_post_routes_require_key_without_creating_a_batch(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(api, 'connect', connect)
    client = TestClient(api.app)
    for path in ['/decisions/today', '/decisions/date/2026-10-01']:
        assert client.post(path, json={}).status_code == 422
        assert client.post(path, json={}, headers={'Idempotency-Key': 'a b'}).status_code == 422
    connect.assert_not_called()


def test_retry_replays_original_response_without_refreezing(monkeypatch):
    req = api.DecisionRequest(label='daily')
    day = date(2026, 10, 1)
    saved = {'batch_id': 'original', 'matches': [{'picked_odds': 13.83}], 'markets': []}
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {'request_sha256': request_fingerprint(day, req), 'response': saved}
    monkeypatch.setattr(api, 'connect', lambda: conn)
    freeze = MagicMock()
    monkeypatch.setattr(api, '_freeze_new_batch', freeze)
    sync = MagicMock()
    monkeypatch.setattr(api, 'synchronize_strategy_simulations', sync)
    assert api.create_decision_batch(day, req, idempotency_key='daily') == saved
    freeze.assert_not_called()
    sync.assert_called_once()
    conn.commit.assert_called_once()


def test_reused_key_with_changed_payload_never_commits(monkeypatch):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {'request_sha256': 'other', 'response': {}}
    monkeypatch.setattr(api, 'connect', lambda: conn)
    with pytest.raises(HTTPException) as exc:
        api.create_decision_batch(date(2026, 10, 1), api.DecisionRequest(), idempotency_key='daily')
    assert exc.value.status_code == 409
    conn.commit.assert_not_called()
