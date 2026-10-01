"""Real PostgreSQL locks, DB clock, rollback and durable state; controlled browser."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest

from app import result_validation, runtime_health
from app.source_resilience import SourceCircuit, assert_owner, record_skipped, source_key
from app.source_fallback import pilot_fallback_report
from staging.result_sources import pilot48 as results
from test_collect_settle_postgres import pipeline
from test_pilot_content_postgres import Page, TEXT, browser_fixture


def circuit(pipeline):
    return SourceCircuit(pipeline['connect'], source_key('result', 'durable-test', 'source'))


def expire(pipeline, key):
    with pipeline['connect']() as conn:
        conn.execute("""UPDATE runtime_state SET value=jsonb_set(jsonb_set(value,
             '{lease_until_utc}',to_jsonb((clock_timestamp()-interval '1 second')::text)),
             '{retry_at_utc}',to_jsonb((clock_timestamp()-interval '1 second')::text)) WHERE key=%s""", (key,))


def test_concurrent_workers_have_one_owner_and_busy_never_seals_slot(pipeline):
    gate = Barrier(2)
    def claim():
        gate.wait(timeout=5)
        return circuit(pipeline).claim()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(c['decision'] for c in claims) == ['ADMITTED', 'BUSY']
    busy = next(c for c in claims if c['decision'] == 'BUSY')
    record_skipped(pipeline['connect'], 'result', 'durable-test', 'source', 'https://example.org',
                   1, 'BUSY', busy['state'])
    with pipeline['connect']() as conn:
        assert conn.execute('SELECT count(*) AS n FROM result_source_pilot_runs').fetchone()['n'] == 0


def test_restart_durability_single_half_open_probe_and_stale_owner_cannot_commit(pipeline):
    c = circuit(pipeline)
    old = c.claim()
    c.register_browser(old, 'opaque-browser-id')
    expire(pipeline, c.key)
    new = circuit(pipeline).claim()  # New process/object sees the stored lease.
    assert new['state']['mode'] == 'HALF_OPEN'
    assert new['state']['stop_pending_id'] == 'opaque-browser-id'
    assert circuit(pipeline).claim()['decision'] == 'BUSY'
    with pipeline['connect']() as conn, conn.cursor() as cur:
        with pytest.raises(RuntimeError, match='superseded'):
            assert_owner(cur, old)
    with pytest.raises(RuntimeError, match='stale'):
        c.finish(old, {'reason': 'READY_PARSED', 'failed': False})
    c.clear_stopped_browser(new)
    c.finish(new, {'reason': 'READY_PARSED', 'failed': False})
    assert circuit(pipeline).claim()['state']['mode'] == 'CLOSED'


def test_maintenance_suspension_skips_real_adapter_without_new_browser_or_history_mutation(pipeline, monkeypatch):
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'PILOT_ID', 'maintenance-test')
    monkeypatch.setattr(runtime_health, 'connect', pipeline['connect'])
    browser_fixture(monkeypatch, results, Page(['Under maintenance']))
    first = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 1)
    assert first['source_resilience']['mode'] == 'OPEN'
    with pipeline['connect']() as conn:
        before = conn.execute('SELECT to_jsonb(r) AS row FROM result_source_pilot_runs r').fetchone()['row']
    monkeypatch.setattr(results, 'browser_api', lambda *a: pytest.fail('suspended source allocated a browser'))
    second = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 2)
    assert second['status'] == 'CIRCUIT_OPEN' and second['rows'] == 0
    assert runtime_health.runtime_health()['counts']['suspended_pilot_sources'] == 1
    with pipeline['connect']() as conn:
        assert conn.execute('SELECT to_jsonb(r) AS row FROM result_source_pilot_runs r WHERE slot_no=1').fetchone()['row'] == before
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 0
        for table in ('model_snapshots', 'decision_tickets', 'market_decision_tickets'):
            assert conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 0
    key = source_key('result', results.PILOT_ID, 'smart_soccer_stats')
    expire(pipeline, key)
    browser_fixture(monkeypatch, results, Page([TEXT]))
    recovered = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 3)
    assert recovered['page_capture']['state'] == 'READY_PARSED'
    assert recovered['source_resilience']['mode'] == 'CLOSED'


def test_unconfirmed_stop_is_persisted_and_cleanup_precedes_next_browser(pipeline, monkeypatch):
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'PILOT_ID', 'cleanup-test')
    monkeypatch.setattr(runtime_health, 'connect', pipeline['connect'])
    browser_fixture(monkeypatch, results, Page([TEXT]))
    original = results.browser_api
    calls = []
    unconfirmed = [True]
    def api(method, url, body=None):
        calls.append(method)
        if method == 'GET' and unconfirmed[0]: return 200, {'status': 'active'}
        return original(method, url, body)
    monkeypatch.setattr(results, 'browser_api', api)
    failure = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 1)
    assert failure['error_class'] == 'BrowserStopError'
    assert runtime_health.runtime_health()['counts']['pending_pilot_browser_stops'] == 1
    expire(pipeline, source_key('result', results.PILOT_ID, 'smart_soccer_stats'))
    calls.clear()
    unconfirmed[0] = False
    recovered = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 2)
    assert calls[:3] == ['PATCH', 'GET', 'POST']
    assert recovered['source_resilience']['mode'] == 'CLOSED'
    assert recovered['source_resilience']['stop_pending_id'] is None


def test_result_observations_rollback_with_rejected_parent_raw_and_no_pilot_vote(pipeline, monkeypatch):
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'PILOT_ID', 'rollback-test')
    monkeypatch.setattr(results, 'result_anchor_rows', lambda: [{'match_id': 'fixture',
        'home_team': 'Lithuania', 'away_team': 'Andorra',
        'kickoff_utc': datetime(2026, 9, 30, 16, tzinfo=timezone.utc)}])
    browser_fixture(monkeypatch, results, Page([TEXT]))
    with pipeline['connect']() as conn:
        conn.execute("""CREATE FUNCTION reject_parent() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN IF NEW.status='OK' THEN RAISE EXCEPTION 'synthetic parent failure'; END IF; RETURN NEW; END $$""")
        conn.execute('CREATE TRIGGER reject_parent BEFORE INSERT ON result_source_pilot_runs FOR EACH ROW EXECUTE FUNCTION reject_parent()')
    outcome = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 1)
    assert outcome['status'] == 'ERROR'
    with pipeline['connect']() as conn:
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 0
        assert conn.execute('SELECT status FROM result_source_pilot_runs').fetchone()['status'] == 'ERROR'


def test_current_run_fallback_evidence_is_persisted_and_duplicate_slot_does_not_refetch(pipeline, monkeypatch):
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'PILOT_ID', 'fallback-test')
    monkeypatch.setattr(results, 'result_anchor_rows', lambda: [{'match_id': 'fixture',
        'home_team': 'Lithuania', 'away_team': 'Andorra',
        'kickoff_utc': datetime(2026, 9, 30, 16, tzinfo=timezone.utc)}])
    browser_fixture(monkeypatch, results, Page([TEXT]))
    outcome = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 1)
    with pipeline['connect']() as conn:
        manifest = conn.execute('SELECT manifest FROM result_source_pilot_runs').fetchone()['manifest']
        assert manifest['fallback_candidates'][0]['raw_page_sha256']
        assert manifest['fallback_candidates'][0]['final_away_goals'] == 0
    report = pilot_fallback_report(manifest['fallback_candidates'])
    assert report['matches'][0]['fields']['ht_away_goals']['value'] == 0
    assert report['production']['status'] == 'NO_QUALIFIED_FALLBACK'
    monkeypatch.setattr(results, 'browser_api', lambda *a: pytest.fail('immutable slot refetched'))
    duplicate = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 1)
    assert duplicate['status'] == 'ALREADY_DONE' and duplicate['run_id'] == outcome['run_id']


def test_actual_pilot_adapters_route_worldfootball_ft_when_smart_is_down_without_production_vote(pipeline, monkeypatch):
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'PILOT_ID', 'alternative-test')
    monkeypatch.setattr(results, 'result_anchor_rows', lambda: [{'match_id': 'fixture',
        'home_team': 'Juventus', 'away_team': 'Milan', 'league': 'Serie A',
        'kickoff_utc': datetime(2026, 9, 30, 16, tzinfo=timezone.utc)}])
    browser_fixture(monkeypatch, results, Page(['Under maintenance'], http_status=502))
    primary = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 1)
    assert primary['source_resilience']['mode'] == 'OPEN'
    browser_fixture(monkeypatch, results, Page(['30.09.2026\nJuventus\n2:0\nMilan\nStanding']))
    alternative = results.collect_source('worldfootball', results.SOURCES['worldfootball'], 1)
    assert alternative['normalized_observations'] == {'parsed': 1, 'matched': 1, 'persisted': 1}
    with pipeline['connect']() as conn:
        runs = conn.execute('SELECT manifest FROM result_source_pilot_runs WHERE slot_no=1').fetchall()
        candidates = [row for run in runs for row in run['manifest'].get('fallback_candidates', [])]
        assert conn.execute('SELECT provider FROM result_observations').fetchone()['provider'] == 'pilot:worldfootball'
    report = pilot_fallback_report(candidates)
    fields = report['matches'][0]['fields']
    assert fields['final_away_goals']['status'] == 'FALLBACK_SELECTED'
    assert fields['final_away_goals']['value'] == 0
    assert fields['final_away_goals']['provider'] == 'pilot:worldfootball'
    assert fields['final_away_goals']['verification'] == 'SINGLE_SOURCE'
    for field in ('ht_home_goals', 'corners_home', 'ht_corners_home'):
        assert fields[field]['verification'] == 'MISSING' and fields[field]['value'] is None
    production = result_validation.rebuild_consensus('fixture')
    assert production['status'] == 'UNVERIFIED'
    assert all(f['status'] == 'MISSING' for f in production['fields'].values())
