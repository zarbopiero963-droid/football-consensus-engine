"""Real PostgreSQL; replayed public DOM with explicitly controlled date variants.

CDP is controlled, not live. Actual external Browser verification is separate.
"""
from datetime import datetime, timedelta, timezone
import base64
import gzip
import hashlib
import json
from pathlib import Path
import re

import pytest

from staging.browser_use_shortlist import pilot48 as shortlist
from staging.issue29_pilot import pilot48 as issue29
from staging.structured_predictions import HTML_PARSERS, explicit_time, select_prematch
from app.runtime_health import COUNTERS_SQL
from test_collect_settle_postgres import pipeline
from test_pilot_content_postgres import Page, browser_fixture


def document(source, kind):
    pack = json.loads((Path(__file__).parents[2] / 'staging/structured_predictions_evidence/live_fragments_20261001.json').read_text())
    doc = next(d for d in pack['documents'] if d['source'] == source and d['kind'] == kind)
    raw = gzip.decompress(base64.b64decode(doc['fragment_gzip_base64']))
    assert hashlib.sha256(raw).hexdigest() == doc['fragment_sha256']
    return doc, raw


def controlled_date_variant(raw, reference, cutoff, days):
    delta = cutoff - datetime.fromisoformat(reference) + timedelta(days=days)
    def replace(match):
        stamp = explicit_time(match.group(2))
        assert stamp is not None
        return match.group(1) + (stamp + delta).isoformat().replace('+00:00', 'Z') + '"'
    return re.sub(r'((?:datetime|data-utc-time)=")([^"]+)"', replace, raw.decode())


@pytest.mark.parametrize('module,source,expected', [
    (shortlist, 'elofoot', 10), (issue29, 'opta_analyst', 28), (issue29, 'clubelo', 50),
])
def test_real_postgres_future_html_predictions_utc_native_values_seals_and_history(pipeline, monkeypatch, module, source, expected):
    connect = pipeline['connect']
    with connect() as conn:
        cutoff = conn.execute('SELECT clock_timestamp() AS at').fetchone()['at']
        conn.execute('''INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,
                     started_at_utc,status,manifest) VALUES ('synthetic-historical-run',
                     'synthetic-structured-pilot',%s,1,NOW(),'OK','{"historical":true}')''', (source,))
        conn.execute('''INSERT INTO source_pilot_predictions(run_id,source,source_match_key,
                     home_team,away_team,p1,px,p2,payload) VALUES ('synthetic-historical-run',
                     %s,'synthetic-old-key','Synthetic old home','Synthetic old away',100,0,0,'{"historical":true}')''', (source,))
        previous_run = conn.execute("SELECT to_jsonb(r) AS row FROM source_pilot_runs r").fetchone()['row']
        previous_row = conn.execute("SELECT to_jsonb(p) AS row FROM source_pilot_predictions p").fetchone()['row']
    monkeypatch.setattr(module, 'connect', connect)
    monkeypatch.setattr(module, 'PILOT_ID', 'synthetic-structured-pilot')
    monkeypatch.setattr(module, 'utcnow', lambda: cutoff)
    doc, raw = document(source, 'independent_pass2')
    html = controlled_date_variant(raw, doc['diagnostic_cutoff_utc'], cutoff, 2)
    native, _ = select_prematch(HTML_PARSERS[source](html), cutoff)
    assert len(native) == expected
    page = Page(['controlled public DOM replay'])
    page.content = lambda: html
    stopped = browser_fixture(monkeypatch, module, page)
    anchors = [{**native[0], 'match_id': 'synthetic-anchor'}]
    outcome = module.collect_source(source, module.SOURCES[source], 2, anchors)
    assert outcome['rows'] == expected and stopped == [{'action': 'stop'}]
    with connect() as conn:
        run = conn.execute('SELECT * FROM source_pilot_runs WHERE slot_no=2').fetchone()
        rows = list(conn.execute('SELECT * FROM source_pilot_predictions WHERE run_id=%s ORDER BY home_team,away_team', (run['run_id'],)))
        assert len(rows) == expected
        assert run['manifest']['page_capture']['parser_input'] == 'HTML_SAME_SAMPLE'
        audit = run['manifest']['structured_prediction_audit']
        assert audit['rows_accepted'] == expected
        assert datetime.fromisoformat(audit['ingestion_cutoff_utc']) >= cutoff
        compressed_raw = gzip.decompress(bytes(run['html_gzip']))
        assert hashlib.sha256(compressed_raw).hexdigest() == run['html_sha256']
        assert html.encode() in compressed_raw
        native_map = {(r['home_team'], r['away_team']): r for r in native}
        for row in rows:
            model = native_map[(row['home_team'], row['away_team'])]
            assert row['kickoff_raw'] == model['kickoff_raw']
            assert row['kickoff_utc'] == model['kickoff_utc'] > row['sealed_at_utc'] >= run['sealed_at_utc']
            assert row['timezone_status'] == 'EXPLICIT_OFFSET'
            assert (row['p1'], row['px'], row['p2']) == (model['p1'], model['px'], model['p2'])
            assert row['payload']['production_eligible'] is False
            assert row['payload']['source_match_id'] == model['payload_extra']['source_match_id']
            assert row['payload']['time_evidence'] == model['payload_extra']['time_evidence']
        statuses = list(conn.execute('SELECT match_status FROM source_pilot_matches'))
        assert sum(r['match_status'] == 'FOUND_NAME_ONLY' for r in statuses) == 1
        assert not any(r['match_status'] == 'FOUND' for r in statuses)
        assert conn.execute(COUNTERS_SQL).fetchone()['unresolved_pilot_timezone_rows'] == 0
        # A browser-only timezone remains unresolved even with a UTC column.
        conn.execute('''INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,
                     started_at_utc,status) VALUES ('synthetic-browser-time',
                     'synthetic-browser-pilot','synthetic-browser',1,NOW(),'OK')''')
        conn.execute('''INSERT INTO source_pilot_predictions(run_id,source,source_match_key,
                     home_team,away_team,kickoff_utc,timezone_status)
                     VALUES ('synthetic-browser-time','synthetic-browser','synthetic-browser-key',
                     'Synthetic home','Synthetic away',NOW()+INTERVAL '1 day','VERIFIED_BROWSER_TZ')''')
        assert conn.execute(COUNTERS_SQL).fetchone()['unresolved_pilot_timezone_rows'] == 1
        assert conn.execute("SELECT to_jsonb(r) AS row FROM source_pilot_runs r WHERE slot_no=1").fetchone()['row'] == previous_run
        assert conn.execute("SELECT to_jsonb(p) AS row FROM source_pilot_predictions p WHERE run_id='synthetic-historical-run'").fetchone()['row'] == previous_row
        for table in ('model_snapshots', 'decision_tickets', 'market_decision_tickets', 'source_pilot_control'):
            assert conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 0


@pytest.mark.parametrize('failure', ['already_started', 'http_error', 'starts_during_cleanup'])
def test_real_postgres_negative_acquisition_and_cleanup_never_insert_prediction(pipeline, monkeypatch, failure):
    connect = pipeline['connect']
    with connect() as conn:
        cutoff = conn.execute('SELECT clock_timestamp() AS at').fetchone()['at']
    monkeypatch.setattr(issue29, 'connect', connect)
    monkeypatch.setattr(issue29, 'PILOT_ID', 'synthetic-structured-negative')
    monkeypatch.setattr(issue29, 'utcnow', lambda: cutoff)
    doc, raw = document('opta_analyst', 'independent_pass2')
    html = controlled_date_variant(raw, doc['diagnostic_cutoff_utc'], cutoff, -3 if failure == 'already_started' else 2)
    page = Page(['controlled source DOM'], http_status=404 if failure == 'http_error' else 200)
    page.content = lambda: html
    stopped = browser_fixture(monkeypatch, issue29, page)
    if failure == 'starts_during_cleanup':
        api = issue29.browser_api
        def browser_api(method, url, body=None):
            result = api(method, url, body)
            if method == 'GET':
                # Controlled elapsed cleanup clock, explicitly not a live test.
                monkeypatch.setattr(issue29, 'utcnow', lambda: cutoff + timedelta(days=10))
            return result
        monkeypatch.setattr(issue29, 'browser_api', browser_api)
    outcome = issue29.collect_source('opta_analyst', issue29.SOURCES['opta_analyst'], 1, [])
    assert outcome['rows'] == 0 and stopped == [{'action': 'stop'}]
    with connect() as conn:
        run = conn.execute('SELECT * FROM source_pilot_runs').fetchone()
        audit = run['manifest']['structured_prediction_audit']
        if failure == 'http_error':
            assert audit['capture_rejected'] == 'HTTP_ERROR'
        else:
            assert audit['rejection_counts']['NOT_PREMATCH_AT_INGESTION'] == 28
        assert run['manifest']['rows_parsed'] == 0
        assert conn.execute('SELECT count(*) AS n FROM source_pilot_predictions').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM source_pilot_matches').fetchone()['n'] == 0
        for table in ('model_snapshots', 'decision_tickets', 'market_decision_tickets'):
            assert conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 0
