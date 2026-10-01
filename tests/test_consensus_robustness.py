from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path

import pytest

from staging.consensus_robustness.harness import (diagnostics, eligibility, pool, report,
    required_votes, vector, view)

ROOT = Path(__file__).resolve().parents[1] / 'staging/consensus_robustness/fixtures'
KICK = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)


def row(source='a', values=(.6, .25, .15), at=None, **changes):
    at = at or KICK - timedelta(hours=2)
    return {'row_id': source + at.isoformat(), 'source': source, 'match_id': 'synthetic',
            'probabilities': list(values), 'raw_scale': 1, 'role': 'MODEL_CANDIDATE',
            'parse_status': 'OK', 'match_status': 'EXACT_PAIR_TIME_CONTEXT',
            'timezone_status': 'EXPLICIT_OFFSET', 'kickoff_utc': KICK.isoformat(),
            'captured_at_utc': at.isoformat(), 'sealed_at_utc': (at + timedelta(seconds=1)).isoformat(),
            **changes}


def test_frozen_pack_hash_and_all_cases():
    expected, filename = (ROOT / 'SHA256SUMS').read_text().strip().split()
    assert hashlib.sha256((ROOT / filename).read_bytes()).hexdigest() == expected
    pack = json.loads((ROOT / filename).read_text())
    assert pack['provenance'] == 'SYNTHETIC_TEST_ONLY_NOT_SOURCE_EVIDENCE'
    assert len(pack['cases']) == 11
    for case in pack['cases']:
        if 'error' in case:
            with pytest.raises(ValueError, match=case['error']):
                vector(case['values'], case['scale'])
        elif 'values' in case:
            assert vector(case['values'], case['scale']) == pytest.approx(case['expected'])
        else:
            result = diagnostics(case['sources'], market=case.get('market'), clusters=case.get('clusters'))
            assert sum(result['linear']['vector']) == pytest.approx(1)
            assert sum(result['log_shadow']['vector']) == pytest.approx(1)
            json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('method', ['linear', 'log'])
def test_identical_order_and_known_weighted_formula(method):
    same = [.6, .3, .1]
    assert pool([same, same], method=method) == pytest.approx(same)
    a, b = [.7, .2, .1], [.3, .4, .3]
    assert pool([a, b], method=method, weights=[1, 2]) == pytest.approx(
        pool([b, a], method=method, weights=[2, 1]))
    if method == 'linear':
        assert pool([a, b], weights=[1, 2]) == pytest.approx([13/30, 1/3, 7/30])
    else:
        import math
        geometric = [a[i]**(1/3) * b[i]**(2/3) for i in range(3)]
        assert pool([a, b], method=method, weights=[1, 2]) == pytest.approx(
            [v / math.fsum(geometric) for v in geometric])


@pytest.mark.parametrize('n,votes', [(2, 2), (3, 2), (4, 3), (6, 4), (7, 5)])
def test_integer_ceil_gate(n, votes):
    assert required_votes(n) == votes
    assert required_votes(n, Fraction(1, 1)) == n


def test_clones_inflate_vote_but_known_cluster_cap_restores_baseline():
    base = diagnostics({'a': [.6, .25, .15], 'b': [.2, .3, .5]})
    clones = diagnostics({'a': [.6, .25, .15], 'c1': [.6, .25, .15],
                          'c2': [.6, .25, .15], 'b': [.2, .3, .5]},
                         clusters={'a': 'a', 'c1': 'a', 'c2': 'a', 'b': 'b'})
    assert clones['linear']['agreement'] == {'numerator': 3, 'denominator': 4, 'required': 3}
    capped = clones['cluster_diagnostic']['linear']
    assert capped['vector'] == pytest.approx(base['linear']['vector'])
    assert capped['agreement'] == {'numerator': 1, 'denominator': 2, 'required': 2}
    for attack in base['clone_sensitivity']:
        if not attack['tiny_noise']:
            assert attack['capped']['tv'] == pytest.approx(0)
        else:
            assert attack['capped']['tv'] < 1e-3
    one_cluster = diagnostics({'a': [.6, .25, .15], 'b': [.6, .25, .15]},
                              clusters={'a': 'same', 'b': 'same'})
    assert not one_cluster['cluster_diagnostic']['linear']['draft_gate_050']


def test_loo_identical_irrelevant_and_influential_source():
    same = diagnostics({'a': [.6, .3, .1], 'b': [.6, .3, .1]})
    assert same['loo']['a']['linear']['tv'] == pytest.approx(0)
    outlier = diagnostics({'a': [.7, .2, .1], 'b': [.7, .2, .1], 'flat': [.3, .35, .35]})
    assert outlier['loo']['flat']['linear']['tv'] > outlier['loo']['a']['linear']['tv']
    assert len(outlier['loo']) == 3


def test_zero_valid_missing_rejected_clip_pre_pool_and_veto():
    assert vector([1, 0, 0], 1) == [1, 0, 0]
    with pytest.raises(ValueError, match='INCOMPLETE_VECTOR'):
        vector([1, None, 0], 1)
    assert pool([[1, 0, 0], [1, 0, 0]], method='log', epsilon=.01) == pytest.approx([1/1.02, .01/1.02, .01/1.02])
    result = diagnostics({'a': [.8, .15, .05], 'hostile': [.000001, .4999995, .4999995]})
    assert result['linear']['pick'] == '1'
    assert result['log_shadow']['pick'] != '1'
    assert result['epsilon_sensitivity']['0.05']['tv'] > .1
    assert result['near_zero_veto'][0]['log_by_epsilon']['1e-06']['pick_flip']


def test_market_outside_normal_pool_and_negative_control_halves_edge():
    result = diagnostics({'a': [.7, .2, .1], 'b': [.7, .2, .1]}, market=[.4, .35, .25])
    assert result['linear']['vector'] == pytest.approx([.7, .2, .1])
    negative = result['market_negative_control']
    assert negative['role'] == 'NEGATIVE_CONTROL_MARKET_SHRINK' and not negative['production_eligible']
    assert negative['contaminated_edges'] == pytest.approx([e/2 for e in negative['base_edges']])
    with pytest.raises(ValueError, match='MARKET_SOURCE_FORBIDDEN'):
        diagnostics({'a': [.7, .2, .1], 'pinnacle': [.4, .35, .25]})
    assert 'MARKET_SOURCE_FORBIDDEN' in eligibility(row('pinnacle'), KICK)[1]


@pytest.mark.parametrize('changes,reason', [
    ({'match_status': 'FOUND_NAME_ONLY'}, 'NAME_ONLY'),
    ({'match_status': 'FOUND'}, 'UNVERIFIED_MATCH'),
    ({'timezone_status': 'VERIFIED_BROWSER_TZ'}, 'TIMEZONE_UNRESOLVED'),
    ({'timezone_status': 'TIMEZONE_UNRESOLVED'}, 'TIMEZONE_UNRESOLVED'),
    ({'role': 'HYBRID_MARKET_AWARE'}, 'NON_MODEL_OR_UNCLASSIFIED'),
    ({'role': 'COMMUNITY_CONSENSUS'}, 'NON_MODEL_OR_UNCLASSIFIED'),
    ({'role': 'UNKNOWN'}, 'NON_MODEL_OR_UNCLASSIFIED'),
    ({'sealed_at_utc': None}, 'TIME_AUDIT_MISSING'),
    ({'captured_at_utc': KICK.isoformat()}, 'NOT_SEALED_PREMATCH'),
    ({'sealed_at_utc': KICK.isoformat()}, 'NOT_SEALED_PREMATCH'),
    ({'kickoff_utc': '2026-10-01T16:00:00'}, 'TIME_AUDIT_MISSING'),
    ({'parse_status': 'PARTIAL_PICK'}, 'PARSE_NOT_OK'),
    ({'probabilities': [60, 25, 15]}, 'INVALID_VECTOR'),
    ({'probabilities': [float('nan'), .3, .7]}, 'INVALID_VECTOR'),
    ({'probabilities': [float('inf'), .3, .7]}, 'INVALID_VECTOR'),
    ({'probabilities': [-.1, .3, .8]}, 'INVALID_VECTOR'),
    ({'probabilities': [.6, None, .4]}, 'INCOMPLETE_VECTOR'),
    ({'probabilities': [.6, .3, .3]}, 'INVALID_SUM'),
    ({'raw_scale': None}, 'SCALE_UNRESOLVED'),
])
def test_invalid_inputs_never_become_models(changes, reason):
    p, reasons = eligibility(row(**changes), KICK)
    assert reason in reasons
    result = report([row('a'), row('b', **changes)], known_at=KICK)
    assert result['counts']['multisource_matches'] == 0
    assert result['sample_status'] == 'INSUFFICIENT_SAMPLE'


def test_latest_valid_snapshot_per_source_and_no_future_or_slot_inflation():
    rows = [row('a'), row('a', [.7, .2, .1], KICK-timedelta(hours=1)), row('b'),
            row('a', [.8, .15, .05], KICK+timedelta(seconds=1))]
    result = report(rows, known_at=KICK)
    assert result['counts']['multisource_matches'] == 1
    assert result['matches'][0]['included_sources'] == ['a', 'b']
    assert result['matches'][0]['diagnostics']['linear']['vector'] == pytest.approx([.65, .225, .125])
    early = report(rows, known_at=KICK-timedelta(minutes=90))
    assert early['matches'][0]['diagnostics']['linear']['vector'] == pytest.approx([.6, .25, .15])
    assert early['counts']['exclusion_reasons']['NOT_KNOWN_AT'] == 1


def test_conflicting_latest_and_kickoff_revisions_rejected():
    a, conflict = row('a'), row('a', [.7, .2, .1], row_id='conflict')
    assert report([a, conflict, row('b')], known_at=KICK)['counts']['multisource_matches'] == 0
    revised = row('a', kickoff_utc=(KICK+timedelta(hours=1)).isoformat())
    result = report([a, revised, row('b')], known_at=KICK)
    assert result['counts']['match_groups'] == 0
    assert result['counts']['exclusion_reasons']['AMBIGUOUS_FIXTURE_REVISION'] == 3


def test_order_invariance_ties_no_mutation_and_no_signals():
    rows = [row('a', [.7, .2, .1]), row('b', [.1, .6, .3])]
    original = deepcopy(rows)
    result = report(rows, known_at=KICK)
    assert result == report(rows[::-1], known_at=KICK)
    assert rows == original
    assert result['matches'][0]['diagnostics']['linear']['pick'] is None
    assert not result['matches'][0]['diagnostics']['linear']['draft_gate_050']
    assert result['policy']['strategy_signals'] is False and result['production_eligible'] is False
    assert 'tickets' not in result and 'strategies' not in result


@pytest.mark.parametrize('weights', [[0, 1], [-1, 2], [float('nan'), 1], [1]])
def test_bad_weight_policy_rejected(weights):
    with pytest.raises(ValueError, match='INVALID_WEIGHTS'):
        pool([[.6, .3, .1], [.5, .3, .2]], weights=weights)


def test_policy_errors_are_visible():
    with pytest.raises(ValueError, match='TIMEZONE_UNRESOLVED'):
        report([], known_at='2026-10-01T10:00:00')
    with pytest.raises(ValueError, match='INCOMPLETE_CLUSTER_MAP'):
        diagnostics({'a': [.6, .3, .1], 'b': [.5, .3, .2]}, clusters={'a': 'one'})
    assert report([], known_at=KICK)['aggregate']['linear_log_tv']['mean'] is None
