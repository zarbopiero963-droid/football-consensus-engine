"""Actual SQL fixture test; synthetic data stays in an isolated test schema."""
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
from app.market_reference import MATCH_CONTRACT
from analysis.scoring.data import load_dataset
from analysis.scoring.metrics import ScorePolicy
from analysis.scoring.report import report


def test_scoring_real_sql_field_verification_cutoff_and_readonly():
    dsn=os.environ.get('TEST_DATABASE_URL')
    if not dsn:pytest.skip('TEST_DATABASE_URL required for real PostgreSQL integration')
    schema='scoring_test_'+uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))
    def connect():
        return psycopg.connect(dsn,row_factory=dict_row,options=f'-c search_path={schema}')
    apply_migrations(connect)
    now=datetime.now(timezone.utc);kick=now-timedelta(days=1);capture=kick-timedelta(hours=3)
    payload={'source_url':'https://footinsights.com/predictions/synthetic/',
        'p_home_normalized':0,'p_draw_normalized':.4,'p_away_normalized':.6,
        'timezone_status':'EXPLICIT_OFFSET','kickoff_raw':kick.isoformat(),
        'source_time_evidence':'SportsEvent.startDate','pin_matchup_id':'synthetic-pin',
        'sha256_odds_raw':'a'*64,'name_score_note':MATCH_CONTRACT,'pin_home':'Synthetic Home',
        'pin_away':'Synthetic Away','pin_league':'Synthetic League','pin_kickoff_utc':kick.isoformat(),
        'pin_snapshot_timestamp_utc':(capture+timedelta(seconds=1)).isoformat(),
        'odds_home':2,'odds_draw':4,'odds_away':4}
    with connect() as conn:
        for mid in ('synthetic-verified','synthetic-single'):
            conn.execute('''INSERT INTO model_snapshots(match_id,snapshot_timestamp_utc,kickoff_utc,
                home_team,away_team,league,valid_prematch,payload,imported_at)
                VALUES (%s,%s,%s,'Synthetic Home','Synthetic Away','Synthetic League',TRUE,%s::jsonb,%s)''',
                (mid,capture,kick,json.dumps(payload),capture+timedelta(seconds=2)))
            for provider in ('synthetic-a','synthetic-b') if mid=='synthetic-verified' else ('synthetic-a',):
                conn.execute('''INSERT INTO result_observations(observation_id,match_id,provider,
                    observed_at_utc,status,final_home_goals,final_away_goals)
                    VALUES (%s,%s,%s,%s,'FT',0,2)''',(mid+provider,mid,provider,kick+timedelta(hours=2)))
        # A correction exists in the DB, but was not yet known at report cutoff.
        conn.execute('''INSERT INTO result_observations(observation_id,match_id,provider,
            observed_at_utc,status,final_home_goals,final_away_goals)
            VALUES ('future-correction','synthetic-verified','synthetic-a',%s,'FT',3,0)''',
            (now+timedelta(days=1),))
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
    policy=ScorePolicy(1e-6,(0,.5,1),2,50,17,.95)
    r=report(data,known_at=now,methods=('footinsights','pinnacle_open','pinnacle_close'),policy=policy)
    assert r['n']==1 and r['common_match_ids']==['synthetic-verified']
    assert {v['n'] for v in r['methods'].values()}=={1}
    assert r['methods']['footinsights']['scored_rows'][0]['p_raw'][0]==0
    assert r['methods']['footinsights']['scored_rows'][0]['outcome_index']==2
    assert r['methods']['pinnacle_close']['scored_rows'][0]['p_raw']==[.5,.25,.25]
    assert all(v['metrics']['brier']['ci'] is None for v in r['methods'].values())
    past=load_dataset(known_at=capture-timedelta(seconds=1),connection_factory=connect)
    assert past=={'forecasts':[],'observations':[]}
    late=load_dataset(known_at=now+timedelta(days=2),connection_factory=connect)
    assert report(late,known_at=now+timedelta(days=2),methods=('footinsights',),policy=policy)['n']==0
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM model_snapshots').fetchone()['n']==2
        for table in ('decision_tickets','market_decision_tickets','strategy_simulations','source_pilot_predictions'):
            assert conn.execute(SQL('SELECT count(*) AS n FROM {}').format(Identifier(table))).fetchone()['n']==0
