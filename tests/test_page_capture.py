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
