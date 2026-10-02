"""Run success is determined before its immutable seal, not by browser delivery."""
import base64
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from app.source_resilience import capture_fault, pilot_run_outcome


@pytest.mark.parametrize('state,http,expected,reason', [
    ('READY_PARSED', 200, 'OK', 'READY_PARSED'),
    ('CAPABILITY_ONLY', 200, 'OK', 'CAPABILITY_ONLY'),
    ('EMPTY_UNCONFIRMED', 200, 'OK', 'EMPTY_UNCONFIRMED'),
    ('HTTP_ERROR', 404, 'ERROR', 'HTTP_404'),
    ('HTTP_ERROR', 504, 'ERROR', 'HTTP_504'),
    ('HTTP_UNOBSERVED', None, 'ERROR', 'HTTP_UNOBSERVED'),
    ('MAINTENANCE', 200, 'ERROR', 'MAINTENANCE'),
    ('ACCESS_BLOCKED', 403, 'ERROR', 'ACCESS_BLOCKED'),
    ('LOADING_TIMEOUT', 200, 'ERROR', 'LOADING_TIMEOUT'),
    ('CAPTURE_ERROR', 200, 'ERROR', 'CAPTURE_ERROR'),
    ('PARSER_ERROR', 200, 'ERROR', 'PARSER_ERROR'),
])
def test_run_status_agrees_with_existing_circuit_policy(state, http, expected, reason):
    audit = {'state': state, 'http_status': http}
    before = dict(audit)
    out = pilot_run_outcome(audit)
    assert out['status'] == expected and out['reason'] == reason
    assert (out['status'] == 'ERROR') == capture_fault({'page_capture': audit})['failed']
    assert audit == before
    assert not out['partial']
    assert out['capture_states'] == {'main': state}


def test_partial_source_retains_working_pages_and_failed_field_audit():
    main = {'state': 'HTTP_ERROR', 'http_status': 404}
    extra = {'tomorrow': {'state': 'READY_PARSED', 'http_status': 200},
             'corners': {'state': 'PARSER_ERROR', 'http_status': 200}}
    out = pilot_run_outcome(main, extra)
    assert out['status'] == 'OK' and out['partial'] and out['reason'] == 'READY_PARSED'
    assert out['capture_states'] == {'main': 'HTTP_ERROR', 'tomorrow': 'READY_PARSED', 'corners': 'PARSER_ERROR'}
    assert not capture_fault({'page_capture': main, 'extra_page_captures': extra})['failed']


def test_capability_extra_page_cannot_hide_failed_prediction_acquisition():
    out = pilot_run_outcome({'state': 'MAINTENANCE', 'http_status': 200},
                            {'metadata': {'state': 'CAPABILITY_ONLY', 'http_status': 200}})
    assert out['status'] == 'ERROR' and not out['partial']


def test_actual_external_http404_record_replay_preserves_legacy_contradiction():
    path = Path(__file__).parent / 'fixtures/pilot_false_ok_20261002.json'
    saved = path.read_bytes()
    row = json.loads(saved)
    html = gzip.decompress(base64.b64decode(row['html_gzip_base64']))
    assert hashlib.sha256(html).hexdigest() == row['html_sha256']
    assert len(html) == 3449
    assert row['status'] == 'OK' and row['http_status'] == 404
    assert row['page_capture']['state'] == 'HTTP_ERROR'
    assert row['source_resilience_at_admission']['mode'] == 'CLOSED'
    out = pilot_run_outcome(row['page_capture'])
    assert out['status'] == 'ERROR' and out['reason'] == 'HTTP_404'
    assert row['status'] == 'OK' and path.read_bytes() == saved
