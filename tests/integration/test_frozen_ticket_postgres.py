"""Real PostgreSQL: immutable prospective registry and exact-ticket read path."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import os
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL
import pytest

from app.migrations import apply_migrations
from analysis.decisions.data import load_dataset
from analysis.decisions.protocol import register
from analysis.decisions.report import report
from tests.test_frozen_ticket_scoring import PROTOCOL, ticket


@pytest.fixture
def db():
    dsn=os.environ.get('TEST_DATABASE_URL')
    if not dsn: pytest.skip('TEST_DATABASE_URL required for actual PostgreSQL')
    schema='ticket_scoring_'+uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))
    def connect():
        return psycopg.connect(dsn,row_factory=dict_row,options=f'-c search_path={schema}')
    apply_migrations(connect)
    # Test schemas are isolated and intentionally retain evidence until CI teardown.
    return connect


def test_registry_server_time_hash_replay_future_gate_and_immutability(db):
    now=datetime.now(timezone.utc)
    future=replace(PROTOCOL,window_start=(now+timedelta(days=1)).isoformat(),
                   window_end=(now+timedelta(days=29)).isoformat())
    row=register('trial',future,connection_factory=db)
    assert row['sealed_at_utc']>=now and row['sealed_at_utc']<now+timedelta(days=1)
    assert row['policy_sha256']==future.sha256()
    assert register('trial',future,connection_factory=db)==row
    with pytest.raises(ValueError,match='DIFFERENT_POLICY'):
        register('trial',replace(future,min_roi=.03),connection_factory=db)
    for statement in ("UPDATE evaluation_protocols SET policy_json='{}' WHERE protocol_id='trial'",
                      "DELETE FROM evaluation_protocols WHERE protocol_id='trial'",
                      'TRUNCATE evaluation_protocols'):
        with pytest.raises(psycopg.errors.RaiseException,match='append-only'), db() as conn:
            conn.execute(statement)
    past=replace(future,window_start=(now-timedelta(days=2)).isoformat(),
                 window_end=(now-timedelta(days=1)).isoformat())
    with pytest.raises(psycopg.errors.RaiseException,match='future window'):
        register('late',past,connection_factory=db)
    with pytest.raises(psycopg.errors.RaiseException,match='server-owned'), db() as conn:
        conn.execute('''INSERT INTO evaluation_protocols(protocol_id,window_start,window_end,policy_json,sealed_at_utc)
            VALUES ('backdate',%s,%s,%s,%s)''',
            (future.window_start,future.window_end,future.canonical(),now-timedelta(days=1)))
    with pytest.raises(psycopg.errors.RaiseException,match='Complete evaluation'), db() as conn:
        conn.execute('''INSERT INTO evaluation_protocols(protocol_id,window_start,window_end,policy_json)
            VALUES ('partial',%s,%s,'{}')''',(future.window_start,future.window_end))
    apply_migrations(db)
    with db() as conn:
        assert conn.execute('SELECT * FROM evaluation_protocols').fetchall()==[row]


def test_exact_ticket_sql_later_snapshots_prices_cutoff_and_readonly(db):
    now=datetime.now(timezone.utc); kick=now-timedelta(days=1); capture=kick-timedelta(hours=3)
    request=kick-timedelta(hours=2)
    protocol=replace(PROTOCOL,window_start=(kick-timedelta(days=2)).isoformat(),
                     window_end=(kick+timedelta(days=2)).isoformat())
    t=ticket(); original=t['original_snapshot']; payload=original['payload']
    payload.update(kickoff_raw=kick.isoformat(),pin_kickoff_utc=kick.isoformat(),
                   pin_snapshot_timestamp_utc=(capture+timedelta(seconds=1)).isoformat())
    view=t['snapshot_view']; view.update(kickoff_utc=kick.isoformat(),snapshot_timestamp_utc=capture.isoformat())
    with db() as conn:
        for at, p, odds in ((capture,(.6,.25,.15),2),(request+timedelta(minutes=10),(.8,.1,.1),3)):
            data={**payload,'p_home_normalized':p[0],'p_draw_normalized':p[1],'p_away_normalized':p[2],
                  'odds_home':odds,'pin_snapshot_timestamp_utc':(at+timedelta(seconds=1)).isoformat()}
            conn.execute('''INSERT INTO model_snapshots(match_id,snapshot_timestamp_utc,kickoff_utc,
                home_team,away_team,league,valid_prematch,payload,imported_at)
                VALUES ('a',%s,%s,'Home','Away','League',TRUE,%s::jsonb,%s)''',
                (at,kick,json.dumps(data),at+timedelta(seconds=2)))
        conn.execute('''INSERT INTO decision_batches(batch_id,requested_at_utc,requested_date,created_at)
            VALUES ('first',%s,%s,%s)''',(request,request.date(),request+timedelta(seconds=1)))
        conn.execute('''INSERT INTO decision_tickets(batch_id,match_id,snapshot_timestamp_utc,kickoff_utc,
            home_team,away_team,league,model_pick,model_probability,picked_odds,strategies,display_status,
            snapshot_view,shown_to_user,created_at)
            VALUES ('first','a',%s,%s,'Home','Away','League','1',.6,2,'["S1","S2"]','BET',%s::jsonb,TRUE,%s)''',
            (capture,kick,json.dumps(view),request+timedelta(seconds=1)))
        for provider in ('source-a','source-b'):
            conn.execute('''INSERT INTO result_observations(observation_id,match_id,provider,
                observed_at_utc,kickoff_utc,status,final_home_goals,final_away_goals)
                VALUES (%s,'a',%s,%s,%s,'FT',2,0)''',(provider,provider,kick+timedelta(hours=2),kick))
        conn.execute('''INSERT INTO result_observations(observation_id,match_id,provider,observed_at_utc,
            kickoff_utc,status,final_home_goals,final_away_goals)
            VALUES ('future','a','source-a',%s,%s,'FT',0,2)''',(now+timedelta(days=1),kick))
        before=conn.execute('SELECT md5(string_agg(snapshot_view::text,\'\' ORDER BY batch_id,match_id)) AS hash FROM decision_tickets').fetchone()
    @contextmanager
    def readonly():
        with db() as conn:
            yield conn
            assert conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'
            assert conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation']=='repeatable read'
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("INSERT INTO runtime_state(key,value) VALUES ('forbidden','{}')")
            conn.rollback()
    dataset, registration=load_dataset(known_at=now,protocol=protocol,connection_factory=readonly)
    result=report(dataset,known_at=now,protocol=protocol,registration=registration)
    assert result['n']==1 and result['model']['scored_rows'][0]['p_raw']==[.6,.25,.15]
    assert result['roi']['roi']==1 and result['roi']['rows'][0]['picked_odds']==2
    assert result['benchmark']['rows'][0]['benchmark']['p_raw']==[.5,.25,.25]
    assert result['benchmark']['verdict']['status']=='DATI_INSUFFICIENTI'
    late, _=load_dataset(known_at=now+timedelta(days=2),protocol=protocol,connection_factory=db)
    assert report(late,known_at=now+timedelta(days=2),protocol=protocol)['n']==0
    past,_=load_dataset(known_at=capture,protocol=protocol,connection_factory=db)
    assert past['tickets']==[]
    with db() as conn:
        assert conn.execute('SELECT md5(string_agg(snapshot_view::text,\'\' ORDER BY batch_id,match_id)) AS hash FROM decision_tickets').fetchone()==before
        assert conn.execute('SELECT count(*) AS n FROM model_snapshots').fetchone()['n']==2
        assert conn.execute('SELECT count(*) AS n FROM evaluation_protocols').fetchone()['n']==0
