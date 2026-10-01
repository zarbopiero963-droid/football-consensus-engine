import json

import pytest
import psycopg

from app import api, runtime_health as health
from prospective import collect
from test_collect_settle_postgres import pipeline


def test_health_detects_illegal_settlement_pilot_leak_and_parser_time_issues(pipeline, monkeypatch, capsys):
    monkeypatch.setattr(health, 'connect', pipeline['connect'])
    collect.main()
    capsys.readouterr()
    api.create_decision_batch(pipeline['kickoff'].date(), api.DecisionRequest(),
                              requested_at=pipeline['now'], idempotency_key='health-test')
    pipeline['cycle']([pipeline['fifa']])
    with pipeline['connect']() as conn:
        conn.execute("UPDATE market_decision_tickets SET status='WON'")
        conn.execute("""INSERT INTO model_snapshots(match_id,snapshot_timestamp_utc,kickoff_utc,
                     home_team,away_team,valid_prematch,payload)
                     VALUES ('test-pilot-leak',NOW(),NOW()-INTERVAL '1 hour','Test','Other',TRUE,
                             '{"pilot_id":"test-pilot","timezone_status":"TIMEZONE_UNRESOLVED"}')""")
        for source in ['test-broken', 'test-empty']:
            conn.execute("""INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,started_at_utc,status)
                            VALUES (%s,'test-pilot',%s,1,NOW(),'OK')""", (source, source))
        conn.execute("""INSERT INTO source_pilot_predictions(run_id,source,source_match_key,home_team,
                         away_team,parse_status,timezone_status,kickoff_utc)
                         VALUES ('test-broken','test-broken','test-row','Test','Other','BROKEN',
                                 'VERIFIED_BROWSER_TZ',NOW())""")
        counts_before = conn.execute('SELECT count(*) AS n FROM model_snapshots').fetchone()['n']
    report = health.runtime_health()
    assert report['status'] == 'FAIL' and not report['ready']
    assert report['counts']['illegal_market_settlements'] == 2
    assert report['counts']['pilot_or_unknown_snapshots'] == report['counts']['unsafe_production_time'] == 1
    assert report['counts']['broken_pilot_parsers'] == report['counts']['zero_row_pilot_parsers'] == 1
    assert report['counts']['unresolved_pilot_timezone_rows'] == 1
    with pipeline['connect']() as conn:
        assert conn.execute('SELECT count(*) AS n FROM model_snapshots').fetchone()['n'] == counts_before
        assert conn.execute("SELECT count(*) AS n FROM market_decision_tickets WHERE status='WON'").fetchone()['n'] == 2


def test_health_transaction_rejects_accidental_write(pipeline, monkeypatch):
    monkeypatch.setattr(health, 'connect', pipeline['connect'])
    monkeypatch.setattr(health, 'COUNTERS_SQL', "INSERT INTO runtime_state(key,value) VALUES ('illegal','{}')")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        health.runtime_health()
    with pipeline['connect']() as conn:
        assert conn.execute("SELECT count(*) AS n FROM runtime_state WHERE key='illegal'").fetchone()['n'] == 0
