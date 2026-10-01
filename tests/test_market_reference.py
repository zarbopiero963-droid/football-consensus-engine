from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import api
from app.market_reference import MATCH_CONTRACT, exact_market_match, select_references
from prospective.collect import parse_iso

KICK = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)


def quote(at=KICK - timedelta(hours=2), *, odds=(2, 3, 4)):
    return {'match_id': 'test-fixture', 'kickoff_utc': KICK,
            'snapshot_timestamp_utc': at - timedelta(seconds=10), 'imported_at': at + timedelta(seconds=1),
            'home_team': 'United', 'away_team': 'Town', 'league': 'Test league', 'valid_prematch': True,
            'payload': {'pin_kickoff_utc': KICK.isoformat(), 'pin_home': 'United', 'pin_away': 'Town',
                        'pin_league': 'Test league', 'pin_matchup_id': '123',
                        'pin_snapshot_timestamp_utc': at.isoformat(),
                        'odds_home': odds[0], 'odds_draw': odds[1], 'odds_away': odds[2],
                        'sha256_odds_raw': 'a' * 64, 'market_version': 3,
                        'name_score_note': MATCH_CONTRACT}}


def pair():
    return ({'home': 'United', 'away': 'Town', 'kickoff': KICK, 'league': 'Test league'},
            {'home': 'United', 'away': 'Town', 'start': KICK, 'league': 'Test league'})


def test_exact_pair_and_tight_time():
    model, market = pair()
    assert exact_market_match(model, market, KICK - timedelta(seconds=1))
    market['start'] += timedelta(minutes=15)
    assert exact_market_match(model, market, KICK - timedelta(seconds=1))
    market['start'] += timedelta(seconds=1)
    assert not exact_market_match(model, market, KICK - timedelta(seconds=1))


@pytest.mark.parametrize('field,value', [('home', 'Manchester United'), ('away', 'Town U21'),
    ('away', 'Town Women'), ('away', 'Town II'), ('home', 'FC United'), ('league', 'Other league'),
    ('league', ''), ('country', 'GB'), ('category', 'MEN:SENIOR:FIRST'),
    ('start', KICK + timedelta(hours=5)), ('start', KICK.replace(tzinfo=None))])
def test_unsafe_match_rejected(field, value):
    model, market = pair()
    market[field] = value
    assert not exact_market_match(model, market, KICK - timedelta(hours=1))


def test_kickoff_capture_and_naive_time_rejected():
    model, market = pair()
    assert not exact_market_match(model, market, KICK)
    assert parse_iso('2026-10-01T16:00:00') is None
    assert parse_iso('2026-10-01T18:00:00+02:00') == KICK


def test_open_close_proxy_asof_and_future_price_cannot_leak():
    first, last = quote(), quote(KICK - timedelta(minutes=1), odds=(2.1, 3, 3.8))
    rows = [last, first, quote(KICK), quote(KICK + timedelta(seconds=1))]
    before = select_references(rows, known_at=KICK - timedelta(minutes=30))
    assert before['status'] == 'PROVISIONAL' and before['q_close'] is None
    assert before['q_open']['odds']['1'] == 2
    closed = select_references(rows, known_at=KICK + timedelta(hours=3))
    assert closed['q_close']['odds']['1'] == 2.1
    assert closed['q_close']['seconds_before_kickoff'] == 60
    assert closed['rejected']['NOT_SEALED_PRE_KICKOFF'] == 2
    assert sum(closed['q_close']['fair'].values()) == pytest.approx(1)
    assert closed['q_open']['overround'] == pytest.approx(1/2 + 1/3 + 1/4)
    assert select_references(rows, known_at=KICK + timedelta(days=20))['q_close'] == closed['q_close']


@pytest.mark.parametrize('mutate,reason', [
    (lambda r: r.update(imported_at=KICK), 'NOT_SEALED_PRE_KICKOFF'),
    (lambda r: r['payload'].update(pin_snapshot_timestamp_utc='2026-10-01T14:00:00'), 'TIMEZONE_OR_AUDIT_MISSING'),
    (lambda r: r['payload'].update(odds_home=float('nan')), 'INVALID_ODDS'),
    (lambda r: r['payload'].update(odds_draw=None), 'INVALID_ODDS'),
    (lambda r: r['payload'].update(odds_away=1), 'INVALID_ODDS'),
    (lambda r: r['payload'].update(sha256_odds_raw=''), 'PROVENANCE_MISSING'),
    (lambda r: r['payload'].update(pin_away='Town U21'), 'PAIR_MISMATCH'),
    (lambda r: r['payload'].update(pin_league='Other league'), 'CONTEXT_MISMATCH'),
])
def test_invalid_quote_stays_missing(mutate, reason):
    row = quote()
    mutate(row)
    result = select_references([row], known_at=KICK)
    assert result['q_open'] is result['q_close'] is None
    assert result['rejected'] == {reason: 1}


def test_provider_identity_revision_or_conflicting_capture_is_ambiguous():
    one = quote()
    for changes in ({'pin_matchup_id': '456'}, {'odds_home': 2.5},
                    {'pin_kickoff_utc': (KICK + timedelta(hours=1)).isoformat()}):
        two = deepcopy(one)
        two['payload'].update(changes)
        if 'pin_kickoff_utc' in changes:
            two['kickoff_utc'] += timedelta(hours=1)
        assert select_references([one, two], known_at=KICK + timedelta(hours=2))['status'] == 'AMBIGUOUS_SERIES'


def test_legacy_cannot_be_certified_and_inputs_are_immutable():
    row = quote()
    row['payload']['name_score_note'] = 'auto'
    original = deepcopy(row)
    result = select_references([row], known_at=KICK)
    assert result['status'] == 'LEGACY_UNCERTIFIED'
    assert result['q_open'] is result['q_close'] is None
    assert result['legacy_diagnostic']['production_eligible'] is False
    assert row == original


def test_naive_api_asof_is_422():
    assert TestClient(api.app).get('/market-reference/test?known_at=2026-10-01T10:00:00').status_code == 422
