"""Real PostgreSQL HTTP/transaction tests; opt in with TEST_DATABASE_URL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import uuid
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL
import pytest

from app import api, market_engine, strategy_sync
from app.migrations import apply_migrations


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL required for real PostgreSQL integration')
    schema = 'decision_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f'-c search_path={schema}')

    apply_migrations(connect)
    for module in (api, market_engine, strategy_sync):
        monkeypatch.setattr(module, 'connect', connect)
    now = datetime.now(timezone.utc)
    kickoff = now + timedelta(days=1)
    day = kickoff.astimezone(ZoneInfo('Europe/Rome')).date().isoformat()
    payload = {'model_pick': '1', 'model_pick_probability': .6,
               'p_home_normalized': .6, 'p_draw_normalized': .25, 'p_away_normalized': .15,
               'analysis_eligible': '1', 'same_as_favorite': '1', 'picked_odds': 1.8,
               'picked_edge': .05, 'odds_home': 1.8, 'odds_draw': 3.5, 'odds_away': 5,
               'fair_home': .55, 'fair_draw': .27, 'fair_away': .18}

    def seed(stamp, data):
        with connect() as conn:
            conn.execute('''INSERT INTO model_snapshots(match_id,snapshot_timestamp_utc,kickoff_utc,
                         home_team,away_team,valid_prematch,payload)
                         VALUES ('integration-match',%s,%s,'Test A','Test B',TRUE,%s::jsonb)''',
                         (stamp, kickoff, json.dumps(data)))

    seed(now - timedelta(minutes=2), payload)

    def counts():
        with connect() as conn:
            return {table: conn.execute(SQL('SELECT count(*) AS n FROM {}').format(Identifier(table))).fetchone()['n']
                    for table in ('decision_requests', 'decision_batches', 'decision_tickets',
                                  'market_decision_tickets', 'strategy_simulations')}

    return {'connect': connect, 'path': '/decisions/date/' + day, 'payload': payload,
            'seed': seed, 'now': now, 'counts': counts}


def post(database, key='one', body=None):
    with TestClient(api.app, raise_server_exceptions=False) as client:
        return client.post(database['path'], json=body or {}, headers={'Idempotency-Key': key})


def assert_one_decision(database, response):
    count = database['counts']()
    assert count['decision_requests'] == count['decision_batches'] == count['decision_tickets'] == 1
    assert count['market_decision_tickets'] == len(response['markets']) == 2
    assert count['strategy_simulations'] == len(response['matches'][0]['strategies'])


def test_http_retry_preserves_response_after_new_snapshot(database):
    first = post(database)
    assert first.status_code == 200, first.text
    changed = {**database['payload'], 'model_pick': '2', 'picked_odds': 9.9}
    database['seed'](database['now'] - timedelta(seconds=1), changed)
    retry = post(database)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert first.json()['matches'][0]['picked_odds'] == 1.8
    assert_one_decision(database, first.json())


def test_eight_concurrent_http_retries_produce_one_stake_set(database):
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post(database), range(8)))
    assert [r.status_code for r in responses] == [200] * 8, [r.text for r in responses]
    assert all(r.json() == responses[0].json() for r in responses)
    assert_one_decision(database, responses[0].json())


def test_payload_or_date_conflict_does_not_create_new_stake(database):
    first = post(database)
    assert first.status_code == 200, first.text
    assert post(database, body={'label': 'changed'}).status_code == 409
    with TestClient(api.app) as client:
        assert client.post('/decisions/date/2099-01-01', json={}, headers={'Idempotency-Key': 'one'}).status_code == 409
    assert_one_decision(database, first.json())


def test_market_failure_rolls_back_entire_freeze_and_retry_recovers(database, monkeypatch):
    original = api.freeze_market_tickets

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('injected failure after market insert')

    monkeypatch.setattr(api, 'freeze_market_tickets', fail)
    assert post(database).status_code == 500
    assert all(n == 0 for n in database['counts']().values())
    monkeypatch.setattr(api, 'freeze_market_tickets', original)
    result = post(database)
    assert result.status_code == 200, result.text
    assert_one_decision(database, result.json())


def test_post_commit_projection_failure_retries_same_frozen_batch(database, monkeypatch):
    original = api.synchronize_strategy_simulations
    monkeypatch.setattr(api, 'synchronize_strategy_simulations', lambda: (_ for _ in ()).throw(RuntimeError('projection failure')))
    assert post(database).status_code == 500
    with database['connect']() as conn:
        saved = conn.execute('SELECT response FROM decision_requests').fetchone()['response']
    monkeypatch.setattr(api, 'synchronize_strategy_simulations', original)
    retry = post(database)
    assert retry.status_code == 200, retry.text
    assert retry.json() == saved
    assert_one_decision(database, saved)


def test_new_key_is_an_intentional_new_decision(database):
    first, second = post(database, 'one'), post(database, 'two')
    assert first.status_code == second.status_code == 200
    assert first.json()['batch_id'] != second.json()['batch_id']
    counts = database['counts']()
    assert counts['decision_requests'] == counts['decision_batches'] == counts['decision_tickets'] == 2
    assert counts['market_decision_tickets'] == 4
