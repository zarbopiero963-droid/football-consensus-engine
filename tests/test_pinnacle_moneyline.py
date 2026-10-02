"""Strict FT1X2 gates, malformed-row isolation and a fresh public DOM replay."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.pinnacle_moneyline import parse_moneylines, CONTRACT
from prospective.collect import american_to_decimal, fair_from_odds
from staging.same_market.pinnacle import _document


NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
KICK = NOW + timedelta(hours=6)


def source():
    fixture = {'id': 1, 'type': 'matchup', 'isLive': False, 'units': 'Regular',
               'status': 'pending', 'parentId': None, 'parent': None,
               'league': {'id': 1, 'name': 'Test league', 'sport': {'id': 29}},
               'startTime': KICK.isoformat(), 'participants': [
                   {'alignment': 'home', 'name': 'Home'}, {'alignment': 'away', 'name': 'Away'}]}
    market = {'matchupId': 1, 'type': 'moneyline', 'period': 0, 'status': 'open',
              'key': 's;0;m', 'isAlternate': False, 'cutoffAt': KICK.isoformat(),
              'prices': [{'designation': 'away', 'price': 200},
                         {'designation': 'home', 'price': 100},
                         {'designation': 'draw', 'price': -150}]}
    return [fixture], [market]


def parse(fixtures, markets, **changes):
    return parse_moneylines(fixtures, markets, **{
        'league_id': 1, 'league_name': 'Test league', 'captured_at': NOW, **changes})


def test_preserves_explicit_hda_order_prices_fair_and_legacy_overround():
    quotes, audit = parse(*source())
    assert audit['contract'] == CONTRACT and audit['accepted'] == 1 and not audit['rejected']
    q = quotes[0]
    assert q['american'] == [100, -150, 200]
    assert q['dec'] == [2, 1.6667, 3]
    assert (q['fair'], q['overround']) == fair_from_odds(*q['dec'])
    assert sum(q['fair']) == pytest.approx(1)
    assert q['captured_at'] < q['start'] and q['home'] == 'Home'


@pytest.mark.parametrize('field,value,reason', [
    ('isLive', None, 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('isLive', 0, 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('isLive', True, 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('type', None, 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('type', 'special', 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('units', None, 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('units', 'Corners', 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('status', 'started', 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('parentId', 2, 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('parent', {}, 'MATCHUP_NOT_EXPLICIT_PREMATCH'),
    ('league', [], 'SOURCE_LEAGUE_OR_SPORT_MISMATCH'),
    ('startTime', '2026-10-02T06:00:00', 'INVALID_PREMATCH_TIME_OR_CUTOFF'),
    ('startTime', NOW.isoformat(), 'INVALID_PREMATCH_TIME_OR_CUTOFF'),
    ('startTime', '0001-01-01T00:00:00+14:00', 'INVALID_PREMATCH_TIME_OR_CUTOFF'),
    ('participants', {}, 'INVALID_PARTICIPANTS'),
    ('participants', [None, None], 'INVALID_PARTICIPANTS'),
    ('participants', [{'alignment': [], 'name': 'Home'}, {'alignment': 'away', 'name': 'Away'}], 'INVALID_PARTICIPANTS'),
])
def test_bad_fixture_is_rejected_without_crashing_other_quotes(field, value, reason):
    f, m = source()
    f.append(deepcopy(f[0])); f[1]['id'] = 2
    m.append(deepcopy(m[0])); m[1]['matchupId'] = 2
    f[0][field] = value
    quotes, audit = parse(f, m)
    assert [q['matchup_id'] for q in quotes] == [2]
    assert audit['rejected'] == {reason: 1}


@pytest.mark.parametrize('field,value,reason', [
    ('isAlternate', None, 'INVALID_MARKET_CONTRACT'),
    ('isAlternate', 0, 'INVALID_MARKET_CONTRACT'),
    ('isAlternate', True, 'OTHER_PERIOD_OR_ALTERNATE'),
    ('period', False, 'INVALID_MARKET_CONTRACT'),
    ('period', 0.0, 'INVALID_MARKET_CONTRACT'),
    ('period', 1, 'OTHER_PERIOD_OR_ALTERNATE'),
    ('key', None, 'INVALID_MARKET_CONTRACT'),
    ('key', 's;1;m', 'INVALID_MARKET_CONTRACT'),
    ('key', 'unknown;m', 'INVALID_MARKET_CONTRACT'),
    ('status', None, 'MARKET_NOT_OPEN'),
    ('status', 'suspended', 'MARKET_NOT_OPEN'),
    ('cutoffAt', None, 'INVALID_PREMATCH_TIME_OR_CUTOFF'),
    ('cutoffAt', NOW.isoformat(), 'INVALID_PREMATCH_TIME_OR_CUTOFF'),
    ('cutoffAt', (KICK + timedelta(seconds=1)).isoformat(), 'INVALID_PREMATCH_TIME_OR_CUTOFF'),
    ('cutoffAt', KICK.replace(tzinfo=None).isoformat(), 'INVALID_PREMATCH_TIME_OR_CUTOFF'),
    ('prices', {}, 'INVALID_PRICES'),
    ('prices', [None, None, None], 'INVALID_PRICES'),
])
def test_bad_market_cannot_price_a_snapshot(field, value, reason):
    f, m = source(); m[0][field] = value
    quotes, audit = parse(f, m)
    assert quotes == [] and audit['rejected'] == {reason: 1}


@pytest.mark.parametrize('price', [True, '100', None, 0, 99, -99, float('nan'), float('inf'), 10 ** 500, -10 ** 500, -10 ** 300])
def test_invalid_numeric_prices_remain_missing(price):
    f, m = source(); m[0]['prices'][0]['price'] = price
    assert parse(f, m)[1]['rejected'] == {'INVALID_PRICES': 1}


@pytest.mark.parametrize('change', ['duplicate_price', 'duplicate_fixture', 'duplicate_market',
                                  'damaged_duplicate_market', 'duplicate_participant', 'missing_draw',
                                  'wrong_sport', 'wrong_league', 'wrong_league_name', 'bool_id'])
def test_ambiguity_and_source_identity_fail_closed(change):
    f, m = source()
    if change == 'duplicate_price': m[0]['prices'].append({'designation': 'home', 'price': 1000})
    if change == 'duplicate_fixture': f.append(deepcopy(f[0]))
    if change == 'duplicate_market': m.append(deepcopy(m[0]))
    if change == 'damaged_duplicate_market': m.append(dict(m[0], prices=None))
    if change == 'duplicate_participant': f[0]['participants'].append(deepcopy(f[0]['participants'][0]))
    if change == 'missing_draw': m[0]['prices'][2]['designation'] = 'home'
    if change == 'wrong_sport': f[0]['league']['sport']['id'] = 3
    if change == 'wrong_league': f[0]['league']['id'] = True
    if change == 'wrong_league_name': f[0]['league']['name'] = 'Other'
    if change == 'bool_id': m[0]['matchupId'] = True
    quotes, audit = parse(f, m)
    assert not quotes and audit['rejected']


def test_malformed_rows_and_containers_are_visible_and_isolated():
    f, m = source(); f.extend([None, [], {'id': True}]); m.extend([None, [], {'matchupId': []}])
    quotes, audit = parse(f, m)
    assert len(quotes) == 1
    assert audit['rejected']['INVALID_MATCHUP_ROW'] == 3
    assert audit['rejected']['INVALID_MARKET_ROW'] == 2
    assert parse({}, m)[1]['rejected'] == {'INVALID_DOCUMENT_CONTAINER': 1}


@pytest.mark.parametrize('changes', [{'captured_at': NOW.replace(tzinfo=None)}, {'league_id': True}, {'league_name': ''}])
def test_invalid_capture_context_is_explicit(changes):
    with pytest.raises(ValueError, match='INVALID_CAPTURE_CONTEXT'):
        parse(*source(), **changes)


def test_fresh_public_capture_replay_and_duplicate_home_regression():
    path = Path(__file__).parents[1] / 'staging/pinnacle_transport/moneyline_capture_20261002.json'
    bundle = json.loads(path.read_text())
    assert bundle['session_stopped'] is True
    f, m = [_document(bundle['documents'][kind], 1842, kind) for kind in ('matchups', 'markets')]
    assert (len(f), len(m)) == (19, 653)
    kwargs = {'league_id': 1842, 'league_name': bundle['league_name'], 'captured_at': bundle['captured_at_utc']}
    quotes, audit = parse_moneylines(f, m, **kwargs)
    assert len(quotes) == 17 and audit['accepted'] == 17
    for quote in quotes:
        assert quote['dec'] == [american_to_decimal(p) for p in quote['american']]
        assert (quote['fair'], quote['overround']) == fair_from_odds(*quote['dec'])
    mid = 1637151323
    original = next(q for q in quotes if q['matchup_id'] == mid)
    assert original['dec'][0] == 4.04
    market = next(r for r in m if r.get('matchupId') == mid and r.get('type') == 'moneyline' and r.get('period') == 0)
    market['prices'].append({'designation': 'home', 'price': 1000})
    changed, audit = parse_moneylines(f, m, **kwargs)
    assert len(changed) == 16 and mid not in {q['matchup_id'] for q in changed}
    assert audit['rejected']['INVALID_PRICES'] == 1
    assert [q for q in quotes if q['matchup_id'] != mid] == changed
