"""Hashed real public DOM fragments plus explicitly synthetic negative variants."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from staging.structured_predictions import (
    CONTRACT, HTML_PARSERS, Tree, explicit_time, parse_elofoot_html,
    parse_opta_html, parse_clubelo_html, select_prematch, diagnostic_match,
    prediction_key,
)
from staging.page_capture import capture_page, navigate_capture
from staging.browser_use_shortlist import pilot48 as shortlist
from staging.issue29_pilot import pilot48 as issue29
from test_page_capture import Page, NavigationPage


PACK = json.loads((Path(__file__).parents[1] / 'staging/structured_predictions_evidence/live_fragments_20261001.json').read_text())


def document(source, kind='pilot_saved'):
    doc = next(d for d in PACK['documents'] if d['source'] == source and d['kind'] == kind)
    raw = gzip.decompress(base64.b64decode(doc['fragment_gzip_base64']))
    assert len(raw) == doc['fragment_bytes']
    assert hashlib.sha256(raw).hexdigest() == doc['fragment_sha256']
    return doc, raw


def card(source, index=0):
    """A real card subtree, serialized only for synthetic edge-case mutations."""
    _, raw = document(source, 'independent_pass2')
    from html import escape
    def serialize(n):
        if isinstance(n, str):
            return escape(n)
        attrs = ''.join(f' {k}="{escape(v or "", quote=True)}"' for k, v in n.attrs.items())
        return f'<{n.tag}{attrs}>' + ''.join(serialize(c) for c in n.children) + f'</{n.tag}>'
    nodes = [n for n in Tree(raw).root.walk() if
             (source == 'elofoot' and n.tag == 'a' and n.attrs.get('href', '').startswith('/en/prediction/')) or
             (source == 'opta_analyst' and n.tag == 'li' and n.attrs.get('data-match-id'))]
    if source == 'opta_analyst':
        nodes = [next(n for n in nodes if f'Win probability: {name} ' in n.text())
                 for name in ('Kazakhstan', 'Cyprus')]
    return serialize(nodes[index])


@pytest.mark.parametrize('doc', PACK['documents'], ids=lambda d: d['source'] + '-' + d['kind'])
def test_real_saved_dom_replay_has_native_time_vectors_and_lifecycle_rejections(doc):
    _, raw = document(doc['source'], doc['kind'])
    parsed = HTML_PARSERS[doc['source']](raw)
    rows, audit = select_prematch(parsed, datetime.fromisoformat(doc['diagnostic_cutoff_utc']))
    assert len(rows) == doc['expected_accepted']
    assert audit['rejection_counts'] == doc['expected_rejections']
    assert all(r['timezone_status'] == 'EXPLICIT_OFFSET' and r['source_timezone'] == 'UTC' for r in rows)
    assert all(r['kickoff_utc'] == explicit_time(r['kickoff_raw']) for r in rows)
    assert all(r['league'] and r['payload_extra']['source_match_id'] for r in rows)
    assert all(r['payload_extra']['production_eligible'] is False and r['payload_extra']['adds_source_vote'] is False for r in rows)


def test_real_live_opta_triplet_is_excluded_even_though_it_is_mathematically_complete():
    doc, raw = document('opta_analyst')
    parsed = parse_opta_html(raw)
    live = next(r for r in parsed if r['home_team'] == 'Austria Wien')
    assert (live['p1'], live['px'], live['p2']) == (75.5, 21.2, 3.4)
    assert "83" in live['payload_extra']['source_status_raw']
    assert live['kickoff_utc'] is None
    rows, audit = select_prematch(parsed, datetime.fromisoformat(doc['diagnostic_cutoff_utc']))
    assert live not in rows
    assert {'source_match_id': live['payload_extra']['source_match_id'], 'reason': 'LIVE_OR_FINAL'} in audit['rejections']


def test_real_elofoot_native_score_and_time_not_the_display_timezone():
    doc, raw = document('elofoot')
    row = parse_elofoot_html(raw)[0]
    assert (row['home_team'], row['away_team'], row['correct_score']) == ('Israel', 'Kosovo', '0-1')
    assert (row['p1'], row['px'], row['p2']) == (29, 28, 42)
    assert row['kickoff_utc'] == datetime(2026, 10, 1, 18, 45, tzinfo=timezone.utc)
    assert row['payload_extra']['kickoff_display_raw'] == '19:45 UK/Lagos'
    assert row['payload_extra']['correct_score_probability'] == 13


def test_real_clubelo_all_fifty_rows_and_native_columns_without_rounding_or_title_probability():
    _, raw = document('clubelo')
    rows = parse_clubelo_html(raw)
    assert len(rows) == 50
    first = rows[0]
    assert (first['home_team'], first['away_team']) == ('Helmond Sport', 'Heracles')
    assert (first['p1'], first['px'], first['p2']) == (16.2, 25.0, 58.8)
    assert first['league'] == 'Eerste Divisie 26/27'
    assert first['payload_extra']['source_team_country_codes'] == [['NED'], ['NED']]
    last = next(r for r in rows if r['home_team'] == 'Zemun')
    assert last['away_team'] == 'Železničar Pančevo'
    assert (last['p1'], last['px'], last['p2']) == (12.8, 21.0, 66.2)


@pytest.mark.parametrize('source', ['elofoot', 'opta_analyst'])
@pytest.mark.parametrize('raw', ['', '20:45', '2026-10-02T14:00:00', '2026-13-02T14:00Z'])
def test_synthetic_naive_missing_invalid_clock_never_borrows_time_from_another_card(source, raw):
    import re
    html = card(source)
    html = re.sub(r'datetime="[^"]*"', f'datetime="{raw}"', html)
    # The valid neighbouring card is not a timestamp donor.
    rows = HTML_PARSERS[source](html + card(source, 1))
    good, audit = select_prematch(rows, datetime(2026, 10, 1, 21, tzinfo=timezone.utc))
    assert len(good) == 1
    assert audit['rejection_counts']['TIME_UNRESOLVED'] == 1


def test_synthetic_conflicting_times_reject_and_equal_explicit_offsets_agree():
    html = card('elofoot')
    extra = '<time datetime="2026-10-02T16:00:00+02:00">16:00</time>'
    rows = parse_elofoot_html(html.replace('</a>', extra + '</a>'))
    good, audit = select_prematch(rows, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len(good) == 1 and len(good[0]['payload_extra']['time_evidence']['raw_values']) == 2
    conflict = html.replace('</a>', extra.replace('16:00:00', '17:00:00') + '</a>')
    assert select_prematch(parse_elofoot_html(conflict), datetime(2026, 10, 1, tzinfo=timezone.utc))[1]['rejection_counts'] == {'TIME_UNRESOLVED': 1}


@pytest.mark.parametrize('source', ['elofoot', 'opta_analyst', 'clubelo'])
def test_synthetic_cutoff_equality_and_elapsed_cleanup_block_the_row(source):
    _, raw = document(source, 'independent_pass2')
    row = HTML_PARSERS[source](raw)[0]
    if row['parse_status'] != 'OK':
        row = next(r for r in HTML_PARSERS[source](raw) if r['parse_status'] == 'OK' and r['kickoff_utc'])
    kickoff = row['kickoff_utc']
    assert len(select_prematch([row], kickoff - timedelta(microseconds=1))[0]) == 1
    assert select_prematch([row], kickoff)[1]['rejection_counts'] == {'NOT_PREMATCH_AT_INGESTION': 1}
    assert select_prematch([row], kickoff + timedelta(hours=1))[0] == []


@pytest.mark.parametrize('status', ['HT', 'FT', "81′"])
def test_synthetic_opta_live_status_with_future_datetime_is_still_not_prematch(status):
    import re
    html = re.sub(r'Kick-off\s*', status + ' ', card('opta_analyst'))
    rows, audit = select_prematch(parse_opta_html(html), datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert rows == [] and audit['rejection_counts'] == {'LIVE_OR_FINAL': 1}


def test_synthetic_duplicate_cards_are_deduped_but_conflicting_identity_is_rejected():
    html = card('elofoot')
    good, audit = select_prematch(parse_elofoot_html(html + html), datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len(good) == 1 and audit['identical_duplicates'] == 1
    drift = html.replace('58 %', '57 %').replace('17 %', '18 %')
    assert drift != html
    good, audit = select_prematch(parse_elofoot_html(html + drift), datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert good == [] and audit['rejection_counts'] == {'AMBIGUOUS_SOURCE_ID': 1}


@pytest.mark.parametrize('values', [(101, 0, -1), (40, 40, 40), (None, 50, 50), (float('nan'), 50, 50)])
def test_synthetic_invalid_incomplete_vectors_never_normalize_or_infer(values):
    row = parse_elofoot_html(card('elofoot'))[0]
    row.update(zip(('p1', 'px', 'p2'), values))
    good, audit = select_prematch([row], datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert good == [] and audit['rejection_counts'] == {'INVALID_OR_INCOMPLETE_VECTOR_OR_PAIR': 1}


def test_synthetic_true_zero_remains_zero():
    row = parse_elofoot_html(card('elofoot'))[0]
    row.update(p1=100, px=0, p2=0)
    good, _ = select_prematch([row], datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert (good[0]['p1'], good[0]['px'], good[0]['p2']) == (100, 0, 0)


@pytest.mark.parametrize('variant', ['header', 'shared_draw', 'date_conflict'])
def test_synthetic_clubelo_structure_or_time_conflict_never_becomes_another_market(variant):
    _, raw = document('clubelo')
    html = raw.decode()
    if variant == 'header':
        html = html.replace('<th>1</th>', '<th>Title</th>', 1)
        assert parse_clubelo_html(html) == []
    elif variant == 'shared_draw':
        html = html.replace('<td rowspan="2">', '<td>', 1)
        rows = parse_clubelo_html(html)
        assert rows[0]['parse_status'] == 'BROKEN'
    else:
        pos = html.index('2026-10-02T19:00Z')
        pos = html.index('2026-10-02T19:00Z', pos + 1)
        html = html[:pos] + html[pos:].replace('2026-10-02T19:00Z', '2026-10-03T19:00Z', 1)
        assert parse_clubelo_html(html)[0]['kickoff_utc'] is None


def test_synthetic_structured_matching_never_promotes_even_with_time_pair_and_league():
    pred = parse_elofoot_html(card('elofoot'))[0]
    anchor = {**pred, 'match_id': 'synthetic-anchor'}
    for match in (diagnostic_match, shortlist.match_anchor, issue29.match_anchor):
        assert match(pred, [anchor])[1] == 'FOUND_NAME_ONLY'
        assert match(pred, [anchor, anchor])[0:2] == (None, 'AMBIGUOUS')
        assert match(pred, [{**anchor, 'home_team': 'FC ' + anchor['home_team']}])[1] == 'NOT_FOUND'
        assert match(pred, [{**anchor, 'kickoff_utc': anchor['kickoff_utc'] + timedelta(minutes=16)}])[1] == 'KICKOFF_MISMATCH'
        assert match(pred, [{**anchor, 'league': 'Other competition'}])[1] == 'LEAGUE_MISMATCH'


def test_html_readiness_uses_same_raw_sample_and_detects_time_changes_even_if_text_is_stable():
    a, b = card('elofoot'), card('elofoot').replace('2026-10-02T14:00:00.000Z', '2026-10-03T14:00:00.000Z')
    page = Page(['fixed', 'fixed', 'fixed'])
    calls = []
    def content():
        calls.append(page.index)
        return a if page.index == 0 else b
    page.content = content
    result = capture_page(page, 200, lambda _: pytest.fail('text parser must not run'),
                          html_parser=parse_elofoot_html, monotonic=lambda: page.clock)
    assert result.accepted and calls == [0, 1, 2]
    assert result.html == b.encode()
    assert result.parsed[0]['kickoff_raw'] == '2026-10-03T14:00:00.000Z'
    assert result.audit['parser_input'] == 'HTML_SAME_SAMPLE'
    assert result.audit['samples'][-1]['html_sha256'] == hashlib.sha256(result.html).hexdigest()


def test_html_adapter_http_error_and_parser_error_never_ingest_or_hide_the_fault():
    page = Page(['fixed'])
    page.content = lambda: card('elofoot')
    result = capture_page(page, 404, None, html_parser=lambda _: pytest.fail('HTTP error must not parse'))
    assert not result.accepted and result.parsed == []
    result = capture_page(page, 200, None, html_parser=lambda _: (_ for _ in ()).throw(ValueError('secret=not-logged')))
    assert result.audit['state'] == 'PARSER_ERROR'
    assert 'not-logged' not in json.dumps(result.audit)


def test_html_parser_is_forwarded_through_real_navigation_retry_gate():
    page = NavigationPage([(200, 'fixed', None)])
    page.content = lambda: card('elofoot')
    result = navigate_capture(page, 'https://source.example/fixtures', None,
                              html_parser=parse_elofoot_html, monotonic=lambda: page.clock)
    assert result.accepted and len(page.calls) == 1
    assert result.audit['parser_input'] == 'HTML_SAME_SAMPLE'


def test_naive_cutoff_and_oversized_html_are_explicit_errors():
    with pytest.raises(ValueError):
        select_prematch([], datetime(2026, 10, 1))
    with pytest.raises(ValueError):
        parse_elofoot_html('x' * (5 * 1024 * 1024 + 1))


def test_synthetic_multiple_source_ids_for_same_fixture_and_multiple_vectors_are_ambiguous():
    row = parse_elofoot_html(card('elofoot'))[0]
    other = deepcopy(row)
    other['payload_extra']['source_match_id'] = 'synthetic-other-id'
    good, audit = select_prematch([row, other], datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert good == [] and audit['rejection_counts'] == {'AMBIGUOUS_FIXTURE_IDS': 2}
    html = card('elofoot').replace('</a>', '<span>1 10 % X 20 % 2 70 %</span></a>')
    assert parse_elofoot_html(html)[0]['parse_status'] == 'BROKEN'
    assert explicit_time('2026-10-02T14:00:00-00:00') is None


def test_source_key_preserves_fixture_identity_and_normalized_kickoff_without_changing_legacy_keys():
    row = parse_elofoot_html(card('elofoot'))[0]
    formatting = deepcopy(row)
    formatting['kickoff_raw'] = '2026-10-02T16:00:00+02:00'
    assert prediction_key('elofoot', row) == prediction_key('elofoot', formatting)
    changed = deepcopy(row)
    changed['kickoff_utc'] += timedelta(days=1)
    assert prediction_key('elofoot', row) != prediction_key('elofoot', changed)
    old = {'home_team': 'A', 'away_team': 'B', 'kickoff_raw': '12:00'}
    assert prediction_key('legacy', old) == hashlib.sha256(b'legacy|A|B|12:00').hexdigest()[:24]


@pytest.mark.parametrize('raw', ['0001-01-01T00:00:00+01:00', '9999-12-31T23:00:00-01:00'])
def test_synthetic_datetime_overflow_is_unresolved_instead_of_crashing_other_cards(raw):
    import re
    assert explicit_time(raw) is None
    html = re.sub(r'datetime="[^"]*"', f'datetime="{raw}"', card('elofoot'))
    rows, audit = select_prematch(parse_elofoot_html(html + card('elofoot', 1)),
                                  datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len(rows) == 1 and audit['rejection_counts'] == {'TIME_UNRESOLVED': 1}
