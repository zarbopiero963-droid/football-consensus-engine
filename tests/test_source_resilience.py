from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.source_resilience import admit_state, capture_fault, finish_state, initial_state, source_key
from staging.page_capture import capture_summary, page_state

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_transient_failures_need_two_cycles_then_single_probe_escalates_and_recovers():
    state, decision = admit_state(initial_state(), NOW, 'first')
    assert decision == 'ADMITTED'
    fault = {'reason': 'HTTP_504', 'failed': True}
    state = finish_state(state, NOW, fault)
    assert state['mode'] == 'CLOSED' and state['failures'] == 1
    state, _ = admit_state(state, NOW, 'second')
    state = finish_state(state, NOW, fault)
    for hours in (3, 6, 12, 12):
        assert datetime.fromisoformat(state['retry_at_utc']) == NOW + timedelta(hours=hours)
        assert admit_state(state, NOW, 'blocked')[1] == 'SUSPENDED'
        later = datetime.fromisoformat(state['retry_at_utc'])
        state, decision = admit_state(state, later, 'probe')
        assert decision == 'ADMITTED' and state['mode'] == 'HALF_OPEN'
        assert admit_state(state, later, 'racer')[1] == 'BUSY'
        state = finish_state(state, NOW, fault)
    state, _ = admit_state(state, datetime.fromisoformat(state['retry_at_utc']), 'healthy')
    state = finish_state(state, NOW, {'reason': 'READY_PARSED', 'failed': False})
    assert state['mode'] == 'CLOSED' and state['failures'] == state['open_count'] == 0


@pytest.mark.parametrize('reason,hours', [('MAINTENANCE', 3), ('ACCESS_BLOCKED', 12), ('PARSER_ERROR', 12)])
def test_terminal_failures_suspend_immediately_without_reinterpreting_data(reason, hours):
    state, _ = admit_state(initial_state(), NOW, 'owner')
    state = finish_state(state, NOW, {'reason': reason, 'failed': True, 'immediate': True})
    assert state['mode'] == 'OPEN'
    assert datetime.fromisoformat(state['retry_at_utc']) == NOW + timedelta(hours=hours)


def test_retry_after_can_defer_beyond_cooldown_and_expired_browser_is_reaped_first():
    state, _ = admit_state(initial_state(), NOW, 'owner')
    state['active_browser_id'] = 'opaque-public-id'
    renewed, _ = admit_state(state, NOW + timedelta(minutes=16), 'new-owner')
    assert renewed['stop_pending_id'] == 'opaque-public-id' and renewed['mode'] == 'HALF_OPEN'
    state = finish_state(renewed, NOW, {'reason': 'HTTP_429', 'failed': True,
                                      'immediate': True, 'retry_after_seconds': 30 * 86400})
    assert datetime.fromisoformat(state['retry_at_utc']) == NOW + timedelta(days=30)


def test_partial_source_is_available_but_manifest_summary_keeps_failures_without_raw():
    audit = {'state': 'HTTP_ERROR', 'http_status': 504,
             'navigation_attempts': [{'html_gzip_base64': 'raw', 'html_sha256': 'proof'}]}
    outcome = {'page_capture': audit, 'extra_page_captures': {'tomorrow': {'state': 'READY_PARSED'}}}
    assert not capture_fault(outcome)['failed']
    assert capture_summary(audit)['navigation_attempts'] == [{'html_sha256': 'proof'}]
    assert audit['navigation_attempts'][0]['html_gzip_base64'] == 'raw'


def test_identity_isolated_by_role_and_pilot_and_invalid_registry_inputs_fail():
    assert source_key('prediction', 'pilot-a', 'source') != source_key('result', 'pilot-a', 'source')
    assert source_key('result', 'pilot-a', 'source') != source_key('result', 'pilot-b', 'source')
    with pytest.raises(ValueError): source_key('unknown', 'pilot', 'source')
    with pytest.raises(ValueError): source_key('result', 'pilot', '../bad')


def test_live_maintenance_dom_fixture_trips_circuit_without_retrying_or_inventing_rows():
    real = json.loads((Path(__file__).parent / 'fixtures/source_maintenance_20261001.json').read_text())
    state = page_state(real['http_status'], real['title'], real['text'])
    assert state == 'MAINTENANCE' and real['stop_confirmed'] and not real['truncated']
    fault = capture_fault({'page_capture': {'state': state, 'http_status': real['http_status']}})
    admitted, _ = admit_state(initial_state(), NOW, 'owner')
    suspended = finish_state(admitted, NOW, fault)
    assert suspended['mode'] == 'OPEN' and suspended['failures'] == 1
    assert admit_state(suspended, NOW, 'another-browser')[1] == 'SUSPENDED'


def test_empty_or_existing_slot_does_not_certify_half_open_recovery():
    state, _ = admit_state(initial_state(), NOW, 'owner')
    state = finish_state(state, NOW, {'reason': 'MAINTENANCE', 'failed': True, 'immediate': True})
    state, _ = admit_state(state, NOW + timedelta(hours=3), 'probe')
    empty = finish_state(state, NOW + timedelta(hours=3), capture_fault({'page_capture': {'state': 'EMPTY_UNCONFIRMED'}}))
    assert empty['mode'] == 'OPEN' and empty['failures'] == 1
    done = finish_state(state, NOW + timedelta(hours=3), capture_fault({'status': 'ALREADY_DONE'}))
    assert done['mode'] == 'OPEN' and done['failures'] == 1
