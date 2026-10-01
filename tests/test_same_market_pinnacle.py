import base64
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from app.market_vectors import make_vector
from staging.same_market.pinnacle import normalize, select_same_market

ROOT = Path(__file__).resolve().parents[1] / 'staging/same_market'
KNOWN = '2026-10-01T18:19:30Z'


def capture():
    return json.loads((ROOT / 'live_capture_20261001.json').read_text())


def rewrite(bundle, kind, rows):
    raw = json.dumps(rows, allow_nan=False).encode()
    doc = bundle['documents'][kind]
    doc.update(raw_gzip_base64=base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
               sha256=hashlib.sha256(raw).hexdigest(), bytes_utf8=len(raw))


@pytest.fixture
def single():
    bundle = capture()
    quote = normalize(bundle, known_at=KNOWN)['quotes'][0]
    docs = {kind: json.loads(gzip.decompress(base64.b64decode(doc['raw_gzip_base64'])))
            for kind, doc in bundle['documents'].items()}
    matchup = next(m for m in docs['matchups'] if str(m['id']) == quote['provider_match_id'])
    market = next(m for m in docs['markets'] if m['matchupId'] == matchup['id']
                  and m['key'] == 's;0;ou;2.5' and m['period'] == 0)
    rewrite(bundle, 'matchups', [matchup]); rewrite(bundle, 'markets', [market])
    return bundle, matchup, market


def test_complete_real_dom_capture_replay_and_exact_prices():
    bundle = capture()
    report = normalize(bundle, known_at=KNOWN)
    assert report == json.loads((ROOT / 'live_report_20261001.json').read_text())
    assert report['quote_count'] == 43
    assert report['rejected']['PUSH_SPLIT_LINE_UNSUPPORTED'] == 119
    assert report['btts_status'] == 'NOT_OBSERVED_NO_ADAPTER'
    assert report['q_open'] is None and report['q_close'] is None
    first = report['quotes'][0]
    assert first['american_prices'] == {'OVER': -234, 'UNDER': 182}
    assert first['odds']['OVER'] == pytest.approx(1 + 100 / 234)
    assert first['odds']['UNDER'] == pytest.approx(2.82)
    assert sum(first['fair'].values()) == pytest.approx(1)
    assert all(q['period'] == 'FT' and q['role'] == 'MARKET_BENCHMARK'
               and not q['prematch_certified'] and not q['production_eligible']
               and not q['adds_source_vote'] for q in report['quotes'])


@pytest.mark.parametrize('field,value', [('sha256', 'f'*64), ('bytes_utf8', 1),
    ('truncated', True), ('http_status', 403), ('raw_format', 'UNKNOWN'),
    ('final_url', 'https://example.com/challenge'), ('raw_gzip_base64', 'broken')])
def test_untrusted_document_never_yields_prices(single, field, value):
    bundle, _, _ = single
    bundle['documents']['markets'][field] = value
    with pytest.raises(ValueError):
        normalize(bundle, known_at=KNOWN)


@pytest.mark.parametrize('field,value,reason', [
    ('type', 'team_total', 'UNSUPPORTED_MARKET_OR_PERIOD'),
    ('type', 'moneyline', 'UNSUPPORTED_MARKET_OR_PERIOD'),
    ('period', 1, 'UNSUPPORTED_MARKET_OR_PERIOD'),
    ('period', False, 'UNSUPPORTED_MARKET_OR_PERIOD'),
    ('status', 'suspended', 'MARKET_NOT_OPEN_OR_UNAUDITED'),
    ('isAlternate', None, 'MARKET_NOT_OPEN_OR_UNAUDITED'),
    ('key', 's;0;ou;2.75', 'PUSH_SPLIT_LINE_UNSUPPORTED'),
    ('key', 's;0;ou;3.0', 'PUSH_SPLIT_LINE_UNSUPPORTED'),
    ('key', 's;0;ou;nan', 'MARKET_KEY_INVALID'),
    ('matchupId', True, 'MATCHUP_ID_INVALID'),
])
def test_market_period_push_and_split_lines_remain_unsupported(single, field, value, reason):
    bundle, _, market = single
    market[field] = value; rewrite(bundle, 'markets', [market])
    report = normalize(bundle, known_at=KNOWN)
    assert report['quote_count'] == 0 and report['rejected'][reason] == 1


@pytest.mark.parametrize('change,reason', [
    ('points', 'SELECTION_LINE_MISMATCH'), ('missing', 'INCOMPLETE_OR_DUPLICATE_PRICES'),
    ('duplicate', 'INCOMPLETE_OR_DUPLICATE_PRICES'), ('zero', 'INVALID_PRICE'),
    ('bool', 'INVALID_PRICE'), ('empty', 'INVALID_PRICE'),
])
def test_selection_completeness_prices_and_exact_line(single, change, reason):
    bundle, _, market = single
    if change == 'points': market['prices'][1]['points'] = 3.5
    elif change == 'missing': market['prices'].pop()
    elif change == 'duplicate': market['prices'][1]['designation'] = 'over'
    else: market['prices'][0]['price'] = {'zero': 0, 'bool': True, 'empty': None}[change]
    rewrite(bundle, 'markets', [market])
    report = normalize(bundle, known_at=KNOWN)
    assert report['quote_count'] == 0 and report['rejected'][reason] == 1


@pytest.mark.parametrize('field,value', [('isLive', True), ('isLive', None),
    ('parentId', 123), ('units', 'Corners'), ('status', 'live')])
def test_live_child_and_non_goal_fixture_rejected(single, field, value):
    bundle, matchup, _ = single
    matchup[field] = value; rewrite(bundle, 'matchups', [matchup])
    assert normalize(bundle, known_at=KNOWN)['rejected'] == {'UNSAFE_FIXTURE': 1}


def test_duplicate_markets_and_matchups_do_not_pick_arbitrary_price(single):
    bundle, matchup, market = single
    rewrite(bundle, 'markets', [market, market])
    report = normalize(bundle, known_at=KNOWN)
    assert report['quote_count'] == 0 and report['rejected']['AMBIGUOUS_MARKET_SERIES'] == 2
    rewrite(bundle, 'markets', [market]); rewrite(bundle, 'matchups', [matchup, matchup])
    assert normalize(bundle, known_at=KNOWN)['quote_count'] == 0


@pytest.mark.parametrize('change', ['capture_after_kickoff', 'naive_start', 'stop', 'future_capture', 'naive_known'])
def test_no_backdating_or_unknown_stop(single, change):
    bundle, matchup, _ = single
    if change == 'capture_after_kickoff': bundle['captured_at_utc'] = matchup['startTime']
    if change == 'naive_start': matchup['startTime'] = '2026-10-10T13:30:00'; rewrite(bundle, 'matchups', [matchup])
    if change == 'stop': bundle['session_stopped'] = False
    if change == 'future_capture': bundle['captured_at_utc'] = '2027-01-01T00:00Z'
    if change in ('stop', 'future_capture', 'naive_known'):
        with pytest.raises(ValueError): normalize(bundle, known_at='2026-10-01T18:19:30' if change == 'naive_known' else KNOWN)
    else:
        assert normalize(bundle, known_at='2026-10-11T00:00Z')['quote_count'] == 0


def test_same_market_join_requires_full_vector_resolved_time_pair_and_context(single):
    bundle, _, _ = single
    quote = normalize(bundle, known_at=KNOWN)['quotes'][0]
    fixture = {k: quote[k] for k in ('home', 'away', 'league', 'country', 'category')}
    fixture['kickoff'] = quote['start']
    vector = make_vector('FT_OU_2.5', period='FT', components={'OVER': 55, 'UNDER': 45},
        raw_scale=100, source_timestamp=KNOWN, matching_status='EXACT_PAIR_KICKOFF_COMPETITION',
        kickoff_timezone_status='EXPLICIT_OFFSET')
    selected = select_same_market(vector, fixture, [quote], known_at=KNOWN)
    assert selected['status'] == 'STAGING_EXACT_MATCH_UNSEALED' and not selected['production_eligible']
    for change in ('line', 'period', 'pair', 'league', 'country', 'kickoff'):
        v, f = deepcopy(vector), deepcopy(fixture)
        if change == 'line': v.update(market_id='FT_OU_3.5', line='3.5')
        elif change == 'period': v.update(market_id='HT_OU_2.5', period='HT')
        elif change == 'pair': f['home'], f['away'] = f['away'], f['home']
        elif change == 'league': f['league'] = 'Bundesliga'
        elif change == 'country': f['country'] = 'Germany'
        elif change == 'kickoff': f['kickoff'] = '2026-10-10T16:00Z'
        assert select_same_market(v, f, [quote], known_at=KNOWN)['quote'] is None
    for field, value in (('vector_status', 'PARTIAL'), ('kickoff_timezone_status', 'TIMEZONE_UNRESOLVED'),
                         ('matching_status', 'FOUND_NAME_ONLY'), ('source_timestamp_utc', quote['start'])):
        v = deepcopy(vector); v[field] = value
        assert select_same_market(v, fixture, [quote], known_at=KNOWN)['quote'] is None
    assert select_same_market(vector, fixture, [quote, quote], known_at=KNOWN)['status'] == 'AMBIGUOUS'
    assert select_same_market(vector, fixture, [quote], known_at='2026-10-01T18:19:29Z')['quote'] is None
    v = deepcopy(vector); v['source_timestamp_utc'] = '2026-10-01T18:19:31Z'
    assert select_same_market(v, fixture, [quote], known_at=KNOWN)['quote'] is None


def test_btts_never_uses_totals_or_1x2_as_proxy(single):
    bundle, _, _ = single
    quotes = normalize(bundle, known_at=KNOWN)['quotes']
    fixture = {**quotes[0], 'kickoff': quotes[0]['start']}
    vector = make_vector('FT_BTTS', period='FT', components={'GG': 60, 'NG': 40}, raw_scale=100,
        source_timestamp=KNOWN, matching_status='EXACT_PAIR_KICKOFF_COMPETITION', kickoff_timezone_status='EXPLICIT_OFFSET')
    assert select_same_market(vector, fixture, quotes, known_at=KNOWN)['quote'] is None


@pytest.mark.parametrize('raw', [b'{"object":1}', b'[null]', b'[{"price":NaN}]', b'[] trailing',
    b'\xff', b' ' * (5 * 1024 * 1024 + 1)])
def test_bounded_whole_json_document_required(single, raw):
    bundle, _, _ = single
    doc = bundle['documents']['markets']
    doc.update(raw_gzip_base64=base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
               sha256=hashlib.sha256(raw).hexdigest(), bytes_utf8=len(raw))
    with pytest.raises(ValueError, match='DOCUMENT_RAW_INVALID'):
        normalize(bundle, known_at=KNOWN)


def test_empty_complete_feed_is_missing_and_never_a_zero_vector(single):
    bundle, _, _ = single
    rewrite(bundle, 'markets', [])
    report = normalize(bundle, known_at=KNOWN)
    assert report['quotes'] == [] and report['quote_count'] == 0
    assert report['q_open'] is None and report['q_close'] is None
