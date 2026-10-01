"""Common-fixture comparisons, with exclusions decided before scoring outcomes."""
from collections import Counter, defaultdict
from itertools import combinations
import hashlib
import json
import math

from app.market_reference import utc, select_references
from app.result_validation import FIELDS, compute_consensus, result_1x2
from app.market_vectors import make_vector
from analysis.coverage.report import FINAL_STATUS, SAFE_MATCH, SOURCE_TIME, _integer
from analysis.scoring.metrics import simplex, scores_1x2, calibration, bootstrap_interval


def _audit_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {'nonfinite_float': repr(value)}
    if isinstance(value, dict): return {k:_audit_value(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)): return [_audit_value(v) for v in value]
    return value


def dataset_from_rows(snapshots, observations, *, known_at):
    """Only FI native 1X2 plus certified Pin benchmarks; no pilot name joins."""
    groups = defaultdict(list)
    for row in snapshots:
        groups[row['match_id']].append(row)
    forecasts = []
    for mid, rows in sorted(groups.items()):
        versions = {(str(r.get('kickoff_utc')), r.get('home_team'), r.get('away_team'), r.get('league')) for r in rows}
        for row in rows:
            p = row['payload']; kick = utc(row.get('kickoff_utc'))
            native = make_vector('FT_1X2',period='FT',source='footinsights',raw_scale=1,
                source_timestamp=row.get('snapshot_timestamp_utc'),
                components={label:p.get('p_'+side+'_normalized') for label,side in
                            zip(('1','X','2'),('home','draw','away'))})
            is_anchor = (p.get('source', 'footinsights') == 'footinsights' and
                         p.get('source_url', '').startswith('https://footinsights.com/predictions/') and
                         not p.get('pilot_id') and p.get('channel') != 'PILOT')
            proof = (p.get('source_time_evidence') == 'SportsEvent.startDate' and kick is not None and
                     utc(p.get('kickoff_raw')) == kick)
            forecasts.append({'match_id': mid, 'method': 'footinsights', 'market_id': 'FT_1X2',
                'probabilities': [native['normalized_vector'].get(x) for x in ('1','X','2')],
                'source_components_raw': native['raw_components'],
                'adapter_normalization': native['contract_version'],
                'raw_scale': 1, 'parse_status': 'OK', 'vector_status': native['vector_status'], 'order': ['1','X','2'],
                'kickoff_utc': row.get('kickoff_utc'), 'captured_at_utc': row.get('snapshot_timestamp_utc'),
                'sealed_at_utc': row.get('imported_at'), 'valid_prematch': row.get('valid_prematch'),
                'matching_status': 'ANCHOR_NATIVE' if is_anchor and len(versions) == 1 else 'MATCH_UNRESOLVED',
                'timezone_status': p.get('timezone_status') if proof else 'TIMEZONE_UNRESOLVED',
                'role': 'MODEL', 'provenance_verified': is_anchor})
        refs = select_references(rows, known_at=known_at)
        for key, method in (('q_open', 'pinnacle_open'), ('q_close', 'pinnacle_close')):
            quote = refs[key]
            if quote is None:
                continue
            forecasts.append({'match_id': mid, 'method': method, 'market_id': 'FT_1X2',
                'probabilities': [quote['fair'][x] for x in ('1','X','2')], 'raw_scale': 1,
                'parse_status': 'OK', 'vector_status': 'FULL', 'order': ['1','X','2'],
                'kickoff_utc': quote['kickoff_utc'], 'captured_at_utc': quote['captured_at_utc'],
                'sealed_at_utc': quote['persisted_at_utc'], 'valid_prematch': True,
                'matching_status': 'EXACT_PAIR_TIME_CONTEXT', 'timezone_status': 'EXPLICIT_OFFSET',
                'role': 'MARKET_BENCHMARK', 'provenance_verified': True,
                'raw_sha256': quote['raw_sha256'], 'reference_definition': refs[key.replace('q_', '')+'_definition']})
    return {'forecasts': forecasts, 'observations': observations}


def _forecast_failure(row, known):
    if row.get('market_id') != 'FT_1X2' or tuple(row.get('order', ())) != ('1','X','2'):
        return 'MARKET_OR_ORDER_MISMATCH'
    if (row.get('parse_status') != 'OK' or row.get('vector_status') != 'FULL' or
            isinstance(row.get('raw_scale'), bool) or row.get('raw_scale') != 1):
        return 'BROKEN_PARTIAL_OR_SCALE_MISMATCH'
    try:
        if len(simplex(row.get('probabilities', ()))) != 3:
            return 'INVALID_VECTOR'
    except (ValueError, TypeError):
        return 'INVALID_VECTOR'
    kick, capture, seal = (utc(row.get(f)) for f in ('kickoff_utc','captured_at_utc','sealed_at_utc'))
    if not all((kick, capture, seal)) or row.get('timezone_status') not in SOURCE_TIME:
        return 'TIMEZONE_OR_SEAL_UNRESOLVED'
    if not capture <= seal < kick or row.get('valid_prematch') is not True:
        return 'NOT_SEALED_PREMATCH'
    if seal > known or capture > known:
        return 'NOT_KNOWN_AT'
    if row.get('matching_status') not in SAFE_MATCH or row.get('provenance_verified') is not True:
        return 'MATCH_OR_PROVENANCE_UNRESOLVED'
    if row.get('role') not in ('MODEL', 'MARKET_BENCHMARK'):
        return 'INELIGIBLE_ROLE'
    return None


def report(dataset, *, known_at, methods, policy):
    known = utc(known_at)
    if known is None: raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    methods = tuple(methods)
    if not methods or len(set(methods)) != len(methods) or any(not m for m in methods):
        raise ValueError('EXPLICIT_UNIQUE_COMPARISON_METHODS_REQUIRED')
    excluded = Counter(); latest = {}; ambiguous = set()
    # All forecast exclusions and tie decisions happen without reading outcomes.
    for row in dataset['forecasts']:
        if row['method'] not in methods: continue
        reason = _forecast_failure(row, known)
        if reason: excluded['forecast:'+reason] += 1; continue
        key = (row['match_id'],row['method']); at = utc(row['captured_at_utc'])
        previous = latest.get(key)
        if previous is None or at > utc(previous['captured_at_utc']):
            latest[key] = row; ambiguous.discard(key)
        elif at == utc(previous['captured_at_utc']) and row != previous:
            ambiguous.add(key)
    for key in ambiguous: del latest[key]; excluded['forecast:AMBIGUOUS_LATEST'] += 1
    selected = {}; ambiguous_results = set()
    for row in dataset['observations']:
        at = utc(row.get('observed_at_utc')); key = (row['match_id'],row['provider'])
        if at is None or at > known:
            excluded['result:NOT_KNOWN_AT'] += 1; continue
        previous = selected.get(key)
        if previous is None or at > utc(previous['observed_at_utc']):
            selected[key] = row; ambiguous_results.discard(key)
        elif at == utc(previous['observed_at_utc']) and row != previous:
            ambiguous_results.add(key)
    groups = defaultdict(list)
    for key, row in selected.items():
        if row['provider'].startswith('pilot:'): excluded['result:PILOT_ONLY'] += 1; continue
        if key in ambiguous_results or str(row.get('status','')).upper() not in FINAL_STATUS:
            excluded['result:NONFINAL_OR_AMBIGUOUS'] += 1; continue
        groups[row['match_id']].append({**row, **{f: row.get(f) if _integer(row.get(f)) else None for f in FIELDS}})
    outcomes = {}
    for mid, rows in groups.items():
        values, fields, _ = compute_consensus(rows)
        if all(fields[f]['status'] == 'VERIFIED' for f in ('final_home_goals','final_away_goals')):
            outcomes[mid] = result_1x2(values['final_home_goals'],values['final_away_goals'])
        else: excluded['result:FT_NOT_VERIFIED'] += 1
    coverage = {m: {mid for mid,method in latest if method == m} for m in methods}
    common = set.intersection(*(coverage[m] for m in methods)) & set(outcomes)
    # A fixture revision or premature result cannot leak into evaluation.
    for mid in tuple(common):
        kicks = {utc(latest[mid,m]['kickoff_utc']) for m in methods}
        if len(kicks) != 1 or next(iter(kicks)) > known or any(
                utc(r['observed_at_utc']) < next(iter(kicks)) for r in groups[mid]):
            common.remove(mid); excluded['sample:KICKOFF_MISMATCH_OR_PREMATURE_RESULT'] += 1
    common = sorted(common); n = len(common); method_reports = {}; score_rows = {}
    for m in methods:
        rows = [scores_1x2(latest[mid,m]['probabilities'],outcomes[mid],policy) for mid in common]
        score_rows[m] = rows
        metrics = {name: {'mean': sum(r[name] for r in rows)/n if n else None,
                         'ci': bootstrap_interval([r[name] for r in rows],policy)}
                   for name in ('log_loss','brier','rps')}
        method_reports[m] = {'n': n, 'eligible_forecasts': len(coverage[m]),
            'verified_overlap_before_common_set': len(coverage[m] & set(outcomes)),
            'metrics': metrics, 'extreme_probability_rows': sum(r['extreme_probability'] for r in rows),
            'per_outcome_brier': {label: sum(r['brier_components'][i] for r in rows)/n if n else None
                                 for i,label in enumerate(('1','X','2'))},
            'calibration': {label: calibration([r['p_raw'][i] for r in rows],
                [int(outcomes[mid] == label) for mid in common],policy) for i,label in enumerate(('1','X','2'))},
            'scored_rows': [{'match_id':mid, **r,
                'source_components_raw': latest[mid,m].get('source_components_raw'),
                'adapter_normalization': latest[mid,m].get('adapter_normalization')}
                for mid,r in zip(common,rows)]}
    comparisons = []
    for left,right in combinations(methods,2):
        comparisons.append({'left': left, 'right': right, 'n': n,
            'difference_definition': 'LEFT_MINUS_RIGHT_LOWER_IS_BETTER',
            'paired': {metric: {'mean': sum(a[metric]-b[metric] for a,b in zip(score_rows[left],score_rows[right]))/n if n else None,
                'ci': bootstrap_interval([a[metric]-b[metric] for a,b in zip(score_rows[left],score_rows[right])],policy)}
                for metric in ('log_loss','brier','rps')}})
    return {'report_version': 'proper-scoring-v1', 'mode': 'READ_ONLY_DIAGNOSTIC',
        'known_at': known.isoformat(), 'market_id': 'FT_1X2', 'policy': policy.audit(),
        'input_sha256': hashlib.sha256(json.dumps(_audit_value(dataset),sort_keys=True,default=str,
                        separators=(',', ':'),allow_nan=False).encode()).hexdigest(),
        'requested_methods': list(methods), 'common_match_ids': common, 'n': n,
        'sample_definition': 'SAME_COMPLETE_SAFE_SEALED_PREMATCH_FIXTURES_WITH_VERIFIED_FT',
        'status': 'INSUFFICIENT_SAMPLE' if n < 2 else 'DIAGNOSTIC_ONLY',
        'methods': method_reports, 'paired_comparisons': comparisons,
        'outcome_reference': {'kind': 'IN_SAMPLE_BASE_RATE_REFERENCE',
            'frequencies': {label: sum(outcomes[mid] == label for mid in common)/n if n else None
                            for label in ('1','X','2')}, 'predictive_baseline': False},
        'clv': {'status': 'NOT_COMPUTED', 'reason': 'REQUIRES_FROZEN_SELECTION_AND_PRICE_SEPARATE_FROM_PROPER_SCORES'},
        'excluded': dict(sorted(excluded.items())), 'production_eligible': False,
        'promotion_verdict': None}
