from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from app import api
from app.runtime_health import evaluate


COUNTERS = {name: 0 for name in ('snapshots','due_tickets','pending_due_tickets',
    'illegal_market_settlements','illegal_strategy_settlements','nonproduction_market_sources',
    'pilot_or_unknown_snapshots','pilot_or_unknown_tickets','unsafe_production_time',
    'broken_pilot_parsers','zero_row_pilot_parsers','failed_pilot_transports',
    'unresolved_pilot_timezone_rows')}
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
WORKERS = {'collector': NOW, 'settlement': NOW}


def test_healthy_observed_runtime_passes_and_no_due_matches_is_not_zero_ratio():
    report = evaluate({**COUNTERS, 'snapshots': 1}, WORKERS, NOW)
    assert report['ready'] and report['status'] == 'PASS'
    assert report['pending_ratio'] is None


@pytest.mark.parametrize('counter', ['illegal_market_settlements','illegal_strategy_settlements',
    'nonproduction_market_sources','pilot_or_unknown_snapshots','pilot_or_unknown_tickets',
    'unsafe_production_time'])
def test_each_critical_invariant_blocks_readiness(counter):
    report = evaluate({**COUNTERS, 'snapshots': 1, counter: 1}, WORKERS, NOW)
    assert report['status'] == 'FAIL' and not report['ready']
    assert counter in report['violations']


def test_parser_timezone_pending_and_missing_worker_are_visible_degradation():
    report = evaluate({**COUNTERS, 'snapshots': 1, 'due_tickets': 10, 'pending_due_tickets': 8,
                       'broken_pilot_parsers': 1,'zero_row_pilot_parsers': 1,
                       'unresolved_pilot_timezone_rows': 2}, {}, NOW)
    assert report['status'] == 'DEGRADED' and not report['ready']
    assert report['pending_ratio'] == .8
    assert set(report['warnings']) == {'high_pending_ratio','broken_pilot_parsers',
        'zero_row_pilot_parsers','unresolved_pilot_timezone_rows','collector_unobserved','settlement_unobserved'}


def test_stale_future_and_unobserved_workers_are_not_certified():
    report = evaluate({**COUNTERS, 'snapshots': 1},
                       {'collector': NOW - timedelta(hours=7), 'settlement': NOW + timedelta(seconds=1)}, NOW)
    assert not report['ready']
    assert set(report['warnings']) == {'collector_stale_or_future','settlement_stale_or_future'}
    assert 'no_production_snapshots' in evaluate(COUNTERS, WORKERS, NOW)['warnings']
    with pytest.raises(ValueError, match='timezone'):
        evaluate(COUNTERS, WORKERS, NOW.replace(tzinfo=None))


def test_http_failure_degrades_health_even_with_no_parser_errors():
    report = evaluate({**COUNTERS, 'snapshots': 1, 'failed_pilot_transports': 1}, WORKERS, NOW)
    assert report['status'] == 'DEGRADED' and not report['ready']
    assert report['warnings'] == ['failed_pilot_transports']
    assert report['violations'] == []


def test_http_gate_exposes_fail_without_claiming_database_error_is_healthy(monkeypatch):
    report = evaluate({**COUNTERS,'illegal_market_settlements':1}, WORKERS, NOW)
    monkeypatch.setattr(api, 'runtime_health', lambda: report)
    with TestClient(api.app, raise_server_exceptions=False) as client:
        response = client.get('/health/invariants')
        assert response.status_code == 503 and response.json() == report
        monkeypatch.setattr(api, 'runtime_health', lambda: (_ for _ in ()).throw(RuntimeError('database down')))
        assert client.get('/health/invariants').status_code == 500
