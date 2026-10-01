from datetime import datetime, timedelta, timezone

import pytest

from app.source_fallback import select_result_fallback

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
ROW = {'provider': 'pilot:worldfootball', 'channel': 'PILOT', 'match_id': 'fixture',
       'observed_at_utc': NOW.isoformat(), 'http_status': 200, 'capture_state': 'READY_PARSED',
       'raw_page_sha256': 'real-hash-reference', 'status': 'FT',
       'match_status': 'EXACT_PAIR_KICKOFF_COMPETITION_DIAGNOSTIC',
       'result_match_contract': 'RESULT_PAIR_DATE_COMPETITION_V1',
       'competition': 'Serie A', 'home_team': 'Home', 'away_team': 'Away',
       'production_eligible': False, 'final_home_goals': 2, 'final_away_goals': 0,
       'corners_home': 9}  # Nonqualified field must never be selected.


def route(rows, channel='PILOT'):
    return select_result_fallback('pilot:smart_soccer_stats', rows, channel=channel, match_id='fixture', now=NOW)


def test_qualified_alternative_retains_identity_zero_and_only_supported_fields():
    report = route([ROW])
    ft = report['fields']['final_away_goals']
    assert ft['status'] == 'FALLBACK_SELECTED' and ft['value'] == 0
    assert ft['provider'] == 'pilot:worldfootball' and ft['verification'] == 'SINGLE_SOURCE'
    for field in ('ht_home_goals', 'corners_home', 'ht_corners_home'):
        assert report['fields'][field]['value'] is None
        assert report['fields'][field]['verification'] == 'MISSING'
    assert not report['production_eligible'] and not report['creates_consensus_vote']


@pytest.mark.parametrize('change,reason', [
    ({'provider': 'unknown'}, 'SOURCE_NOT_QUALIFIED'),
    ({'match_id': 'other'}, 'MATCH_MISMATCH'),
    ({'channel': 'PRODUCTION'}, 'CHANNEL_MISMATCH'),
    ({'observed_at_utc': (NOW - timedelta(hours=4)).isoformat()}, 'STALE_OR_UNOBSERVED'),
    ({'observed_at_utc': (NOW + timedelta(seconds=1)).isoformat()}, 'STALE_OR_UNOBSERVED'),
    ({'observed_at_utc': '2026-10-01'}, 'STALE_OR_UNOBSERVED'),
    ({'http_status': 504}, 'CAPTURE_UNACCEPTED'),
    ({'capture_state': 'MAINTENANCE'}, 'CAPTURE_UNACCEPTED'),
    ({'raw_page_sha256': None}, 'EVIDENCE_INCOMPLETE'),
    ({'match_status': 'FOUND_NAME_ONLY'}, 'MATCH_UNSAFE'),
    ({'match_status': 'EXACT_PAIR_DATE'}, 'MATCH_UNSAFE'),
    ({'result_match_contract': None}, 'MATCH_UNSAFE'),
    ({'competition': ''}, 'MATCH_UNSAFE'),
])
def test_unqualified_stale_or_failed_evidence_stays_missing(change, reason):
    report = route([{**ROW, **change}])
    assert report['fields']['final_home_goals']['status'] == 'NO_QUALIFIED_FALLBACK'
    assert report['rejected'][0]['reason'] == reason


def test_conflicts_block_and_same_provider_is_not_an_independent_confirmation():
    other = {**ROW, 'provider': 'pilot:smart_soccer_stats'}
    assert route([ROW, ROW])['fields']['final_home_goals']['verification'] == 'SINGLE_SOURCE'
    assert route([ROW, other])['fields']['final_home_goals']['verification'] == 'VERIFIED'
    report = route([ROW, {**other, 'final_home_goals': 3}])
    assert report['fields']['final_home_goals']['status'] == 'CONFLICT'
    assert report['fields']['final_home_goals']['value'] is None
    assert report['fields']['final_away_goals']['status'] == 'PRIMARY'


def test_pilot_flags_cannot_self_promote_and_production_has_no_qualified_alternative():
    report = route([{**ROW, 'production_eligible': True, 'channel': 'PRODUCTION'}], 'PRODUCTION')
    assert all(f['status'] == 'NO_QUALIFIED_FALLBACK' for f in report['fields'].values())
    with pytest.raises(ValueError): route([], 'UNKNOWN')


def test_normalized_finished_adapter_status_is_accepted_but_live_or_ht_is_rejected():
    assert route([{**ROW, 'status': 'FINISHED'}])['fields']['final_away_goals']['value'] == 0
    for status in ('HT', 'LIVE', 'SCHEDULED', ''):
        report = route([{**ROW, 'status': status}])
        assert report['fields']['final_away_goals']['value'] is None
        assert report['rejected'][0]['reason'] == 'EVIDENCE_INCOMPLETE'
