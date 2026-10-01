"""Upgrade a populated database; server seal cannot retroactively certify legacy rows."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL
import pytest

from app.migrations import apply_migrations, MIGRATIONS


def test_populated_upgrade_new_server_time_immutability_and_replay(tmp_path):
    dsn=os.environ.get('TEST_DATABASE_URL')
    if not dsn:pytest.skip('TEST_DATABASE_URL required for real PostgreSQL integration')
    schema='sealing_test_'+uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(SQL('CREATE SCHEMA {}').format(Identifier(schema)))
    def connect():
        return psycopg.connect(dsn,row_factory=dict_row,options=f'-c search_path={schema}')
    older=tmp_path/'v4';older.mkdir()
    for p in sorted(MIGRATIONS.glob('*.sql'))[:4]:(older/p.name).write_bytes(p.read_bytes())
    apply_migrations(connect,older)
    now=datetime.now(timezone.utc)
    run_sql='''INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,started_at_utc,finished_at_utc,status)
               VALUES (%s,'synthetic-pilot','synthetic-model',%s,%s,%s,'OK')'''
    pred_sql='''INSERT INTO source_pilot_predictions(run_id,source,source_match_key,home_team,away_team,payload)
                VALUES (%s,'synthetic-model','key','Synthetic Home','Synthetic Away','{}')'''
    with connect() as conn:
        conn.execute(run_sql,('legacy',1,now,now));conn.execute(pred_sql,('legacy',))
        before_run=conn.execute('SELECT to_jsonb(r) AS row FROM source_pilot_runs r').fetchone()['row']
        before_pred=conn.execute('SELECT to_jsonb(p) AS row FROM source_pilot_predictions p').fetchone()['row']
    apply_migrations(connect)
    with connect() as conn:
        legacy_run=conn.execute('SELECT to_jsonb(r) AS row FROM source_pilot_runs r').fetchone()['row']
        legacy_pred=conn.execute('SELECT to_jsonb(p) AS row FROM source_pilot_predictions p').fetchone()['row']
        assert legacy_run.pop('sealed_at_utc') is None and legacy_run==before_run
        assert legacy_pred.pop('sealed_at_utc') is None and legacy_pred==before_pred
        lower=conn.execute('SELECT clock_timestamp() AS at').fetchone()['at']
        conn.execute(run_sql,('fresh',2,now,now));conn.execute(pred_sql,('fresh',))
        upper=conn.execute('SELECT clock_timestamp() AS at').fetchone()['at']
        seals=conn.execute("SELECT r.sealed_at_utc AS run_seal,p.sealed_at_utc AS row_seal FROM source_pilot_runs r JOIN source_pilot_predictions p USING(run_id) WHERE run_id='fresh'").fetchone()
        assert lower<=seals['run_seal']<=seals['row_seal']<=upper
    for table in ('source_pilot_runs','source_pilot_predictions'):
        for sql in (f"UPDATE {table} SET sealed_at_utc=NOW() WHERE run_id='legacy'",
                    f"UPDATE {table} SET source='changed' WHERE run_id='fresh'",
                    f"DELETE FROM {table} WHERE run_id='fresh'",f'TRUNCATE {table} CASCADE'):
            with connect() as conn:
                with pytest.raises(psycopg.errors.RaiseException,match='append-only'):conn.execute(sql)
                conn.rollback()
    with connect() as conn:
        with pytest.raises(psycopg.errors.RaiseException,match='server-owned'):
            conn.execute('''INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,started_at_utc,status,sealed_at_utc)
                        VALUES ('forged','synthetic-pilot','synthetic-model',3,%s,'OK',%s)''',(now,now-timedelta(days=1)))
        conn.rollback()
    apply_migrations(connect)
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM source_pilot_runs').fetchone()['n']==2
        assert conn.execute('SELECT count(*) AS n FROM source_pilot_predictions').fetchone()['n']==2
        assert conn.execute("SELECT sealed_at_utc FROM source_pilot_predictions WHERE run_id='legacy'").fetchone()['sealed_at_utc'] is None
