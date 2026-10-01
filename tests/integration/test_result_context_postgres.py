"""Actual publisher DOM, controlled CDP/anchors, isolated PostgreSQL only."""
import base64
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from app import result_validation
from staging.result_sources import pilot48 as results
from staging.result_sources.worldfootball import parse_html
from test_collect_settle_postgres import pipeline
from test_pilot_content_postgres import Page, browser_fixture


def html():
    path = Path(__file__).resolve().parents[2] / 'staging/result_sources/evidence/worldfootball_20261001.json'
    data = json.loads(path.read_text())
    raw = gzip.decompress(base64.b64decode(data['fragment_gzip_base64']))
    assert hashlib.sha256(raw).hexdigest() == data['fragment_sha256']
    return raw.decode()


def setup(pipeline, monkeypatch, body=None):
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'PILOT_ID', 'controlled-result-context')
    raw = body or html()
    row = parse_html(html())[0]
    a = {'match_id': 'controlled-inter-monza', 'home_team': row['home_team'],
         'away_team': row['away_team'], 'league': 'Serie A', 'kickoff_utc': row['source_kickoff_utc']}
    monkeypatch.setattr(results, 'result_anchor_rows', lambda: [a])
    page = Page(['Published fixture results'], html=raw)
    browser_fixture(monkeypatch, results, page)
    return raw, row, a


def test_worldfootball_real_dom_preserves_native_evidence_idempotency_and_history(pipeline, monkeypatch):
    raw, row, a = setup(pipeline, monkeypatch)
    with pipeline['connect']() as conn:
        conn.execute("""INSERT INTO result_source_pilot_runs(run_id,pilot_id,source,slot_no,
            started_at_utc,status,manifest) VALUES ('legacy-run','legacy','worldfootball',1,NOW(),'OK','{"old":"unchanged"}')""")
        before = conn.execute("SELECT to_jsonb(r) AS row FROM result_source_pilot_runs r WHERE run_id='legacy-run'").fetchone()['row']
    out = results.collect_source('worldfootball', results.SOURCES['worldfootball'], 1)
    assert out['normalized_observations'] == {'parsed': 50, 'matched': 1, 'persisted': 1}
    with pipeline['connect']() as conn:
        run = conn.execute("SELECT * FROM result_source_pilot_runs WHERE pilot_id='controlled-result-context'").fetchone()
        observation = conn.execute('SELECT * FROM result_observations').fetchone()
        original = observation['payload']
        assert observation['provider_match_id'] == row['source_match_id']
        assert observation['kickoff_utc'] == row['source_kickoff_utc']
        assert observation['final_home_goals'] == 4 and observation['final_away_goals'] == 1
        assert observation['ht_home_goals'] is observation['corners_home'] is None
        assert original['source_kickoff_raw'] == row['source_kickoff_raw']
        assert original['source_end_raw'] == row['source_end_raw']
        assert original['lifecycle_evidence'] == row['lifecycle_evidence']
        assert original['timezone_status'] == 'EXPLICIT_OFFSET'
        assert original['match_status'] == 'EXACT_PAIR_KICKOFF_COMPETITION_DIAGNOSTIC'
        assert original['production_eligible'] is False
        assert run['manifest']['result_parser_audit']['rejection_counts'] == {'NOT_FINISHED': 330}
        audit = run['manifest']['result_match_audit']
        assert audit['matched'] == 1 and audit['rejection_counts'] == {'NOT_FOUND': 49}
        assert audit['production_safe'] is False
        assert run['manifest']['page_capture']['parser_input'] == 'HTML_SAME_SAMPLE'
        assert gzip.decompress(bytes(run['html_gzip'])) == raw.encode()
        assert run['html_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    browser_fixture(monkeypatch, results, Page(['Published fixture results'], html=raw))
    repeat = results.collect_source('worldfootball', results.SOURCES['worldfootball'], 2)
    assert repeat['normalized_observations'] == {'parsed': 50, 'matched': 1, 'persisted': 0}
    production = result_validation.rebuild_consensus(a['match_id'])
    assert production['status'] == 'UNVERIFIED'
    assert all(f['status'] == 'MISSING' for f in production['fields'].values())
    with pipeline['connect']() as conn:
        assert conn.execute("SELECT to_jsonb(r) AS row FROM result_source_pilot_runs r WHERE run_id='legacy-run'").fetchone()['row'] == before
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 1
        assert conn.execute('SELECT payload FROM result_observations').fetchone()['payload'] == original
        for table in ('model_snapshots', 'decision_tickets', 'market_decision_tickets', 'strategy_simulations'):
            assert conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 0


@pytest.mark.parametrize('change,reason', [
    ({'league': 'Other league'}, 'COMPETITION_MISMATCH'),
    ({'kickoff_utc': datetime(2026, 8, 22, 16, 46, tzinfo=timezone.utc)}, 'KICKOFF_MISMATCH'),
    ({'home_team': 'Inter FC'}, 'NOT_FOUND'),
])
def test_wrong_context_persists_rejection_audit_without_result_or_fallback(pipeline, monkeypatch, change, reason):
    _, _, a = setup(pipeline, monkeypatch)
    monkeypatch.setattr(results, 'result_anchor_rows', lambda: [{**a, **change}])
    out = results.collect_source('worldfootball', results.SOURCES['worldfootball'], 1)
    assert out['normalized_observations'] == {'parsed': 50, 'matched': 0, 'persisted': 0}
    with pipeline['connect']() as conn:
        manifest = conn.execute("SELECT manifest FROM result_source_pilot_runs").fetchone()['manifest']
        assert manifest['result_match_audit']['rejection_counts'][reason] >= 1
        assert manifest['fallback_candidates'] == []
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 0


def test_native_end_in_future_does_not_become_finished_result(pipeline, monkeypatch):
    raw = html().replace('data-datetime_end="2026-08-22T18:24:02Z"', 'data-datetime_end="2029-01-01T00:00:00Z"', 1)
    setup(pipeline, monkeypatch, raw)
    out = results.collect_source('worldfootball', results.SOURCES['worldfootball'], 1)
    assert out['normalized_observations'] == {'parsed': 50, 'matched': 0, 'persisted': 0}
    with pipeline['connect']() as conn:
        manifest = conn.execute('SELECT manifest FROM result_source_pilot_runs').fetchone()['manifest']
        assert manifest['result_match_audit']['rejection_counts']['END_NOT_OBSERVED'] == 1
        assert manifest['fallback_candidates'] == []
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 0


def test_http_error_with_finished_dom_is_preserved_but_not_ingested(pipeline, monkeypatch):
    raw, _, _ = setup(pipeline, monkeypatch)
    browser_fixture(monkeypatch, results, Page(['Published fixture results'], http_status=404, html=raw))
    out = results.collect_source('worldfootball', results.SOURCES['worldfootball'], 1)
    assert out['normalized_observations'] == {'parsed': 0, 'matched': 0, 'persisted': 0}
    with pipeline['connect']() as conn:
        run = conn.execute('SELECT * FROM result_source_pilot_runs').fetchone()
        assert run['http_status'] == 404
        assert gzip.decompress(bytes(run['html_gzip'])) == raw.encode()
        assert run['manifest']['page_capture']['state'] == 'HTTP_ERROR'
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 0


def test_parser_limit_failure_preserves_raw_without_observation_or_fallback(pipeline, monkeypatch):
    # Controlled oversized page exercises the actual bounded parser, rather
    # than mocking its error or weakening the shared capture limits.
    raw = html() + ' ' * (5 * 1024 * 1024)
    setup(pipeline, monkeypatch, raw)
    out = results.collect_source('worldfootball', results.SOURCES['worldfootball'], 1)
    assert out['normalized_observations'] == {'parsed': 0, 'matched': 0, 'persisted': 0}
    with pipeline['connect']() as conn:
        run = conn.execute('SELECT * FROM result_source_pilot_runs').fetchone()
        assert gzip.decompress(bytes(run['html_gzip'])) == raw.encode()
        assert run['html_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
        assert run['manifest']['page_capture']['state'] == 'PARSER_ERROR'
        assert run['manifest']['result_parser_audit']['error_class'] == 'ValueError'
        assert run['manifest']['fallback_candidates'] == []
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 0
