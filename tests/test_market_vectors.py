from copy import deepcopy
import json

import pytest

from app.market_vectors import make_vector, derive_double_chance

STAMP = '2026-10-01T13:00:00Z'


def make(market='FT_1X2', **kwargs):
    return make_vector(market, **{'period': 'FT', 'raw_scale': 100, 'source': 'synthetic',
                                  'source_timestamp': STAMP, **kwargs})


def test_full_1x2_scale_provenance_and_no_promotion():
    raw = {'1': 60, 'X': 25, '2': 15}
    result = make(components=raw)
    assert result['vector_status'] == 'FULL'
    assert result['normalized_vector'] == {'1': .6, 'X': .25, '2': .15}
    assert result['raw_components'] == raw and result['raw_scale'] == 100
    assert result['source_timestamp_utc'] == '2026-10-01T13:00:00+00:00'
    assert result['matching_status'] == 'MATCH_UNRESOLVED'
    assert result['kickoff_timezone_status'] == 'TIMEZONE_UNRESOLVED'
    assert not result['production_eligible'] and not result['adds_source_vote']
    result['raw_components']['1'] = 0
    assert raw['1'] == 60


def test_native_partial_missing_and_real_zero_no_complements():
    over = make('FT_OU_2.5', components={'OVER': 0})
    assert over['vector_status'] == 'PARTIAL'
    assert over['normalized_vector'] == {'OVER': 0., 'UNDER': None}
    absent = make('FT_OU_2.5', components={'OVER': None, 'UNDER': None})
    assert absent['vector_status'] == 'ABSENT'
    assert absent['normalized_vector'] == {'OVER': None, 'UNDER': None}
    btts = make('FT_BTTS', components={'GG': 100, 'NG': 0})
    assert btts['vector_status'] == 'FULL'
    assert btts['normalized_vector'] == {'GG': 1., 'NG': 0.}
    # 0.5 on a percentage source means 0.5%, never automatically 50%.
    assert make('FT_BTTS', components={'GG': .5})['normalized_vector']['GG'] == .005


@pytest.mark.parametrize('market,kwargs,error', [
    ('FT_CORNERS_OU', {}, 'LINE_MISSING'),
    ('FT_OU_2.5', {'line': 3.5}, 'LINE_MISMATCH'),
    ('FT_OU', {'line': float('nan')}, 'INVALID_LINE'),
    ('FT_OU', {'line': True}, 'INVALID_LINE'),
    ('FT_OU', {'line': -2.5}, 'INVALID_LINE'),
    ('FT_BTTS', {'line': 2.5}, 'UNEXPECTED_LINE'),
    ('HT_1X2', {}, 'PERIOD_MISMATCH'),
    ('FT_UNKNOWN', {}, 'UNSUPPORTED_MARKET'),
    ('FT_1X2', {'components': {'1': .6, 'X': .3, '2': .3}, 'raw_scale': 1}, 'INVALID_SUM'),
    ('FT_1X2', {'components': {'1': 60, 'X': 25, '2': 15}, 'raw_scale': 1}, 'INVALID_PROBABILITY'),
    ('FT_1X2', {'components': {'1': float('nan')}}, 'INVALID_PROBABILITY'),
    ('FT_1X2', {'components': {'1': float('inf')}}, 'INVALID_PROBABILITY'),
    ('FT_1X2', {'components': {'1': -1}}, 'INVALID_PROBABILITY'),
    ('FT_1X2', {'components': {'1': True}}, 'INVALID_PROBABILITY'),
    ('FT_1X2', {'components': {'1': 60}, 'raw_scale': None}, 'SCALE_UNRESOLVED'),
    ('FT_1X2', {'components': {'OVER': 60}}, 'UNKNOWN_COMPONENT'),
    ('FT_1X2', {'components': []}, 'INVALID_COMPONENTS'),
    ('FT_1X2', {'components': False}, 'INVALID_COMPONENTS'),
    ('FT_CORRECT_SCORE', {'outcome_space': [[]]}, 'INVALID_OUTCOME_SPACE'),
    ('FT_CORRECT_SCORE', {'components': {1: 50, '1-0': 50}}, 'NONCANONICAL_SCORE'),
    ('FT_1X2', {'parse_status': 'BROKEN'}, 'PARSE_NOT_OK'),
    ('FT_BTTS', {'derivation': 'DERIVED_FROM_1X2'}, 'DERIVATION_ORIGIN_MISSING'),
    ('FT_1X2', {'pick_probability': 60}, 'PICK_MISSING'),
])
def test_broken_contract(market, kwargs, error):
    result = make(market, **kwargs)
    assert result['vector_status'] == 'BROKEN' and error in result['errors']
    json.dumps(result, allow_nan=False)


def test_exact_market_line_period_identity():
    corners = make('FT_CORNERS_OU', line='9.50', components={'OVER': 55, 'UNDER': 45})
    assert corners['market_id'] == 'FT_CORNERS_OU_9.5' and corners['line'] == '9.5'
    assert corners['vector_status'] == 'FULL'
    other = make('HT_CORNERS_OU', period='HT', line=4.5, components={'OVER': 55, 'UNDER': 45})
    assert other['market_id'] != corners['market_id']
    asian = make('FT_OU', line=2.25, components={'OVER': 60, 'UNDER': 40})
    assert asian['market_id'] == 'FT_OU_2.25'


def test_correct_score_pick_is_not_distribution():
    result = make('FT_CORRECT_SCORE', pick='1-0', pick_probability=18, parse_status='PARTIAL_PICK')
    assert result['vector_status'] == 'PICK_ONLY' and result['pick_probability'] == .18
    assert result['normalized_vector'] == {}
    no_prob = make('FT_CORRECT_SCORE', pick='0-0')
    assert no_prob['vector_status'] == 'PICK_ONLY' and no_prob['pick_probability'] is None
    for bad in ('1–0', '01-0', '١-٠', '1:0'):
        assert make('FT_CORRECT_SCORE', pick=bad)['vector_status'] == 'BROKEN'
    partial = make('FT_CORRECT_SCORE', components={'1-0': 100})
    assert partial['vector_status'] == 'PARTIAL'
    unbounded = make('FT_CORRECT_SCORE', components={'1-0': 100}, distribution_complete=True)
    assert 'OUTCOME_SPACE_REQUIRED' in unbounded['errors']
    distribution = make('FT_CORRECT_SCORE', components={'0-0': 50, 'OTHER_DRAW': 50},
                        distribution_complete=True, outcome_space=['0-0', 'OTHER_DRAW'])
    assert distribution['vector_status'] == 'FULL'


def test_dc_explicit_derived_and_native_are_distinct_no_new_vote():
    parent = make(components={'1': 60, 'X': 25, '2': 15})
    frozen = deepcopy(parent)
    dc = derive_double_chance(parent)
    assert dc['vector_status'] == 'FULL'
    assert dc['normalized_vector'] == pytest.approx({'1X': .85, 'X2': .4, '12': .75})
    assert sum(dc['normalized_vector'].values()) == pytest.approx(2)
    assert dc['vector_semantics'] == 'OVERLAPPING_SELECTIONS'
    assert dc['derivation'] == 'DERIVED_FROM_1X2' and dc['origin']['raw_components'] == parent['raw_components']
    assert dc['source'] == parent['source'] and not dc['adds_source_vote']
    native = make('FT_DOUBLE_CHANCE', components={'1X': 85, 'X2': 40, '12': 75})
    assert native['derivation'] == 'SOURCE' and native['origin'] is None
    assert parent == frozen
    with pytest.raises(ValueError, match='FULL_FT_1X2_REQUIRED'):
        derive_double_chance(make(components={'1': 60}))


def test_data_time_lifecycle_dimensions_separate():
    result = make(components={'1': 60, 'X': 25, '2': 15}, source_timestamp='2026-10-01T13:00:00')
    assert result['vector_status'] == 'FULL' and result['source_timestamp_utc'] is None
    assert result['audit_errors'] == ['SOURCE_TIME_UNRESOLVED']
    assert result['lifecycle'] == 'PILOT' and result['matching_status'] == 'MATCH_UNRESOLVED'
    assert not result['production_eligible']
    json.dumps(result, allow_nan=False)


def test_htft_full_space_and_empty_side():
    full = make('HTFT_1X2', period='HTFT', raw_scale=1,
                components={f'{h}/{f}': 1/9 for h in ('1', 'X', '2') for f in ('1', 'X', '2')})
    assert full['vector_status'] == 'FULL' and len(full['components']) == 9
    assert make('HT_BTTS', period='HT', components={'GG': ''})['vector_status'] == 'ABSENT'
