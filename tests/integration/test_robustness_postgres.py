"""Real PostgreSQL with synthetic evidence; server enforces write denial."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL
import pytest

from app.migrations import apply_migrations
from staging.consensus_robustness.data import load_rows
from staging.consensus_robustness.harness import report


def test_real_readonly_adapter_and_source_gate():
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL required for real PostgreSQL integration')
    schema = 'robustness_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f'-c search_path={schema}')

    apply_migrations(connect)
    now = datetime.now(timezone.utc)
    captured, kickoff = now - timedelta(minutes=1), now + timedelta(hours=2)
    with connect() as conn:
        conn.execute('''INSERT INTO model_snapshots
            (match_id,snapshot_timestamp_utc,kickoff_utc,home_team,away_team,valid_prematch,payload)
            VALUES ('synthetic',%s,%s,'Synthetic Home','Synthetic Away',TRUE,%s::jsonb)''',
            (captured, kickoff, json.dumps({'p_home_normalized': .6, 'p_draw_normalized': .25,
                                            'p_away_normalized': .15,
                                            'source_url': 'https://footinsights.com/predictions/synthetic/'})))
        conn.execute('''INSERT INTO source_pilot_runs
            (run_id,pilot_id,source,slot_no,started_at_utc,finished_at_utc,status)
            VALUES ('synthetic-run','synthetic-pilot','synthetic-model',1,%s,%s,'OK')''', (captured, captured))
        conn.execute('''INSERT INTO source_pilot_predictions
            (run_id,source,source_match_key,home_team,away_team,kickoff_utc,
             timezone_status,p1,px,p2,payload)
            VALUES ('synthetic-run','synthetic-model','key','Synthetic Home','Synthetic Away',
                    %s,'EXPLICIT_OFFSET',55,30,15,%s::jsonb)''',
            (kickoff, json.dumps({'sealed_at_utc': now.isoformat(), 'provenance': 'SYNTHETIC_TEST'})))
        conn.execute('''INSERT INTO source_pilot_matches(run_id,source_match_key,anchor_match_id,match_status)
                        VALUES ('synthetic-run','key','synthetic','EXACT_PAIR_TIME_CONTEXT')''')
        # A pilot/market payload in the anchor table must not become FootInsights.
        conn.execute('''INSERT INTO model_snapshots
            (match_id,snapshot_timestamp_utc,kickoff_utc,home_team,away_team,valid_prematch,payload)
            SELECT 'contaminated',snapshot_timestamp_utc,kickoff_utc,home_team,away_team,TRUE,
                   payload || '{"source":"pinnacle"}'::jsonb
            FROM model_snapshots WHERE match_id='synthetic' ''')
    readonly_checks = []

    @contextmanager
    def audited_connect():
        with connect() as conn:
            yield conn
            assert conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] == 'on'
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("INSERT INTO runtime_state(key,value) VALUES ('forbidden','{}'::jsonb)")
            readonly_checks.append(True)
            conn.rollback()

    unknown = report(load_rows(audited_connect), known_at=kickoff)
    assert unknown['counts']['multisource_matches'] == 0
    included = report(load_rows(audited_connect, source_roles={'synthetic-model': 'MODEL_CANDIDATE'}),
                      known_at=kickoff)
    assert included['counts']['multisource_matches'] == 1
    assert included['matches'][0]['diagnostics']['linear']['vector'] == pytest.approx([.575, .275, .15])
    assert included['sample_status'] == 'INSUFFICIENT_SAMPLE'
    assert readonly_checks == [True, True]
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM runtime_state').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM model_snapshots').fetchone()['n'] == 2
        assert conn.execute('SELECT count(*) AS n FROM source_pilot_predictions').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) AS n FROM decision_tickets').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM strategy_simulations').fetchone()['n'] == 0
