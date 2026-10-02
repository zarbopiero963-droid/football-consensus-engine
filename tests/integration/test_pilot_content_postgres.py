"""Real PostgreSQL ingestion; controlled CDP pages, no production/network writes."""
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import runtime_health
from staging.page_capture import capture_page, navigate_capture
from staging.result_sources import pilot48 as results
from staging.issue29_pilot import pilot48 as predictions
from staging.browser_use_shortlist import pilot48 as shortlist
from test_collect_settle_postgres import pipeline


TEXT = '30 Sept 2026\nLithuania\n3\n-\n0\nAndorra\n(2-0)\nFriendlies - World, 2026'
PRED = '12:00\nLithuania\nAndorra\nFriendly International\n54%\n25%\n22%\n48%\n52%'


class Page:
    def __init__(self, texts, http_status=200, html=None):
        self.texts, self.http_status = texts, http_status
        self.html = html
        self.index, self.clock = 0, 0
        self.url = 'https://public-source.example/fixture'

    def on(self, *args): pass
    def goto(self, url, **kwargs):
        self.url = url
        self.index = 0
        if url.endswith('/tomorrow') and hasattr(self, 'tomorrow_text'):
            self.texts = [self.tomorrow_text]
        return SimpleNamespace(status=self.http_status, header_value=lambda _: None)
    def locator(self, _): return self
    def inner_text(self, **kwargs): return self.texts[self.index]
    def title(self): return 'Source Fixture'
    def content(self): return self.html if self.html is not None else '<html>' + self.texts[self.index] + '</html>'
    def screenshot(self, **kwargs): return b'synthetic-screenshot'
    def wait_for_timeout(self, ms):
        self.clock += ms / 1000
        self.index = min(self.index + 1, len(self.texts) - 1)


def browser_fixture(monkeypatch, module, page):
    from playwright import sync_api
    context = SimpleNamespace(pages=[page], new_cdp_session=lambda _: SimpleNamespace(send=lambda *a: None))
    browser = SimpleNamespace(contexts=[context], close=lambda: None)
    @contextmanager
    def sync_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda _: browser))
    monkeypatch.setattr(sync_api, 'sync_playwright', sync_playwright)
    stopped = []
    def browser_api(method, url, body=None):
        if method == 'POST': return 201, {'id': 'synthetic-browser', 'cdpUrl': 'synthetic-memory-only'}
        if method == 'PATCH': stopped.append(body); return 200, {'status': 'stopped'}
        if method == 'GET': return 200, {'status': 'stopped', 'browserCost': 0}
        raise AssertionError('unexpected browser request')
    monkeypatch.setattr(module, 'browser_api', browser_api)
    def navigate(p, url, parser, **kw):
        def capture(p, h, parser, **kw):
            return capture_page(p, h, parser, monotonic=lambda: page.clock, **kw)
        return navigate_capture(p, url, parser, monotonic=lambda: page.clock, capture=capture, **kw)
    monkeypatch.setattr(module, 'navigate_capture', navigate)
    return stopped


@pytest.mark.parametrize('module,source,role', [
    (results, 'statbunker', 'result'),
    (predictions, 'aipredict_io', 'prediction'),
    (shortlist, 'prosoccer', 'prediction'),
])
@pytest.mark.parametrize('failure', ['HTTP404', 'MAINTENANCE'])
def test_all_collectors_seal_actual_failure_not_ok_and_preserve_legacy_run(pipeline, monkeypatch, module, source, role, failure):
    monkeypatch.setattr(module, 'connect', pipeline['connect'])
    monkeypatch.setattr(module, 'PILOT_ID', 'truthful-run-test')
    monkeypatch.setattr(shortlist, 'PROSOCCER_EXTRA_PAGES', {})
    table = 'result_source_pilot_runs' if role == 'result' else 'source_pilot_runs'
    fixture = json.loads((Path(__file__).parents[1] / 'fixtures/pilot_false_ok_20261002.json').read_text())
    import base64
    archived = gzip.decompress(base64.b64decode(fixture['html_gzip_base64']))
    with pipeline['connect']() as conn:
        conn.execute(f'''INSERT INTO {table}(run_id,pilot_id,source,slot_no,started_at_utc,
                     finished_at_utc,status,http_status,html_gzip,html_sha256,manifest)
                     VALUES ('legacy-false-ok','truthful-run-test',%s,1,NOW(),NOW(),'OK',404,%s,%s,%s::jsonb)''',
                     (source, gzip.compress(archived), fixture['html_sha256'], json.dumps({'page_capture': fixture['page_capture']})))
        before = conn.execute(f"SELECT to_jsonb(r) AS row FROM {table} r WHERE slot_no=1").fetchone()['row']
    page = Page([archived.decode() if failure == 'HTTP404' else 'Under maintenance'],
                http_status=404 if failure == 'HTTP404' else 200,
                html=archived.decode() if failure == 'HTTP404' else '<html>Under maintenance</html>')
    stopped = browser_fixture(monkeypatch, module, page)
    args = [] if role == 'result' else [[]]
    out = module.collect_source(source, module.SOURCES[source], 2, *args)
    assert out['status'] == 'ERROR' and out['source_resilience']['mode'] == 'OPEN'
    assert out['run_outcome']['reason'] == ('HTTP_404' if failure == 'HTTP404' else 'MAINTENANCE')
    assert stopped == [{'action': 'stop'}]
    with pipeline['connect']() as conn:
        run = conn.execute(f'SELECT * FROM {table} WHERE slot_no=2').fetchone()
        assert run['status'] == out['status']
        assert run['manifest']['run_outcome'] == out['run_outcome']
        assert run['manifest']['source_resilience_at_admission']['mode'] == 'CLOSED'
        assert 'source_resilience' not in run['manifest']
        raw = gzip.decompress(bytes(run['html_gzip']))
        assert hashlib.sha256(raw).hexdigest() == run['html_sha256']
        if module != shortlist and failure == 'HTTP404': assert run['html_sha256'] == fixture['html_sha256']
        assert conn.execute(f"SELECT to_jsonb(r) AS row FROM {table} r WHERE slot_no=1").fetchone()['row'] == before
        for target in ('result_observations', 'source_pilot_predictions', 'model_snapshots', 'decision_tickets', 'market_decision_tickets'):
            assert conn.execute(f'SELECT count(*) AS n FROM {target}').fetchone()['n'] == 0
    monkeypatch.setattr(module, 'browser_api', lambda *a: pytest.fail('same slot allocated a browser'))
    repeat = module.collect_source(source, module.SOURCES[source], 2, *args)
    assert repeat['status'] == 'CIRCUIT_OPEN'
    with pipeline['connect']() as conn:
        assert conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 2


def test_partial_prosoccer_keeps_tomorrow_and_circuit_available_when_today_fails(pipeline, monkeypatch):
    monkeypatch.setattr(shortlist, 'connect', pipeline['connect'])
    monkeypatch.setattr(shortlist, 'PILOT_ID', 'partial-outcome-test')
    monkeypatch.setattr(shortlist, 'PROSOCCER_EXTRA_PAGES', {})
    page = Page([PRED])
    page.tomorrow_text = PRED.replace('Lithuania', 'France')
    original = page.goto
    def goto(url, **kwargs):
        result = original(url, **kwargs)
        result.status = 200 if url.endswith('/tomorrow') else 404
        return result
    page.goto = goto
    browser_fixture(monkeypatch, shortlist, page)
    out = shortlist.collect_source('prosoccer', shortlist.SOURCES['prosoccer'], 1, [])
    assert out['status'] == 'OK' and out['run_outcome']['partial'] and out['rows'] == 1
    assert out['source_resilience']['mode'] == 'CLOSED'
    with pipeline['connect']() as conn:
        run = conn.execute('SELECT * FROM source_pilot_runs').fetchone()
        assert run['manifest']['run_outcome'] == out['run_outcome']
        assert run['manifest']['page_capture']['state'] == 'HTTP_ERROR'
        assert run['manifest']['extra_pages']['tomorrow']['page_capture']['state'] == 'READY_PARSED'
        row = conn.execute('SELECT * FROM source_pilot_predictions').fetchone()
        assert row['home_team'] == 'France' and row['kickoff_utc'] is None


def test_result_loading_recovers_then_failure_is_append_only_and_not_ingested(pipeline, monkeypatch):
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(runtime_health, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'PILOT_ID', 'synthetic-content-results')
    monkeypatch.setattr(results, 'result_anchor_rows', lambda: [{
        'match_id': 'synthetic-lithuania-andorra', 'home_team': 'Lithuania', 'away_team': 'Andorra', 'league': 'Friendlies',
        'kickoff_utc': datetime(2026, 9, 30, 16, tzinfo=timezone.utc)}])
    # A run started much earlier must not backdate an observation captured later.
    monkeypatch.setattr(results, 'utcnow', lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    page = Page(['Loading...', TEXT, TEXT])
    stopped = browser_fixture(monkeypatch, results, page)
    successful = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 1)
    assert stopped == [{'action': 'stop'}]
    assert successful['normalized_observations'] == {'parsed': 1, 'matched': 1, 'persisted': 1}
    with pipeline['connect']() as conn:
        before = conn.execute('SELECT to_jsonb(r) AS row FROM result_source_pilot_runs r').fetchone()['row']
        observation = conn.execute('SELECT * FROM result_observations').fetchone()
        assert observation['provider'] == 'pilot:smart_soccer_stats'
        assert observation['final_away_goals'] == observation['ht_away_goals'] == 0
        assert observation['corners_home'] is None
        assert observation['payload']['production_eligible'] is False
        stamp = datetime.fromisoformat(before['manifest']['page_capture']['captured_at_utc'])
        assert observation['observed_at_utc'] == stamp > datetime(2026, 10, 1, tzinfo=timezone.utc)
        raw = gzip.decompress(bytes(conn.execute('SELECT html_gzip FROM result_source_pilot_runs').fetchone()['html_gzip']))
        assert hashlib.sha256(raw).hexdigest() == before['html_sha256']
    page = Page([TEXT], http_status=504)  # Even a parsable error response must not ingest.
    browser_fixture(monkeypatch, results, page)
    failed = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 2)
    assert failed['status'] == 'ERROR'
    assert failed['normalized_observations'] == {'parsed': 0, 'matched': 0, 'persisted': 0}
    report = runtime_health.runtime_health()
    assert report['counts']['failed_pilot_transports'] == report['counts']['failed_pilot_content'] == 1
    with pipeline['connect']() as conn:
        assert conn.execute('SELECT status FROM result_source_pilot_runs WHERE slot_no=2').fetchone()['status'] == 'ERROR'
        assert conn.execute('SELECT to_jsonb(r) AS row FROM result_source_pilot_runs r WHERE slot_no=1').fetchone()['row'] == before
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 1
        for table in ('model_snapshots', 'market_decision_tickets', 'decision_tickets'):
            assert conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 0


@pytest.mark.parametrize('module,source,text,accepted', [
    (predictions, 'aipredict_io', 'Loading...', False),
    (shortlist, 'prosoccer', PRED, True),
])
def test_prediction_pilots_share_gate_preserve_raw_and_server_seals(pipeline, monkeypatch, module, source, text, accepted):
    monkeypatch.setattr(module, 'connect', pipeline['connect'])
    monkeypatch.setattr(module, 'PILOT_ID', 'synthetic-content-predictions')
    monkeypatch.setattr(shortlist, 'PROSOCCER_EXTRA_PAGES', {})
    page = Page([text])
    if accepted:
        page.tomorrow_text = PRED.replace('Lithuania', 'France')
    stopped = browser_fixture(monkeypatch, module, page)
    outcome = module.collect_source(source, module.SOURCES[source], 1, [])
    assert outcome['rows'] == (2 if accepted else 0)  # ProSoccer today and tomorrow are separate raw pages.
    assert stopped == [{'action': 'stop'}]
    with pipeline['connect']() as conn:
        run = conn.execute('SELECT * FROM source_pilot_runs').fetchone()
        assert run['sealed_at_utc'] is not None
        assert run['manifest']['page_capture']['state'] == ('READY_PARSED' if accepted else 'LOADING_TIMEOUT')
        assert run['status'] == outcome['status'] == ('OK' if accepted else 'ERROR')
        raw = gzip.decompress(bytes(run['html_gzip']))
        assert hashlib.sha256(raw).hexdigest() == run['html_sha256']
        rows = list(conn.execute('SELECT * FROM source_pilot_predictions').fetchall())
        assert len(rows) == outcome['rows']
        for row in rows:
            assert row['sealed_at_utc'] >= run['sealed_at_utc']
            assert row['timezone_status'] == 'TIMEZONE_UNRESOLVED' and row['kickoff_utc'] is None
        assert conn.execute('SELECT count(*) AS n FROM market_decision_tickets').fetchone()['n'] == 0


def test_health_latest_http200_content_failures_and_extra_pages_recover_without_backfill(pipeline, monkeypatch):
    monkeypatch.setattr(runtime_health, 'connect', pipeline['connect'])
    with pipeline['connect']() as conn:
        for slot, state in [(1, 'MAINTENANCE'), (2, 'READY_PARSED')]:
            conn.execute('''INSERT INTO result_source_pilot_runs(run_id,pilot_id,source,slot_no,
                         started_at_utc,status,http_status,manifest)
                         VALUES (%s,'content-test','result',%s,NOW(),'OK',200,%s::jsonb)''',
                         (f'result-{slot}', slot, json.dumps({'page_capture': {'state': state}})))
        conn.execute('''INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,started_at_utc,status,http_status,manifest)
                     VALUES ('extra-error','content-test','model',1,NOW(),'OK',200,%s::jsonb)''',
                     (json.dumps({'page_capture': {'state': 'READY_PARSED'},
                                  'extra_pages': {'tomorrow': {'page_capture': {'state': 'PARSER_ERROR'}}}}),))
    report = runtime_health.runtime_health()
    assert report['counts']['failed_pilot_transports'] == 0
    assert report['counts']['failed_pilot_content'] == 1
    assert 'failed_pilot_content' in report['warnings']
    with pipeline['connect']() as conn:
        conn.execute('''INSERT INTO source_pilot_runs(run_id,pilot_id,source,slot_no,started_at_utc,status,http_status,manifest)
                     VALUES ('extra-recovered','content-test','model',2,NOW(),'OK',200,'{"page_capture":{"state":"READY_PARSED"}}')''')
    assert runtime_health.runtime_health()['counts']['failed_pilot_content'] == 0
    with pipeline['connect']() as conn:
        assert conn.execute("SELECT manifest->'page_capture'->>'state' AS state FROM result_source_pilot_runs WHERE run_id='result-1'").fetchone()['state'] == 'MAINTENANCE'


def test_recovered_get_attempt_raw_is_persisted_and_failed_stop_blocks_ingestion(pipeline, monkeypatch):
    import base64
    from staging.page_capture import BrowserStopError
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'result_anchor_rows', lambda: [])
    page = Page([TEXT])
    original = page.goto
    navigations = []
    def goto(url, **kwargs):
        response = original(url, **kwargs)
        navigations.append(url)
        response.status = 504 if len(navigations) == 1 else 200
        return response
    page.goto = goto
    stopped = browser_fixture(monkeypatch, results, page)
    outcome = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 1)
    assert len(navigations) == 2 and stopped == [{'action': 'stop'}]
    assert outcome['normalized_observations']['parsed'] == 1
    with pipeline['connect']() as conn:
        run = conn.execute('SELECT * FROM result_source_pilot_runs').fetchone()
        attempts = run['manifest']['page_capture']['navigation_attempts']
        assert [a['http_status'] for a in attempts] == [504, 200]
        first = gzip.decompress(base64.b64decode(attempts[0]['html_gzip_base64']))
        assert hashlib.sha256(first).hexdigest() == attempts[0]['html_sha256']
    original_api = results.browser_api
    def unconfirmed(method, url, body=None):
        if method == 'GET': return 200, {'status': 'active'}
        return original_api(method, url, body)
    monkeypatch.setattr(results, 'browser_api', unconfirmed)
    failed = results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 2)
    assert failed["status"] == "ERROR" and failed["rows"] == 0
    assert failed["source_resilience"]["stop_pending_id"] == "synthetic-browser"
    assert failed["source_resilience"]["mode"] == "OPEN"
    with pipeline['connect']() as conn:
        assert conn.execute('SELECT count(*) AS n FROM result_source_pilot_runs').fetchone()['n'] == 2
        assert conn.execute("SELECT status FROM result_source_pilot_runs WHERE slot_no=2").fetchone()['status'] == 'ERROR'
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 0
