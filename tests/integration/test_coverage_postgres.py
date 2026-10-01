"""Actual SQL, synthetic source/result data and enforced consistent read-only view."""
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
from analysis.coverage.data import load_dataset
from analysis.coverage.report import report


def test_coverage_sql_gates_readonly_and_no_promotion():
    dsn=os.environ.get('TEST_DATABASE_URL')
    if not dsn:pytest.skip('TEST_DATABASE_URL required for real PostgreSQL integration')
    schema='coverage_test_'+uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))
    def connect():
        return psycopg.connect(dsn,row_factory=dict_row,options=f'-c search_path={schema}')
    apply_migrations(connect)
    now=datetime.now(timezone.utc);capture=now-timedelta(days=2);kick=now-timedelta(days=1)
    payload={'source_url':'https://footinsights.com/predictions/synthetic/',
             'p_home_normalized':.6,'p_draw_normalized':.25,'p_away_normalized':.15}
    with connect() as conn:
        conn.execute('''INSERT INTO model_snapshots(match_id,snapshot_timestamp_utc,kickoff_utc,
            home_team,away_team,league,valid_prematch,payload,imported_at)
            VALUES ('synthetic',%s,%s,'Synthetic Home','Synthetic Away','Synthetic League',TRUE,%s::jsonb,%s)''',
            (capture,kick,json.dumps(payload),capture+timedelta(minutes=1)))
        for provider in ('synthetic-a','synthetic-b'):
            conn.execute('''INSERT INTO result_observations(observation_id,match_id,provider,
                observed_at_utc,status,final_home_goals,final_away_goals)
                VALUES (%s,'synthetic',%s,%s,'FT',0,2)''',(provider,provider,kick+timedelta(hours=2)))
        conn.execute('''INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,started_at_utc,
            finished_at_utc,status) VALUES ('synthetic-run','synthetic-pilot','synthetic-model',1,%s,%s,'OK')''',
            (capture,capture+timedelta(minutes=1)))
        conn.execute('''INSERT INTO source_pilot_predictions(run_id,source,source_match_key,home_team,
            away_team,league,p1,px,p2,kickoff_utc,timezone_status,payload)
            VALUES ('synthetic-run','synthetic-model','key','Synthetic Home','Synthetic Away',
                    'Synthetic League',60,25,15,%s,'VERIFIED_BROWSER_TZ','{}')''',(kick,))
    @contextmanager
    def audited():
        with connect() as conn:
            yield conn
            assert conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'
            assert conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation']=='repeatable read'
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("INSERT INTO runtime_state(key,value) VALUES ('forbidden','{}')")
            conn.rollback()
    data=load_dataset(known_at=now,connection_factory=audited)
    result=report(data,known_at=now);competition=result['competitions'][0]
    assert result['denominators']['latest_source_rows']==2
    assert competition['verified_ft_fixtures_last7d']==1
    assert competition['settled_fixtures_last7d']==0
    assert all(s['full_vectors']==1 and s['safe_matches']==0 and s['timezone_resolved']==0
               and not s['production_eligible'] for s in competition['sources'])
    assert competition['state']=='MONITORING_ONLY'
    past=load_dataset(known_at=capture-timedelta(seconds=1),connection_factory=connect)
    assert past=={'predictions':[],'observations':[],'market_tickets':[]}
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM model_snapshots').fetchone()['n']==1
        assert conn.execute('SELECT count(*) AS n FROM market_decision_tickets').fetchone()['n']==0
        assert conn.execute('SELECT count(*) AS n FROM decision_tickets').fetchone()['n']==0
        assert conn.execute('SELECT count(*) AS n FROM strategy_simulations').fetchone()['n']==0
