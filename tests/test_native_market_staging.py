import base64
from copy import deepcopy
import gzip
import hashlib

import pytest

from staging.market_vectors.native import normalize, published_cards

STAMP = '2026-10-01T12:00:00Z'
KNOWN = '2026-10-01T13:00:00Z'


def card(home='Synthetic Home', away='Synthetic Away', stats=''):
    return (f'<div class="match-card"><span class="match-card-long">{home}</span>'
            f'<span class="match-card-long">{away}</span>{stats}</div>')


def stat(label, value):
    return (f'<span class="match-card-stat"><span class="match-card-stat-key">{label}</span>'
            f'<span class="match-card-stat-value">{value}</span></span>')


def dataset(html=None, **row_changes):
    raw = (html if html is not None else card(stats=stat('Over 2.5', '0%') + stat('Both score', '53%'))).encode()
    run = {'run_id': 'synthetic-run', 'source': 'statsultra', 'status': 'OK', 'http_status': 200,
           'final_url': 'https://statsultra.com/', 'finished_at_utc': STAMP,
           'html_sha256': hashlib.sha256(raw).hexdigest(),
           'html_gzip_base64': base64.b64encode(gzip.compress(raw)).decode()}
    row = {'run_id': run['run_id'], 'source': 'statsultra', 'source_match_key': 'synthetic-key',
           'home_team': 'Synthetic Home', 'away_team': 'Synthetic Away', 'over25': 0,
           'btts_yes': 53, 'under25': None, 'btts_no': None,
           'match_status': 'FOUND_NAME_ONLY', 'timezone_status': 'TIMEZONE_UNRESOLVED',
           'parse_status': 'OK', **row_changes}
    return {'runs': [run], 'rows': [row]}


def report(data):
    return normalize(data, known_at=KNOWN)


def test_native_zero_missing_provenance_and_immutability():
    data = dataset(); original = deepcopy(data)
    result = report(data)
    assert result['status_counts'] == {'PARTIAL': 2}
    assert result['stored_mismatch_count'] == 0 and result['excluded'] == []
    ou, btts = [r['vector'] for r in result['records']]
    assert ou['normalized_vector'] == {'OVER': 0, 'UNDER': None}
    assert btts['normalized_vector'] == {'GG': .53, 'NG': None}
    assert ou['origin']['html_sha256'] == data['runs'][0]['html_sha256']
    assert ou['origin']['source_match_key'] == 'synthetic-key'
    assert ou['matching_status'] == 'FOUND_NAME_ONLY'
    assert ou['kickoff_timezone_status'] == 'TIMEZONE_UNRESOLVED'
    assert all(not r['prematch_certified'] and not r['vector']['production_eligible']
               and not r['vector']['adds_source_vote'] for r in result['records'])
    assert data == original


def test_old_inferred_complement_is_diagnostic_only():
    result = report(dataset(under25=100, over25=72))
    assert result['unpublished_opposite_count'] == 1 and result['stored_mismatch_count'] == 1
    ou = result['records'][0]
    assert ou['stored_value'] == 72 and ou['stored_opposite'] == 100
    assert ou['vector']['normalized_vector'] == {'OVER': 0, 'UNDER': None}


def test_card_boundaries_and_unrelated_script_text_never_fill_missing():
    html = (card(stats='<script>' + stat('Over 2.5', '88%') + '</script>')
            + card('Next Home', 'Next Away', stat('Over 2.5', '70%') + stat('Both score', '75%'))
            + stat('Over 2.5', '90%'))
    result = report(dataset(html))
    assert result['status_counts'] == {'ABSENT': 2}
    assert result['records'][0]['vector']['normalized_vector'] == {'OVER': None, 'UNDER': None}


@pytest.mark.parametrize('kind', ['raw_hash', 'gzip', 'http', 'source', 'url', 'future', 'naive'])
def test_invalid_run_has_no_vectors(kind):
    data = dataset(); run = data['runs'][0]
    if kind == 'raw_hash': run['html_sha256'] = '0' * 64
    if kind == 'gzip': run['html_gzip_base64'] = base64.b64encode(b'not gzip').decode()
    if kind == 'http': run['http_status'] = 403
    if kind == 'source': run['source'] = 'pinnacle'
    if kind == 'url': run['final_url'] = 'https://other.example/'
    if kind == 'future': run['finished_at_utc'] = '2026-10-01T14:00:00Z'
    if kind == 'naive': run['finished_at_utc'] = '2026-10-01T12:00:00'
    result = report(data)
    assert result['records'] == [] and len(result['excluded']) == 1


@pytest.mark.parametrize('kind', ['duplicate_card', 'duplicate_row', 'source', 'unmatched', 'unknown_run'])
def test_ambiguous_or_missing_row_evidence_excluded(kind):
    data = dataset()
    if kind == 'duplicate_card': data = dataset(card() + card())
    if kind == 'duplicate_row': data['rows'].append(deepcopy(data['rows'][0]))
    if kind == 'source': data['rows'][0]['source'] = 'pinnacle'
    if kind == 'unmatched': data['rows'][0]['home_team'] = 'Home'
    if kind == 'unknown_run': data['rows'][0]['run_id'] = 'missing'
    result = report(data)
    assert not result['records'] and result['excluded']


@pytest.mark.parametrize('value', ['-1%', '101%', 'NaN%', '53', ''])
def test_invalid_publication_is_broken_not_zero(value):
    result = report(dataset(card(stats=stat('Over 2.5', value))))
    assert result['records'][0]['vector']['vector_status'] == 'BROKEN'
    assert result['records'][1]['vector']['vector_status'] == 'ABSENT'


def test_duplicate_stat_nested_card_and_truncated_html_fail_closed():
    result = report(dataset(card(stats=stat('Over 2.5', '50%') + stat('Over 2.5', '60%'))))
    assert all(r['vector']['vector_status'] == 'BROKEN' for r in result['records'])
    for html in (card(stats=card()), card()[:-6]):
        result = report(dataset(html))
        assert not result['records'] and result['excluded']
    data = dataset(); data['runs'].append(deepcopy(data['runs'][0]))
    with pytest.raises(ValueError, match='DUPLICATE_RUN'): report(data)
    with pytest.raises(ValueError, match='EXPLICIT_KNOWLEDGE_TIME_REQUIRED'):
        normalize(dataset(), known_at='2026-10-01T13:00:00')


def test_html_entities_case_and_exact_source_pair():
    data = dataset(card('Synthetic &amp; Home', 'Synthetic Away', stat('over 2.5', '0%')),
                   home_team='SYNTHETIC & HOME')
    assert report(data)['records'][0]['vector']['vector_status'] == 'PARTIAL'
    assert published_cards(card())[0]['home_team'] == 'Synthetic Home'


def test_server_seal_cutoff_and_no_prematch_or_promotion_from_sealing_alone():
    data=dataset();data['runs'][0]['sealed_at_utc']='2026-10-01T12:01:00Z'
    data['rows'][0]['sealed_at_utc']='2026-10-01T12:02:00Z'
    result=report(data)
    assert all(r['sealing_status']=='SERVER_SEALED' and not r['prematch_certified']
               and not r['vector']['production_eligible'] for r in result['records'])
    data['rows'][0]['sealed_at_utc']='2026-10-01T14:00:00Z'
    assert not report(data)['records']
    data['rows'][0]['sealed_at_utc']='2026-10-01T12:02:00Z'
    data['runs'][0]['sealed_at_utc']='2026-10-01T14:00:00Z'
    assert not report(data)['records']
