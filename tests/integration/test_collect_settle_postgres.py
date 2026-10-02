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
            data = [{'id': 1, 'type': 'matchup', 'isLive': False, 'units': 'Regular',
                     'status': 'pending', 'parentId': None, 'parent': None,
                     'league': {'id': 1, 'name': 'Test league', 'sport': {'id': 29}},
                     'startTime': kickoff.isoformat(), 'participants': [
                {'alignment': 'home', 'name': 'Czechia'}, {'alignment': 'away', 'name': 'England'}]}]
        elif '/markets/straight' in url:
            data = [{'matchupId': 1, 'type': 'moneyline', 'period': 0, 'status': 'open',
                     'key': 's;0;m', 'version': 1, 'isAlternate': False,
                     'cutoffAt': kickoff.isoformat(), 'prices': [
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
    real_fetch_leagues = collect.fetch_pinnacle_leagues_via_bridge
    monkeypatch.setattr(collect, 'fetch_pinnacle_leagues_via_bridge',
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError('Unexpected bridge fallback')))
    monkeypatch.setattr(collect, 'fetch_pinnacle_batch_via_bridge',
                        lambda ids: (_ for _ in ()).throw(AssertionError('Unexpected batch fallback')))

    def cycle(matches, at=None):
        return settle.run_cycle(now=at or kickoff + timedelta(hours=3),
                                results_path=settle.RESULTS, audit_path=settle.AUDIT,
                                ledger_path=settle.LEDGER, fifa_matches=matches)

    fifa = {'IdMatch': 'test-fifa', 'Date': kickoff.isoformat(), 'HomeTeamScore': 0,
            'AwayTeamScore': 2, 'MatchStatus': 0, 'OfficialityStatus': 2, 'ResultType': 1,
            'Home': {'TeamName': [{'Description': 'Czech Republic'}]},
            'Away': {'TeamName': [{'Description': 'England'}]}}
    return {'connect': connect, 'root': tmp_path, 'now': now, 'kickoff': kickoff,
            'page': page, 'html': html, 'cycle': cycle, 'fifa': fifa, 'http_fixture':http_fixture,
            'fetch_leagues': real_fetch_leagues}


def read(pipeline, sql):
    with pipeline['connect']() as conn:
        return list(conn.execute(sql).fetchall())


def test_collector_recovers_league_documents_preserves_quote_raw_and_strict_matching(pipeline,monkeypatch,capsys):
    original=pipeline['http_fixture']
    def direct(url,**kwargs):
        if '/leagues/1/' in url:return 403,b'public source transport denied',{}
        return original(url,**kwargs)
    monkeypatch.setattr(collect,'http_get',direct)
    batches=[]
    def fallback(ids):
        batches.append(ids)
        assert ids==[1]
        mu=original('https://guest.api.arcadia.pinnacle.com/0.1/leagues/1/matchups?brandId=0')
        mk=original('https://guest.api.arcadia.pinnacle.com/0.1/leagues/1/markets/straight')
        return [{'id':1,'err':None,'matchups':mu[:2],'markets':mk[:2],
                 'captured_at':pipeline['now'],'transport':'browser_bridge_fallback'}]
    monkeypatch.setattr(collect,'fetch_pinnacle_batch_via_bridge',fallback)
    collect.main();summary=json.loads(capsys.readouterr().out)
    assert batches==[[1]] and summary['joined_pinnacle']==1
    rows=read(pipeline,'SELECT * FROM model_snapshots')
    assert len(rows)==1 and rows[0]['picked_odds']==2
    p=rows[0]['payload']
    assert p['pin_transport']=='browser_bridge_fallback' and p['name_score_note']==collect.MATCH_CONTRACT
    match_audit=json.loads(p['market_match_audit'])
    assert match_audit['status']=='UNIQUE_EXACT_MATCH'
    assert match_audit['matched_candidates']==1 and match_audit['candidate_first_rejections']=={}
    raw=next(pipeline['root'].rglob('markets_1.json')).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==p['sha256_odds_raw']
    audit=json.loads(next(pipeline['root'].rglob('transport_audit.json')).read_text())
    assert audit[0]['matchups_direct_status']==audit[0]['markets_direct_status']==403
    assert audit[0]['final_transport']=='browser_bridge_fallback' and audit[0]['final_error'] is None
    quality=json.loads(next((pipeline['root']/'reports').glob('daily_quality_*.json')).read_text())
    assert quality['pinnacle_direct_leagues_recovered']==1
    assert quality['pinnacle_league_transports']=={'direct':0,'browser_bridge_fallback':1,'failed':0}
    for table in ('market_decision_tickets','decision_tickets','source_pilot_predictions'):
        assert read(pipeline,f'SELECT count(*) AS n FROM {table}')[0]['n']==0


def test_failed_league_fallback_is_not_priced_or_hidden(pipeline,monkeypatch,capsys):
    original=pipeline['http_fixture']
    def direct(url,**kwargs):
        if '/leagues/1/' in url:return 403,b'denied',{}
        return original(url,**kwargs)
    def fallback(ids):raise RuntimeError('upstream unavailable')
    monkeypatch.setattr(collect,'http_get',direct)
    monkeypatch.setattr(collect,'fetch_pinnacle_batch_via_bridge',fallback)
    collect.main();summary=json.loads(capsys.readouterr().out)
    assert summary['joined_pinnacle']==0 and summary['errors']>=1
    rows=read(pipeline,'SELECT * FROM model_snapshots')
    assert len(rows)==1 and rows[0]['picked_odds'] is None
    assert rows[0]['payload']['pin_matchup_id']=='' and rows[0]['payload']['pin_transport']==''
    assert json.loads(rows[0]['payload']['market_match_audit'])['status']=='NO_MARKET_QUOTES'
    audit=json.loads(next(pipeline['root'].rglob('transport_audit.json')).read_text())
    assert audit[0]['final_transport']=='failed' and audit[0]['final_error']


@pytest.mark.parametrize('case,status,rejections', [
    ('competition', 'EXACT_PAIR_REJECTED', {'LEAGUE_MISMATCH': 1}),
    ('time', 'EXACT_PAIR_REJECTED', {'KICKOFF_MISMATCH': 1}),
    ('pair', 'NO_EXACT_PAIR', {}),
    ('ambiguous', 'AMBIGUOUS_EXACT_MATCH', {}),
])
def test_rejected_matching_audit_is_sealed_persisted_and_never_priced(pipeline, monkeypatch, capsys, case, status, rejections):
    original = pipeline['http_fixture']

    def http(url, **kwargs):
        code, body, headers = original(url, **kwargs)
        if url == collect.PIN_LEAGUES and case == 'competition':
            body = json.dumps([{'id': 1, 'name': 'Other league', 'matchupCount': 1}]).encode()
        elif '/matchups?' in url:
            rows = json.loads(body)
            if case == 'competition':
                rows[0]['league']['name'] = 'Other league'
            if case == 'time':
                rows[0]['startTime'] = (pipeline['kickoff'] + timedelta(minutes=16)).isoformat()
            if case == 'pair':
                rows[0]['participants'][0]['name'] = 'Czechia Women'
            if case == 'ambiguous':
                rows.append(dict(rows[0], id=2))
            body = json.dumps(rows).encode()
        elif '/markets/straight' in url and case == 'ambiguous':
            rows = json.loads(body)
            rows.append(dict(rows[0], matchupId=2))
            body = json.dumps(rows).encode()
        return code, body, headers

    monkeypatch.setattr(collect, 'http_get', http)
    # Preserve an older CSV header and record byte for byte.
    ledger = pipeline['root'] / 'prospective_ledger.csv'
    historic = b'match_id,snapshot_timestamp_utc,sha256_model_raw\nold,2026-09-01T00:00:00Z,old-hash\n'
    ledger.write_bytes(historic)
    collect.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary['joined_pinnacle'] == summary['eligible'] == 0
    row = read(pipeline, 'SELECT * FROM model_snapshots')[0]
    payload = row['payload']
    assert row['picked_odds'] is None and payload['pin_matchup_id'] == ''
    audit = json.loads(payload['market_match_audit'])
    assert audit['status'] == status and audit['candidate_first_rejections'] == rejections
    state = read(pipeline, "SELECT value FROM runtime_state WHERE key='collector'")[0]['value']
    assert state['market_matching'] == summary['market_matching']
    assert state['market_matching']['model_status_counts'] == {status: 1}
    assert state['market_matching']['candidate_first_rejections'] == rejections
    manifest = json.loads((pipeline['root'] / 'manifests' / ('manifest_' + summary['run_id'] + '.json')).read_text())
    entry = next(f for f in manifest['files'] if f['role'] == 'market-match-audit')
    raw = (pipeline['root'] / entry['path']).read_bytes()
    assert entry['sha256'] == hashlib.sha256(raw).hexdigest() and entry['bytes'] == len(raw)
    report = json.loads(raw)
    assert report['summary'] == state['market_matching']
    assert report['models'][0]['model_raw_sha256'] == row['sha256_model_raw']
    assert report['models'][0]['candidates'] == audit['candidates']
    assert ledger.read_bytes().startswith(historic)
    for table in ('decision_tickets', 'market_decision_tickets', 'source_pilot_predictions'):
        assert read(pipeline, f'SELECT count(*) AS n FROM {table}')[0]['n'] == 0


@pytest.mark.parametrize('case,reason', [
    ('duplicate_price', 'INVALID_PRICES'),
    ('duplicate_participant', 'INVALID_PARTICIPANTS'),
    ('duplicate_fixture', 'MISSING_OR_DUPLICATE_MATCHUP'),
    ('damaged_duplicate_market', 'DUPLICATE_MONEYLINE'),
    ('missing_live_flag', 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('wrong_units', 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('bool_period', 'INVALID_MARKET_CONTRACT'),
    ('wrong_key', 'INVALID_MARKET_CONTRACT'),
    ('expired_cutoff', 'INVALID_PREMATCH_TIME_OR_CUTOFF'),
    ('malformed_prices', 'INVALID_PRICES'),
    ('source_league_mismatch', 'SOURCE_LEAGUE_OR_SPORT_MISMATCH'),
])
def test_moneyline_rejection_survives_real_postgres_pipeline(pipeline, monkeypatch, capsys, case, reason):
    original = pipeline['http_fixture']

    def http(url, **kwargs):
        code, body, headers = original(url, **kwargs)
        if '/matchups?' in url:
            rows = json.loads(body)
            if case == 'duplicate_participant': rows[0]['participants'].append(rows[0]['participants'][0])
            if case == 'duplicate_fixture': rows.append(dict(rows[0]))
            if case == 'missing_live_flag': rows[0].pop('isLive')
            if case == 'wrong_units': rows[0]['units'] = 'Corners'
            if case == 'source_league_mismatch': rows[0]['league']['id'] = 2
            body = json.dumps(rows).encode()
        if '/markets/straight' in url:
            rows = json.loads(body)
            if case == 'duplicate_price': rows[0]['prices'].append({'designation': 'home', 'price': 1000})
            if case == 'damaged_duplicate_market': rows.append(dict(rows[0], prices=None))
            if case == 'bool_period': rows[0]['period'] = False
            if case == 'wrong_key': rows[0]['key'] = 's;1;m'
            if case == 'expired_cutoff': rows[0]['cutoffAt'] = pipeline['now'].isoformat()
            if case == 'malformed_prices': rows[0]['prices'] = {'home': 100}
            body = json.dumps(rows).encode()
        return code, body, headers

    monkeypatch.setattr(collect, 'http_get', http)
    ledger = pipeline['root'] / 'prospective_ledger.csv'
    historic = b'match_id,snapshot_timestamp_utc,sha256_model_raw\nold,2026-09-01T00:00:00Z,old-hash\n'
    ledger.write_bytes(historic)
    collect.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary['valid_prematch'] == 1 and summary['joined_pinnacle'] == summary['eligible'] == 0
    row = read(pipeline, 'SELECT * FROM model_snapshots')[0]
    assert row['picked_odds'] is None and not row['analysis_eligible']
    assert all(row['payload'][k] == '' for k in ('odds_home', 'odds_draw', 'odds_away', 'sha256_odds_raw'))
    state = read(pipeline, "SELECT value FROM runtime_state WHERE key='collector'")[0]['value']
    audit = state['pinnacle_moneyline_validation']
    assert audit == summary['pinnacle_moneyline_validation'] and audit['accepted'] == 0
    assert audit['rejected'] == {reason: 2 if case == 'damaged_duplicate_market' else 1}
    manifest = json.loads((pipeline['root'] / 'manifests' / ('manifest_' + summary['run_id'] + '.json')).read_text())
    entry = next(f for f in manifest['files'] if f['role'] == 'pin-moneyline-audit')
    raw = (pipeline['root'] / entry['path']).read_bytes()
    assert entry['sha256'] == hashlib.sha256(raw).hexdigest() and entry['bytes'] == len(raw)
    assert json.loads(raw)['summary'] == audit == manifest['counts']['pinnacle_moneyline_validation']
    assert ledger.read_bytes().startswith(historic)
    for table in ('decision_tickets', 'market_decision_tickets', 'source_pilot_predictions'):
        assert read(pipeline, f'SELECT count(*) AS n FROM {table}')[0]['n'] == 0


@pytest.mark.parametrize('bridge_code', [200, 401, 503])
def test_initial_bridge_transport_recovery_or_failure_has_safe_persistent_diagnostics(pipeline, monkeypatch, capsys, bridge_code):
    from io import BytesIO
    from urllib.error import HTTPError
    original = pipeline['http_fixture']
    def direct(url, **kwargs):
        if url == collect.PIN_LEAGUES:return 403, b'direct denied', {}
        return original(url, **kwargs)
    monkeypatch.setattr(collect, 'http_get', direct)
    monkeypatch.setattr(collect, 'fetch_pinnacle_leagues_via_bridge',
                        pipeline['fetch_leagues'])
    # The pipeline fixture forbids fallback; restore the real function for this test.
    monkeypatch.setenv('COLLECTOR_BRIDGE_TOKEN', 'synthetic-token')
    calls = []
    def open(req, timeout):
        calls.append(timeout)
        if len(calls) == 1 or bridge_code != 200:
            code = 503 if bridge_code == 200 else bridge_code
            raise HTTPError(req.full_url, code, 'private error message', {}, BytesIO(b'private body'))
        data = {'http_status':200, 'truncated':False,
                'body':json.dumps([{'id':1,'name':'Test league','matchupCount':1}])}
        class Response:
            status = 200
            def __enter__(self):return self
            def __exit__(self, *args):pass
            def read(self):return json.dumps(data).encode()
        return Response()
    monkeypatch.setattr(collect, 'urlopen', open)
    monkeypatch.setattr(collect.time, 'sleep', lambda seconds:None)
    collect.main(); summary = json.loads(capsys.readouterr().out)
    state = read(pipeline, "SELECT value FROM runtime_state WHERE key='collector'")[0]['value']
    assert 'synthetic-token' not in json.dumps(state)
    assert 'private error message' not in json.dumps(state) and 'private body' not in json.dumps(state)
    attempts = state['pinnacle_leagues_bridge_requests']
    if bridge_code == 200:
        assert [a['http_status'] for a in attempts] == [503,200]
        assert state['pinnacle_leagues_failure'] is None
        assert summary['joined_pinnacle'] == 1
        assert read(pipeline, 'SELECT picked_odds FROM model_snapshots')[0]['picked_odds'] == 2
    else:
        assert len(attempts) == (4 if bridge_code == 503 else 1)
        assert state['pinnacle_leagues_failure'] == {'direct_http_status':403,
            'bridge_error_type':'HTTPError', 'bridge_http_status':bridge_code}
        assert summary['joined_pinnacle'] == 0
        assert read(pipeline, 'SELECT picked_odds FROM model_snapshots')[0]['picked_odds'] is None
    audit = json.loads(next(pipeline['root'].rglob('leagues_transport_audit.json')).read_text())
    assert audit['bridge_requests'] == attempts and audit['failure'] == state['pinnacle_leagues_failure']


def test_collect_freeze_settle_retry_correction_and_field_verification(pipeline, capsys):
    collect.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary['valid_prematch'] == summary['joined_pinnacle'] == 1
    assert summary['database']['snapshots']['result'] == 1
    snapshots = read(pipeline, 'SELECT * FROM model_snapshots')
    assert len(snapshots) == 1 and snapshots[0]['model_pick_probability'] == .6
    assert snapshots[0]['picked_odds'] == 2.0
    row = snapshots[0]['payload']
    assert row['kickoff_raw'] == pipeline['kickoff'].isoformat()
    assert row['timezone_status'] == 'EXPLICIT_OFFSET'
    assert row['source_time_evidence'] == 'SportsEvent.startDate'
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


def test_new_time_audit_does_not_corrupt_legacy_cumulative_csv(pipeline,capsys):
    path=pipeline['root']/'prospective_ledger.csv'
    old_fields=['match_id','snapshot_timestamp_utc','sha256_model_raw','model_pick',
                'p_home_normalized','p_draw_normalized','p_away_normalized']
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=old_fields);writer.writeheader()
        writer.writerow({'match_id':'synthetic-legacy','snapshot_timestamp_utc':'old','sha256_model_raw':'old'})
    original=path.read_bytes()
    collect.main();capsys.readouterr()
    assert path.read_bytes().startswith(original)
    with path.open() as f:
        reader=csv.DictReader(f);rows=list(reader)
        assert reader.fieldnames==old_fields and len(rows)==2 and all(None not in r for r in rows)
        assert rows[1]['model_pick']=='1' and rows[1]['p_home_normalized']=='0.6'
    snapshot=read(pipeline,'SELECT payload FROM model_snapshots')[0]['payload']
    assert snapshot['timezone_status']=='EXPLICIT_OFFSET'
    slices=list(pipeline['root'].glob('snapshots/*/prospective_ledger_slice.csv'))
    # Slice path is discovered rather than replacing the existing ledger/header.
    if not slices:slices=list(pipeline['root'].rglob('prospective_ledger_slice.csv'))
    assert slices
    with slices[0].open() as f:
        row=next(csv.DictReader(f));assert row['kickoff_raw']==pipeline['kickoff'].isoformat()


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
    # Include the real DB seal in knowledge time before testing the separate
    # pre-kickoff gate. A fixed 17:00 knowledge time becomes stale after 17:00.
    known_at = max(pipeline['kickoff'] + timedelta(hours=1),
                   sealed['imported_at'] + timedelta(seconds=1))
    result = get_market_reference(sealed['match_id'], known_at=known_at)
    assert result['status'] == 'CLOSED_OBSERVED_PROXY'
    assert result['q_close']['odds']['1'] == 2.1
    assert result['rejected']['NOT_SEALED_PRE_KICKOFF'] == 1 + int(sealed['imported_at'] >= pipeline['kickoff'])
    unseen_at = min(sealed['imported_at'], later) - timedelta(seconds=1)
    unseen = get_market_reference(sealed['match_id'], known_at=unseen_at)
    assert unseen['rejected']['NOT_KNOWN_AT'] == len(original)
    assert unseen['q_open'] is None and unseen['q_close'] is None
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
            if case == 'league':
                data[0]['league']['name'] = 'Different league'
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
