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


def test_latest_prediction_and_result_http_failures_are_visible_despite_parsed_rows(pipeline, monkeypatch):
    monkeypatch.setattr(health, 'connect', pipeline['connect'])
    with pipeline['connect']() as conn:
        conn.execute("""INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,
                        started_at_utc,status,http_status,manifest)
                        VALUES ('opta404','http-test','opta',1,NOW(),'OK',404,'{"rows_parsed":24}')""")
        conn.execute("""INSERT INTO source_pilot_predictions(run_id,source,source_match_key,
                        home_team,away_team,parse_status)
                        VALUES ('opta404','opta','public-shell-row','Home','Away','OK')""")
        conn.execute("""INSERT INTO result_source_pilot_runs(run_id,pilot_id,source,slot_no,
                        started_at_utc,status,http_status,rows_found)
                        VALUES ('result403','http-test','result',1,NOW(),'OK',403,20),
                               ('result-null','http-test','unobserved',1,NOW(),'OK',NULL,20)""")
    first = health.runtime_health()
    assert first['counts']['failed_pilot_transports'] == 3
    assert first['counts']['broken_pilot_parsers'] == first['counts']['zero_row_pilot_parsers'] == 0
    assert 'failed_pilot_transports' in first['warnings']
    with pipeline['connect']() as conn:
        conn.execute("""INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,
                        started_at_utc,status,http_status)
                        VALUES ('opta200','http-test','opta',2,NOW(),'OK',200)""")
        conn.execute("""INSERT INTO result_source_pilot_runs(run_id,pilot_id,source,slot_no,
                        started_at_utc,status,http_status)
                        VALUES ('result200','http-test','result',2,NOW(),'OK',200),
                               ('observed200','http-test','unobserved',2,NOW(),'OK',200)""")
    assert health.runtime_health()['counts']['failed_pilot_transports'] == 0
    with pipeline['connect']() as conn:
        assert conn.execute("SELECT http_status FROM source_pilot_runs WHERE run_id='opta404'").fetchone()['http_status'] == 404
        assert conn.execute("SELECT count(*) AS n FROM source_pilot_predictions").fetchone()['n'] == 1
