"""Real PostgreSQL ingestion; controlled CDP pages, no production/network writes."""
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
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
    def __init__(self, texts, http_status=200):
        self.texts, self.http_status = texts, http_status
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
    def content(self): return '<html>' + self.texts[self.index] + '</html>'
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


def test_result_loading_recovers_then_failure_is_append_only_and_not_ingested(pipeline, monkeypatch):
    monkeypatch.setattr(results, 'connect', pipeline['connect'])
    monkeypatch.setattr(runtime_health, 'connect', pipeline['connect'])
    monkeypatch.setattr(results, 'PILOT_ID', 'synthetic-content-results')
    monkeypatch.setattr(results, 'result_anchor_rows', lambda: [{
        'match_id': 'synthetic-lithuania-andorra', 'home_team': 'Lithuania', 'away_team': 'Andorra',
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
    assert failed['normalized_observations'] == {'parsed': 0, 'matched': 0, 'persisted': 0}
    report = runtime_health.runtime_health()
    assert report['counts']['failed_pilot_transports'] == report['counts']['failed_pilot_content'] == 1
    with pipeline['connect']() as conn:
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
    with pytest.raises(BrowserStopError):
        results.collect_source('smart_soccer_stats', results.SOURCES['smart_soccer_stats'], 2)
    with pipeline['connect']() as conn:
        assert conn.execute('SELECT count(*) AS n FROM result_source_pilot_runs').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) AS n FROM result_observations').fetchone()['n'] == 0
