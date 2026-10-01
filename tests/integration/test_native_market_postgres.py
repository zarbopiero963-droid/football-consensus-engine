"""Actual SQL adapter, synthetic raw publication and server read-only guard."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import os
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL
import pytest

from app.migrations import apply_migrations
from staging.market_vectors.data import load_dataset
from staging.market_vectors.native import normalize


def test_native_adapter_selectonly_and_no_history_rewrite():
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn: pytest.skip('TEST_DATABASE_URL required for real PostgreSQL integration')
    schema = 'native_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))
    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f'-c search_path={schema}')
    apply_migrations(connect)
    now = datetime.now(timezone.utc)
    raw = b'<div class="match-card"><span class="match-card-long">Synthetic Home</span><span class="match-card-long">Synthetic Away</span><span class="match-card-stat"><span class="match-card-stat-key">Over 2.5</span><span class="match-card-stat-value">0%</span></span></div>'
    with connect() as conn:
        conn.execute('''INSERT INTO source_pilot_runs
            (run_id,pilot_id,source,slot_no,started_at_utc,finished_at_utc,status,http_status,
             final_url,html_sha256,html_gzip) VALUES ('synthetic-run','synthetic-pilot',
             'statsultra',1,%s,%s,'OK',200,'https://statsultra.com/',%s,%s)''',
            (now-timedelta(minutes=1),now,hashlib.sha256(raw).hexdigest(),gzip.compress(raw)))
        conn.execute('''INSERT INTO source_pilot_predictions
            (run_id,source,source_match_key,home_team,away_team,over25,under25,payload)
            VALUES ('synthetic-run','statsultra','key','Synthetic Home','Synthetic Away',0,100,'{}')''')
    @contextmanager
    def readonly():
        with connect() as conn:
            yield conn
            assert conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] == 'on'
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("INSERT INTO runtime_state(key,value) VALUES ('forbidden','{}')")
            conn.rollback()
    dataset = load_dataset(known_at=now, connection_factory=readonly)
    result = normalize(dataset, known_at=now)
    assert result['records'][0]['vector']['normalized_vector'] == {'OVER': 0, 'UNDER': None}
    assert result['unpublished_opposite_count'] == 1
    assert result['status_counts'] == {'PARTIAL': 1, 'ABSENT': 1}
    assert load_dataset(known_at=now-timedelta(seconds=1), connection_factory=connect) == {'runs': [], 'rows': []}
    with connect() as conn:
        assert conn.execute('SELECT under25 FROM source_pilot_predictions').fetchone()['under25'] == 100
        assert conn.execute('SELECT count(*) AS n FROM decision_tickets').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM market_decision_tickets').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM strategy_simulations').fetchone()['n'] == 0
