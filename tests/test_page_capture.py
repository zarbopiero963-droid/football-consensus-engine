import hashlib
import json
from pathlib import Path

import pytest

from staging.page_capture import capture_page, page_state
from staging.result_sources.pilot48 import parse_smart_soccer_stats_results


RESULT = "30 Sept 2026\nLithuania\n3\n-\n0\nAndorra\n(2-0)\nFriendlies - World, 2026"
EVIDENCE = json.loads((Path(__file__).parent / 'fixtures/pilot_content_states_20261001.json').read_text())


class Page:
    def __init__(self, texts, title='Game Statistics'):
        self.texts = texts
        self.index = 0
        self.clock = 0
        self.heading = title
        self.waits = []

    def locator(self, selector):
        assert selector == 'body'
        return self

    def inner_text(self, timeout):
        assert 0 < timeout <= 2000
        return self.texts[self.index]

    def title(self):
        return self.heading

    def content(self):
        return '<html>' + self.texts[self.index] + '</html>'

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.clock += milliseconds / 1000
        self.index = min(self.index + 1, len(self.texts) - 1)


def capture(page, status=200, parser=parse_smart_soccer_stats_results, **kwargs):
    return capture_page(page, status, parser, monotonic=lambda: page.clock, **kwargs)


def test_real_loading_shell_waits_for_two_identical_source_outputs_without_reload():
    page = Page([EVIDENCE['loading']['text_from_html'], RESULT, RESULT])
    result = capture(page)
    assert result.accepted and result.text == RESULT
    assert page.waits == [1000, 1000]
    assert [s['state'] for s in result.audit['samples']] == ['LOADING', 'CONTENT', 'CONTENT']
    assert result.parsed[0]['final_away_goals'] == 0
    assert result.parsed[0]['ht_away_goals'] == 0
    assert result.parsed[0]['corners_home'] is None
    assert result.html == page.content().encode()
    assert result.audit['samples'][-1]['text_sha256'] == hashlib.sha256(RESULT.encode()).hexdigest()


@pytest.mark.parametrize('status', [200, 502])
def test_real_maintenance_content_is_terminal_even_with_success_http(status):
    evidence = EVIDENCE['maintenance']
    page = Page([evidence['text']], title=evidence['title'])
    result = capture(page, status, parser=lambda _: pytest.fail('must not parse maintenance'))
    assert result.audit['state'] == 'MAINTENANCE'
    assert result.audit['http_status'] == status
    assert not result.accepted and result.parsed == [] and result.text == ''
    assert result.raw_text == evidence['text'] and result.html
    assert page.waits == []


@pytest.mark.parametrize('status', [401, 403, 404, 429, 503, 504, None])
def test_http_failure_never_ingests_parsable_shell_rows(status):
    page = Page([RESULT])
    result = capture(page, status, parser=lambda _: pytest.fail('must not parse HTTP error'))
    assert result.audit['state'] == ('HTTP_UNOBSERVED' if status is None else 'HTTP_ERROR')
    assert not result.accepted and result.parsed == [] and result.html
    assert page.waits == []


def test_stuck_loading_budget_expires_with_raw_and_no_missing_to_zero():
    page = Page([EVIDENCE['loading']['text_from_html']])
    result = capture(page, timeout_ms=2500)
    assert result.audit['state'] == 'LOADING_TIMEOUT'
    assert page.clock == 2.5 and page.waits == [1000, 1000, 500]
    assert result.parsed == [] and result.text == '' and result.html


def test_empty_http200_is_unconfirmed_not_asserted_schema_drift_or_valid_no_matches():
    page = Page(['No data'])
    result = capture(page, timeout_ms=1000)
    assert result.audit['state'] == 'EMPTY_UNCONFIRMED'
    assert not result.accepted


def test_changing_rows_and_loading_with_rows_are_quarantined_at_deadline():
    page = Page([RESULT, RESULT.replace('\n3\n', '\n4\n')])
    result = capture(page, timeout_ms=1000)
    assert result.audit['state'] == 'UNSTABLE_TIMEOUT' and result.parsed == []
    page = Page([RESULT + '\nLoading...'])
    assert capture(page, timeout_ms=1000).audit['state'] == 'LOADING_TIMEOUT'


@pytest.mark.parametrize('parser', [lambda _: (_ for _ in ()).throw(ValueError('secret=do-not-log')),
                                  lambda _: None, lambda _: [{'p1': float('nan')} ]])
def test_parser_errors_preserve_raw_and_only_safe_exception_class(parser):
    page = Page([RESULT])
    result = capture(page, parser=parser)
    assert result.audit['state'] == 'PARSER_ERROR'
    assert result.parsed == [] and result.html and page.waits == []
    assert 'do-not-log' not in json.dumps(result.audit)


@pytest.mark.parametrize('operation', ['inner_text', 'content', 'wait_for_timeout'])
def test_capture_errors_do_not_publish_previously_parsed_rows(operation):
    page = Page([RESULT])
    def fail(*args, **kwargs):
        raise TimeoutError('secret=do-not-log')
    setattr(page, operation, fail)
    result = capture(page)
    assert result.audit['state'] == 'CAPTURE_ERROR'
    assert result.audit['capture_error_class'] == 'TimeoutError'
    assert not result.accepted and result.parsed == []
    assert 'do-not-log' not in json.dumps(result.audit)


def test_capability_only_content_is_not_normalized_or_readiness_certified():
    page = Page([RESULT])
    result = capture(page, parser=None, evidence_kind='capability_only')
    assert result.audit['state'] == 'CAPABILITY_ONLY'
    assert not result.accepted and result.parsed == [] and page.waits == []


def test_signals_do_not_match_incidental_team_names_or_articles():
    assert page_state(200, 'Football', 'Loading FC\nMaintenance United\nScheduled maintenance discussed') == 'CONTENT'
    assert page_state(200, 'Just a moment...', RESULT) == 'ACCESS_BLOCKED'


def test_mapping_adapters_for_extra_markets_keep_native_values():
    page = Page(['native'])
    result = capture(page, parser=lambda _: {'team-pair': {'over': 44, 'under': None}})
    assert result.accepted and result.parsed['team-pair'] == {'over': 44, 'under': None}


class NavigationPage(Page):
    def __init__(self, responses):
        super().__init__([''])
        self.responses = responses
        self.calls = []

    def goto(self, url, **kwargs):
        from types import SimpleNamespace
        self.calls.append((url, kwargs))
        code, text, retry_after = self.responses[min(len(self.calls)-1, len(self.responses)-1)]
        self.texts = [text]
        if code == 'timeout':
            raise TimeoutError('do-not-log-navigation-capability')
        return SimpleNamespace(status=code, header_value=lambda key: retry_after if key == 'retry-after' else None)


def navigate(page, **kwargs):
    from staging.page_capture import navigate_capture
    return navigate_capture(page, 'https://source.example/public-fixtures', parse_smart_soccer_stats_results,
                            monotonic=lambda: page.clock,
                            capture=lambda p, h, parser, **kw: capture(p, h, parser, **kw), **kwargs)


def test_graduated_retry_retains_each_failed_raw_hash_and_real_final_time():
    import base64
    import gzip
    page = NavigationPage([(504, 'gateway-timeout-one', None), (503, 'warming-two', None), (200, RESULT, None)])
    result = navigate(page)
    assert result.accepted and len(page.calls) == 3
    attempts = result.audit['navigation_attempts']
    assert [a['http_status'] for a in attempts] == [504, 503, 200]
    assert [a['backoff_seconds'] for a in attempts[:-1]] == [3, 10]
    for a in attempts[:-1]:
        raw = gzip.decompress(base64.b64decode(a['html_gzip_base64']))
        assert hashlib.sha256(raw).hexdigest() == a['html_sha256']
        assert len(raw) == a['html_bytes']
    assert attempts[-1]['raw_storage'] == 'FINAL_HTML'
    assert result.audit['captured_at_utc'] >= attempts[0]['captured_at_utc']
    assert page.clock == 14  # 3 + 10 seconds backoff + 1 second stable content.
    assert all(c[0] == page.calls[0][0] and c[1]['wait_until'] == 'domcontentloaded' for c in page.calls)


def test_get_timeout_can_retry_but_its_readable_shell_is_never_ingested():
    page = NavigationPage([('timeout', RESULT, None), (200, RESULT, None)])
    result = navigate(page)
    assert result.accepted and len(page.calls) == 2
    first = result.audit['navigation_attempts'][0]
    assert first['http_status'] is None and first['navigation_error_class'] == 'TimeoutError'
    assert 'do-not-log' not in json.dumps(result.audit)


@pytest.mark.parametrize('code', [401, 403, 404])
def test_no_retry_for_auth_access_or_wrong_route(code):
    page = NavigationPage([(code, RESULT, None)])
    assert not navigate(page).accepted
    assert len(page.calls) == 1 and page.waits == []


def test_maintenance_and_parser_error_are_terminal_not_retried():
    page = NavigationPage([(502, EVIDENCE['maintenance']['text'], None)])
    assert navigate(page).audit['state'] == 'MAINTENANCE'
    assert len(page.calls) == 1 and page.waits == []
    page = NavigationPage([('timeout', EVIDENCE['maintenance']['text'], None)])
    assert navigate(page).audit['state'] == 'MAINTENANCE' and len(page.calls) == 1


def test_retry_after_is_honored_or_deferred_without_exceeding_budget():
    page = NavigationPage([(429, 'rate-limited', '12'), (200, RESULT, None)])
    assert navigate(page).accepted and page.clock == 31
    page = NavigationPage([(429, 'rate-limited', '3600')])
    result = navigate(page)
    assert not result.accepted and len(page.calls) == 1 and page.waits == []
    assert result.audit['navigation_attempts'][0]['retry_deferred'] == 'BUDGET_OR_RETRY_AFTER'
    page = NavigationPage([(504, 'timeout', None)])
    result = navigate(page, budget_ms=2000)
    assert len(page.calls) == 1 and page.clock == 0 and not result.accepted


def test_retry_exhaustion_and_loading_retry_are_bounded():
    page = NavigationPage([(504, 'timeout', None)])
    result = navigate(page)
    assert not result.accepted and len(page.calls) == 3 and page.clock == 13
    page = NavigationPage([(200, 'Loading...', None), (200, RESULT, None)])
    assert navigate(page).accepted and len(page.calls) == 2 and page.clock == 24


def test_retry_after_date_invalid_and_unbounded_values():
    from datetime import datetime, timezone
    from staging.page_capture import retry_after_seconds
    now = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)
    assert retry_after_seconds('Thu, 01 Oct 2026 16:00:10 GMT', now) == 10
    assert retry_after_seconds('Thu, 01 Oct 2026 15:00:10 GMT', now) == 0
    assert retry_after_seconds('2592000', now) == 2592000  # A month exceeds this run, never retry early.
    for v in (None, '-1', 'nan', '9' * 400, 'malformed'):
        assert retry_after_seconds(v, now) is None


def test_exhausted_source_budget_does_not_navigate_or_parse():
    page = NavigationPage([(200, RESULT, None)])
    result = navigate(page, budget_ms=0)
    assert result.audit['state'] == 'BUDGET_EXHAUSTED' and not result.accepted
    assert page.calls == [] and result.html == b''


@pytest.mark.parametrize('stop_code,session_code,state,ok', [
    (200, 200, 'stopped', True), (200, 200, 'active', False),
    (500, 200, 'stopped', False), (200, 503, 'stopped', False),
    (404, 200, 'active', True), (200, 410, '', True)])
def test_browser_stop_must_be_confirmed_before_ingestion(stop_code, session_code, state, ok):
    from staging.page_capture import confirmed_browser_stop, BrowserStopError
    def api(method, url, body=None):
        return (stop_code, {}) if method == 'PATCH' else (session_code, {'status': state})
    if ok:
        assert confirmed_browser_stop(api, 'https://provider.example/browsers', 'test')['status'] == 'stopped'
    else:
        with pytest.raises(BrowserStopError):
            confirmed_browser_stop(api, 'https://provider.example/browsers', 'test')
