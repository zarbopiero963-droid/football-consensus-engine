"""Collector/settlement smoke: real PostgreSQL, synthetic HTTP only, isolated files."""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL
import pytest

from app import api, market_engine, persist, result_validation, strategy_sync
from app.migrations import apply_migrations
from prospective import collect, settle
from app.market_reference import get_market_reference


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL required for real PostgreSQL integration')
    schema = 'pipeline_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f'-c search_path={schema}')

    apply_migrations(connect)
    for module in (api, market_engine, persist, result_validation, strategy_sync):
        monkeypatch.setattr(module, 'connect', connect)
    monkeypatch.setenv('DATABASE_URL', dsn)
    monkeypatch.setenv('DATABASE_REQUIRED', '1')
    now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    kickoff = now + timedelta(hours=6)
    monkeypatch.setattr(collect, 'ROOT', str(tmp_path))
    monkeypatch.setattr(collect, 'utcnow', lambda: now)
    for key, path in {'ROOT': tmp_path, 'LEDGER': tmp_path / 'prospective_ledger.csv',
                      'RESULTS': tmp_path / 'normalized/results.csv',
                      'AUDIT': tmp_path / 'normalized/result_audit.jsonl',
                      'REPORT': tmp_path / 'reports/settlement_status.json',
                      'HISTORY': tmp_path / 'reports/settlement_history.jsonl'}.items():
        monkeypatch.setattr(settle, key, str(path))

    def http_fixture(url, **kwargs):
        if url in collect.FI_INDEXES:
            return 200, b'<a href="/predictions/test-czechia-england-2026-10-01/">test</a>', {}
        if url == collect.PIN_LEAGUES:
            data = [{'id': 1, 'name': 'Test league', 'matchupCount': 1}]
        elif '/matchups?' in url:
            data = [{'id': 1, 'startTime': kickoff.isoformat(), 'participants': [
                {'alignment': 'home', 'name': 'Czechia'}, {'alignment': 'away', 'name': 'England'}]}]
        elif '/markets/straight' in url:
            data = [{'matchupId': 1, 'type': 'moneyline', 'period': 0, 'status': 'open',
                     'key': 's;0;m', 'version': 1, 'prices': [
                         {'designation': 'home', 'price': 100}, {'designation': 'draw', 'price': 250},
                         {'designation': 'away', 'price': 200}]}]
        elif url.startswith('https://footinsights.com/predictions/test-'):
            return 200, page['body'], {}
        else:
            raise AssertionError('Unstubbed external HTTP request')
        return 200, json.dumps(data).encode(), {}

    def html(kick=kickoff, probabilities=True):
        event = {'@type': 'SportsEvent', 'startDate': kick.isoformat(),
                 'organizer': {'name': 'Test league'},
                 'homeTeam': {'name': 'Czechia'}, 'awayTeam': {'name': 'England'}}
        bars = ''.join(f'<div class="model" style="width: {value}%"></div>' for value in [60, 25, 15])
        return ('<script type="application/ld+json">' + json.dumps(event) + '</script>' +
                (bars if probabilities else '')).encode()

    page = {'body': html()}
    monkeypatch.setattr(collect, 'http_get', http_fixture)
    # A fallback is forbidden in this controlled fixture, so no unnoticed network escapes.
    monkeypatch.setattr(collect, 'fetch_pinnacle_leagues_via_bridge',
                        lambda: (_ for _ in ()).throw(AssertionError('Unexpected bridge fallback')))

    def cycle(matches, at=None):
        return settle.run_cycle(now=at or kickoff + timedelta(hours=3),
                                results_path=settle.RESULTS, audit_path=settle.AUDIT,
                                ledger_path=settle.LEDGER, fifa_matches=matches)

    fifa = {'IdMatch': 'test-fifa', 'Date': kickoff.isoformat(), 'HomeTeamScore': 0,
            'AwayTeamScore': 2, 'MatchStatus': 0, 'OfficialityStatus': 2, 'ResultType': 1,
            'Home': {'TeamName': [{'Description': 'Czech Republic'}]},
            'Away': {'TeamName': [{'Description': 'England'}]}}
    return {'connect': connect, 'root': tmp_path, 'now': now, 'kickoff': kickoff,
            'page': page, 'html': html, 'cycle': cycle, 'fifa': fifa}


def read(pipeline, sql):
    with pipeline['connect']() as conn:
        return list(conn.execute(sql).fetchall())


def test_collect_freeze_settle_retry_correction_and_field_verification(pipeline, capsys):
    collect.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary['valid_prematch'] == summary['joined_pinnacle'] == 1
    assert summary['database']['snapshots']['result'] == 1
    snapshots = read(pipeline, 'SELECT * FROM model_snapshots')
    assert len(snapshots) == 1 and snapshots[0]['model_pick_probability'] == .6
    assert snapshots[0]['picked_odds'] == 2.0
    row = snapshots[0]['payload']
    assert persist.upsert_snapshots([row]) == 0
    frozen = api.create_decision_batch(pipeline['kickoff'].date(), api.DecisionRequest(),
                                       requested_at=pipeline['now'] + timedelta(minutes=1),
                                       idempotency_key='pipeline-test')
    assert frozen['count'] == 1 and len(frozen['markets']) == 2
    tickets = read(pipeline, 'SELECT snapshot_view FROM decision_tickets')
    sealed_files = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in pipeline['root'].rglob('*') if p.is_file() and p.name != 'collect.lock'}
    assert pipeline['cycle']([pipeline['fifa']], at=pipeline['kickoff'] + timedelta(hours=1))['appended'] == 0
    assert pipeline['cycle']([{**pipeline['fifa'], 'MatchStatus': 1}])['appended'] == 0
    first = pipeline['cycle']([pipeline['fifa']])
    assert first['appended'] == 1 and first['prematch_hashes_unchanged']
    assert first['result_validation']['fifa_rows'] == 1
    results = read(pipeline, 'SELECT * FROM match_results')
    assert results[0]['final_home_goals'] == 0 and results[0]['final_away_goals'] == 2
    assert results[0]['payload']['final_home_goals'] == 0
    assert all(t['status'] == 'PENDING' for t in read(pipeline, 'SELECT status FROM market_decision_tickets'))
    again = pipeline['cycle']([pipeline['fifa']])
    assert again['appended'] == 0 and again['skipped_identical'] == 1
    assert len(read(pipeline, 'SELECT * FROM result_observations')) == 1
    mid = row['match_id']
    result_validation.upsert_observation(mid, 'test-independent-provider', datetime.now(timezone.utc),
                                         pipeline['kickoff'], {'provider_match_id': 'test-independent',
                                         'status': 'settled', 'final_home_goals': 0, 'final_away_goals': 2})
    result_validation.rebuild_consensus(mid)
    assert market_engine.synchronize_market_settlement()['tickets_updated'] == 2
    assert all(t['status'] == 'LOST' for t in read(pipeline, 'SELECT status FROM market_decision_tickets'))
    consensus = read(pipeline, 'SELECT * FROM result_consensus')[0]
    assert consensus['field_verification']['final_home_goals']['status'] == 'VERIFIED'
    assert consensus['field_verification']['ht_home_goals']['status'] == 'MISSING'
    assert consensus['ht_home_goals'] is None
    corrected = {**pipeline['fifa'], 'HomeTeamScore': 1, 'AwayTeamScore': 1}
    assert pipeline['cycle']([corrected])['corrections'] == 1
    versions = read(pipeline, 'SELECT version,final_home_goals,final_away_goals FROM match_results ORDER BY version')
    assert versions == [{'version': 1, 'final_home_goals': 0, 'final_away_goals': 2},
                        {'version': 2, 'final_home_goals': 1, 'final_away_goals': 1}]
    assert len(read(pipeline, 'SELECT * FROM result_observations')) == 3
    assert all(t['status'] == 'PENDING' for t in read(pipeline, 'SELECT status FROM market_decision_tickets'))
    assert read(pipeline, 'SELECT snapshot_view FROM decision_tickets') == tickets
    assert read(pipeline, 'SELECT * FROM model_snapshots') == snapshots
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in sealed_files.items())
    audit = json.loads(Path(settle.AUDIT).read_text().splitlines()[0])
    assert audit['old_score'] == '0-2' and audit['new_score'] == '1-1'


@pytest.mark.parametrize('invalid', ['past', 'missing_probability'])
def test_invalid_page_cannot_enter_model_or_decision_ledger(pipeline, invalid, capsys):
    pipeline['page']['body'] = pipeline['html'](
        kick=pipeline['now'] - timedelta(hours=1) if invalid == 'past' else pipeline['kickoff'],
        probabilities=invalid != 'missing_probability')
    collect.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary['valid_prematch'] == 0 and summary['invalid'] == 1
    assert summary['database']['snapshots']['result'] == 0
    assert not read(pipeline, 'SELECT * FROM model_snapshots')
    frozen = api.create_decision_batch(pipeline['kickoff'].date(), api.DecisionRequest(),
                                       requested_at=pipeline['now'], idempotency_key='invalid-test')
    assert frozen['count'] == 0 and frozen['markets'] == []
    assert not read(pipeline, 'SELECT * FROM decision_tickets')
    assert not read(pipeline, 'SELECT * FROM market_decision_tickets')


def test_required_database_failure_is_fatal_to_collector(pipeline, monkeypatch):
    monkeypatch.setattr(persist, 'connect', lambda: (_ for _ in ()).throw(RuntimeError('test database unavailable')))
    with pytest.raises(RuntimeError, match='test database unavailable'):
        collect.main()
    assert not read(pipeline, 'SELECT * FROM model_snapshots')


def test_market_reference_real_postgres_preserves_sealed_rows(pipeline, monkeypatch, capsys):
    from app import market_reference
    monkeypatch.setattr(market_reference, 'connect', pipeline['connect'])
    collect.main()
    capsys.readouterr()
    sealed = read(pipeline, 'SELECT * FROM model_snapshots')[0]
    later = pipeline['now'] + timedelta(hours=5)
    payload = {**sealed['payload'], 'pin_snapshot_timestamp_utc': later.isoformat(),
               'odds_home': 2.1, 'snapshot_timestamp_utc': later.isoformat()}
    # Synthetic timestamps explicitly controlled, including knowledge time.
    with pipeline['connect']() as conn:
        conn.execute('''INSERT INTO model_snapshots
            (match_id,snapshot_timestamp_utc,kickoff_utc,home_team,away_team,league,
             valid_prematch,imported_at,payload)
            VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s,%s::jsonb)''',
            (sealed['match_id'], later, pipeline['kickoff'], 'Czechia', 'England',
             'Test league', later + timedelta(seconds=1), json.dumps(payload)))
        # A backfill captured before kickoff but sealed afterwards is excluded.
        payload['pin_snapshot_timestamp_utc'] = (pipeline['kickoff'] - timedelta(seconds=1)).isoformat()
        conn.execute('''INSERT INTO model_snapshots
            (match_id,snapshot_timestamp_utc,kickoff_utc,home_team,away_team,league,
             valid_prematch,imported_at,payload)
            VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s,%s::jsonb)''',
            (sealed['match_id'], pipeline['kickoff'] - timedelta(seconds=1), pipeline['kickoff'],
             'Czechia', 'England', 'Test league', pipeline['kickoff'] + timedelta(seconds=1),
             json.dumps(payload)))
    original = read(pipeline, 'SELECT * FROM model_snapshots ORDER BY snapshot_timestamp_utc')
    result = get_market_reference(sealed['match_id'], known_at=pipeline['kickoff'] + timedelta(hours=1))
    assert result['status'] == 'CLOSED_OBSERVED_PROXY'
    assert result['q_close']['odds']['1'] == 2.1
    assert result['rejected']['NOT_SEALED_PRE_KICKOFF'] == 1 + int(sealed['imported_at'] >= pipeline['kickoff'])
    early = get_market_reference(sealed['match_id'], known_at=later - timedelta(seconds=1))
    assert early['q_close'] is None
    assert early['latest_pre_kickoff'] is None or early['latest_pre_kickoff']['odds']['1'] == 2
    assert get_market_reference('missing', known_at=pipeline['kickoff'])['status'] == 'MISSING'
    assert read(pipeline, 'SELECT * FROM model_snapshots ORDER BY snapshot_timestamp_utc') == original


def test_naive_collector_time_cannot_enter_ledger(pipeline, capsys):
    pipeline['page']['body'] = pipeline['html'](kick=pipeline['kickoff'].replace(tzinfo=None))
    collect.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary['valid_prematch'] == 0 and summary['database']['snapshots']['result'] == 0
    assert not read(pipeline, 'SELECT * FROM model_snapshots')


@pytest.mark.parametrize('case', ['containment', 'shifted', 'league', 'duplicate', 'late_odds', 'invalid_odds'])
def test_unsafe_market_cannot_price_production_snapshot(pipeline, monkeypatch, capsys, case):
    original_http = collect.http_get

    def http(url, **kwargs):
        status, body, headers = original_http(url, **kwargs)
        if '/matchups?' in url:
            data = json.loads(body)
            if case == 'containment':
                data[0]['participants'][0]['name'] = 'Czechia U21'
            if case == 'shifted':
                data[0]['startTime'] = (pipeline['kickoff'] + timedelta(hours=5)).isoformat()
            body = json.dumps(data).encode()
        if url == collect.PIN_LEAGUES and case == 'league':
            data = json.loads(body)
            data[0]['name'] = 'Different league'
            body = json.dumps(data).encode()
        if '/markets/straight' in url:
            if case == 'invalid_odds':
                data = json.loads(body)
                data[0]['prices'][0]['price'] = 'NaN'
                body = json.dumps(data).encode()
            if case == 'duplicate':
                data = json.loads(body)
                body = json.dumps(data + data).encode()
            if case == 'late_odds':
                monkeypatch.setattr(collect, 'utcnow', lambda: pipeline['kickoff'])
        return status, body, headers

    monkeypatch.setattr(collect, 'http_get', http)
    collect.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary['valid_prematch'] == 1 and summary['joined_pinnacle'] == 0
    row = read(pipeline, 'SELECT * FROM model_snapshots')[0]
    assert row['picked_odds'] is None and row['analysis_eligible'] is False


def test_prediction_fetched_at_kickoff_cannot_be_backdated(pipeline, monkeypatch, capsys):
    original_http = collect.http_get

    def http(url, **kwargs):
        response = original_http(url, **kwargs)
        if url.startswith('https://footinsights.com/predictions/test-'):
            monkeypatch.setattr(collect, 'utcnow', lambda: pipeline['kickoff'])
        return response

    monkeypatch.setattr(collect, 'http_get', http)
    collect.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary['valid_prematch'] == 0 and summary['database']['snapshots']['result'] == 0
    assert not read(pipeline, 'SELECT * FROM model_snapshots')
