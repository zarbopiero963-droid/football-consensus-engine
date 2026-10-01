"""Deterministic T1–T8 diagnostics. All probabilities use explicit input scales."""
from collections import Counter, defaultdict
from fractions import Fraction
import math
from statistics import pstdev

from app.market_reference import utc

VERSION = 'robustness-research-v1'
EPSILONS = (1e-6, 1e-4, .01, .05)
SUM_TOLERANCE = .01
MODEL_ROLES = {'INDEPENDENT_MODEL_CANDIDATE', 'MODEL_CANDIDATE'}
SAFE_MATCHES = {'ANCHOR_NATIVE', 'EXACT_PAIR_TIME_CONTEXT', 'CANONICAL_PAIR_TIME_CONTEXT'}
SOURCE_TIMEZONES = {'EXPLICIT_OFFSET', 'VERIFIED_SOURCE_TZ'}


def vector(values, scale):
    if scale not in (1, 100) or isinstance(scale, bool):
        raise ValueError('SCALE_UNRESOLVED')
    if not isinstance(values, (list, tuple)) or len(values) != 3 or any(v is None for v in values):
        raise ValueError('INCOMPLETE_VECTOR')
    try:
        if any(isinstance(v, bool) for v in values):
            raise ValueError
        p = [float(v) / scale for v in values]
    except (ValueError, TypeError, OverflowError):
        raise ValueError('INVALID_VECTOR') from None
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in p):
        raise ValueError('INVALID_VECTOR')
    total = math.fsum(p)
    if total <= 0 or abs(total - 1) > SUM_TOLERANCE + 1e-12:
        raise ValueError('INVALID_SUM')
    return [v / total for v in p]


def pool(vectors, *, method='linear', epsilon=1e-6, weights=None):
    if not vectors:
        raise ValueError('EMPTY_POOL')
    ps = [vector(v, 1) for v in vectors]
    ws = [1.] * len(ps) if weights is None else list(weights)
    if len(ws) != len(ps) or not all(math.isfinite(w) and w > 0 for w in ws):
        raise ValueError('INVALID_WEIGHTS')
    ws = [w / math.fsum(ws) for w in ws]
    if method == 'linear':
        return [math.fsum(w * p[i] for w, p in zip(ws, ps)) for i in range(3)]
    if method != 'log' or not math.isfinite(epsilon) or not 0 < epsilon < 1/3:
        raise ValueError('INVALID_LOG_POLICY')
    clipped = [[max(v, epsilon) for v in p] for p in ps]
    clipped = [[v / math.fsum(p) for v in p] for p in clipped]
    logs = [math.fsum(w * math.log(p[i]) for w, p in zip(ws, clipped)) for i in range(3)]
    exp = [math.exp(v - max(logs)) for v in logs]
    return [v / math.fsum(exp) for v in exp]


def pick(p):
    winners = [i for i, v in enumerate(p) if math.isclose(v, max(p), abs_tol=1e-12, rel_tol=0)]
    return winners[0] if len(winners) == 1 else None


def tv(a, b):
    return math.fsum(abs(x-y) for x, y in zip(a, b)) / 2


def required_votes(n, threshold=Fraction(2, 3)):
    if not isinstance(threshold, Fraction) or not 0 < threshold <= 1 or n < 1:
        raise ValueError('INVALID_AGREEMENT_POLICY')
    return (threshold.numerator * n + threshold.denominator - 1) // threshold.denominator


def view(p, vectors, *, market=None):
    choice = pick(p)
    votes = sum(pick(v) == choice for v in vectors) if choice is not None else 0
    required = required_votes(len(vectors))
    return {'vector': p, 'pick': ('1', 'X', '2')[choice] if choice is not None else None,
            'max_probability': max(p), 'entropy': -math.fsum(v*math.log(v) for v in p if v > 0),
            'agreement': {'numerator': votes, 'denominator': len(vectors), 'required': required},
            'draft_gate_050': len(vectors) >= 2 and choice is not None and max(p) >= .5 and votes >= required,
            'draft_gate_060': len(vectors) >= 2 and choice is not None and max(p) >= .6 and votes >= required,
            'edges': [p[i] - market[i] for i in range(3)] if market is not None else None}


def difference(base, changed):
    return {'tv': tv(base['vector'], changed['vector']), 'pick_flip': base['pick'] != changed['pick'],
            'delta_max_probability': changed['max_probability'] - base['max_probability'],
            'delta_agreement': changed['agreement']['numerator']/changed['agreement']['denominator']
                               - base['agreement']['numerator']/base['agreement']['denominator'],
            'gate_050_flip': base['draft_gate_050'] != changed['draft_gate_050'],
            'gate_060_flip': base['draft_gate_060'] != changed['draft_gate_060'],
            'delta_edges': [y-x for x, y in zip(base['edges'], changed['edges'])]
                           if base['edges'] is not None and changed['edges'] is not None else None}


def diagnostics(sources, *, market=None, clusters=None):
    """Caller supplies eligible models. Cluster IDs are diagnostic inputs, never inferred."""
    names = sorted(sources)
    if any(n.casefold() == 'pinnacle' for n in names):
        raise ValueError('MARKET_SOURCE_FORBIDDEN')
    ps = [vector(sources[n], 1) for n in names]
    if len(names) < 2:
        raise ValueError('MULTISOURCE_REQUIRED')
    market = vector(market, 1) if market is not None else None
    lin = view(pool(ps), ps, market=market)
    log = view(pool(ps, method='log'), ps, market=market)
    loo = {}
    for index, name in enumerate(names):
        remaining = ps[:index] + ps[index+1:]
        loo[name] = {method: difference(base, view(pool(remaining, method=method), remaining, market=market))
                     for method, base in [('linear', lin), ('log', log)]}
    epsilon = {str(e): difference(log, view(pool(ps, method='log', epsilon=e), ps, market=market))
               for e in EPSILONS}
    perturbation = []
    for index, name in enumerate(names):
        for multiplier in (.7, 1.3):
            weights = [1.] * len(ps)
            weights[index] = multiplier
            perturbation.append({'source': name, 'multiplier': multiplier,
                **{method: difference(base, view(pool(ps, method=method, weights=weights), ps, market=market))
                   for method, base in [('linear', lin), ('log', log)]}})
    clones = []
    for index, name in enumerate(names):
        for copies in (1, 3):
            for noisy in (False, True):
                clone = ps[index][:]
                if noisy:
                    clone = [(v + 1e-4) / 1.0003 for v in clone]
                attacked = ps + [clone] * copies
                # Exact known synthetic clone family gets one aggregate vote.
                capped = ps[:]
                capped[index] = pool([ps[index]] + [clone] * copies)
                clones.append({'source': name, 'extra_copies': copies, 'tiny_noise': noisy,
                    'uncapped': difference(lin, view(pool(attacked), attacked, market=market)),
                    'capped': difference(lin, view(pool(capped), capped, market=market)),
                    'uncapped_votes': view(pool(attacked), attacked)['agreement'],
                    'capped_votes': view(pool(capped), capped)['agreement']})
    veto = []
    for name, p in zip(names, ps):
        choice = pick(p)
        if choice is None:
            continue
        hostile = [(1-1e-6)/2] * 3
        hostile[choice] = 1e-6
        reference = view(p, [p], market=market)
        veto.append({'source': name, 'role': 'SYNTHETIC_NEAR_ZERO_ATTACK',
                     'linear': difference(reference, view(pool([p, hostile]), [p, hostile], market=market)),
                     'log_by_epsilon': {str(e): difference(reference,
                         view(pool([p, hostile], method='log', epsilon=e), [p, hostile], market=market))
                         for e in EPSILONS}})
    cluster_view = None
    if clusters is not None:
        if set(clusters) != set(names) or not all(isinstance(c, str) and c for c in clusters.values()):
            raise ValueError('INCOMPLETE_CLUSTER_MAP')
        grouped = defaultdict(list)
        for name, p in zip(names, ps):
            grouped[clusters[name]].append(p)
        centroids = [pool(grouped[c]) for c in sorted(grouped)]
        cluster_view = {'role': 'SUPPLIED_CLUSTER_DIAGNOSTIC', 'clusters': dict(sorted(clusters.items())),
                        'linear': view(pool(centroids), centroids, market=market)}
    shrink = None
    if market is not None:
        contaminated = view(pool([lin['vector'], market]), [lin['vector'], market], market=market)
        shrink = {'role': 'NEGATIVE_CONTROL_MARKET_SHRINK', 'production_eligible': False,
                  'base_edges': lin['edges'], 'contaminated_edges': contaminated['edges'],
                  'comparison': difference(lin, contaminated)}
    return {'linear': lin, 'log_shadow': log, 'linear_vs_log': difference(lin, log),
            'sigma': [pstdev(p[i] for p in ps) for i in range(3)], 'loo': loo,
            'epsilon_sensitivity': epsilon, 'weight_perturbation': perturbation,
            'clone_sensitivity': clones, 'near_zero_veto': veto, 'cluster_diagnostic': cluster_view,
            'market_benchmark': market, 'market_negative_control': shrink}


def eligibility(row, known_at):
    reasons = []
    try:
        p = vector(row.get('probabilities'), row.get('raw_scale'))
    except ValueError as exc:
        p = None
        reasons.append(str(exc))
    if not row.get('source') or row.get('role') not in MODEL_ROLES:
        reasons.append('NON_MODEL_OR_UNCLASSIFIED')
    if str(row.get('source', '')).casefold() == 'pinnacle':
        reasons.append('MARKET_SOURCE_FORBIDDEN')
    if row.get('parse_status') != 'OK':
        reasons.append('PARSE_NOT_OK')
    if row.get('match_status') not in SAFE_MATCHES or not row.get('match_id'):
        reasons.append('NAME_ONLY' if row.get('match_status') == 'FOUND_NAME_ONLY' else 'UNVERIFIED_MATCH')
    if row.get('timezone_status') not in SOURCE_TIMEZONES:
        reasons.append('TIMEZONE_UNRESOLVED')
    kickoff, captured, sealed = [utc(row.get(k)) for k in ('kickoff_utc', 'captured_at_utc', 'sealed_at_utc')]
    if not all((kickoff, captured, sealed)):
        reasons.append('TIME_AUDIT_MISSING')
    elif captured >= kickoff or sealed >= kickoff or sealed < captured:
        reasons.append('NOT_SEALED_PREMATCH')
    elif max(captured, sealed) > known_at:
        reasons.append('NOT_KNOWN_AT')
    if not row.get('row_id'):
        reasons.append('PROVENANCE_MISSING')
    return p, reasons


def report(rows, *, known_at):
    known_at = utc(known_at)
    if known_at is None:
        raise ValueError('TIMEZONE_UNRESOLVED')
    grouped, excluded = defaultdict(dict), []
    for row in rows:
        p, reasons = eligibility(row, known_at)
        if reasons:
            excluded.append({'row_id': row.get('row_id'), 'source': row.get('source'),
                             'match_id': row.get('match_id'), 'reasons': reasons})
            continue
        # Keep full timestamps, not date/slot counts. Different kickoff revisions
        # are distinct research groups, never silently merged.
        key = (row['match_id'], utc(row['kickoff_utc']).isoformat())
        grouped[key].setdefault(row['source'], []).append((row, p))
    revisions = defaultdict(set)
    for mid, kickoff in grouped:
        revisions[mid].add(kickoff)
    for key in list(grouped):
        if len(revisions[key[0]]) > 1:
            for source, observations in grouped.pop(key).items():
                for r, _ in observations:
                    excluded.append({'row_id': r['row_id'], 'source': source, 'match_id': key[0],
                                     'reasons': ['AMBIGUOUS_FIXTURE_REVISION']})
    matches = []
    for (mid, kickoff), candidates in sorted(grouped.items()):
        sources, provenance = {}, {}
        for source, observations in sorted(candidates.items()):
            latest = max(utc(r['captured_at_utc']) for r, _ in observations)
            newest = [(r, p) for r, p in observations if utc(r['captured_at_utc']) == latest]
            if len({tuple(p) for _, p in newest}) != 1:
                for r, _ in newest:
                    excluded.append({'row_id': r['row_id'], 'source': source, 'match_id': mid,
                                     'reasons': ['AMBIGUOUS_LATEST_VECTOR']})
                continue
            r, p = min(newest, key=lambda item: item[0]['row_id'])
            sources[source] = p
            provenance[source] = {'row_id': r['row_id'], 'captured_at_utc': utc(r['captured_at_utc']).isoformat(),
                                  'sealed_at_utc': utc(r['sealed_at_utc']).isoformat(),
                                  'raw_scale': r['raw_scale'], 'raw_probabilities': r['probabilities']}
        state = 'SMOKE_ONLY' if len(sources) >= 2 else 'INSUFFICIENT_SOURCES'
        matches.append({'match_id': mid, 'kickoff_utc': kickoff, 'status': state,
                        'included_sources': sorted(sources), 'provenance': provenance,
                        'diagnostics': diagnostics(sources) if len(sources) >= 2 else None})
    clean = [m for m in matches if m['diagnostics'] is not None]
    reasons = Counter(reason for r in excluded for reason in r['reasons'])
    influence = defaultdict(list)
    for m in clean:
        for source, metric in m['diagnostics']['loo'].items():
            influence[source].append(metric['linear']['tv'])
    distances = sorted(m['diagnostics']['linear_vs_log']['tv'] for m in clean)
    clone_metrics = [c for m in clean for c in m['diagnostics']['clone_sensitivity']]
    veto_metrics = [v for m in clean for v in m['diagnostics']['near_zero_veto']]
    return {'version': VERSION, 'role': 'READ_ONLY_RESEARCH', 'production_eligible': False,
            'known_at_utc': known_at.isoformat(), 'sample_status': 'INSUFFICIENT_SAMPLE',
            'policy': {'epsilon_grid': EPSILONS, 'sum_tolerance': SUM_TOLERANCE,
                       'agreement': '2/3 integer ceil', 'weight_multipliers': [.7, 1.3],
                       'exact_zero': 'VALID_MODEL_ZERO_LOG_CLIPPED_DIAGNOSTIC_ONLY',
                       'cluster_policy': 'SUPPLIED_ONLY_NO_INFERENCE', 'strategy_signals': False},
            'counts': {'input_rows': len(rows), 'excluded_rows': len(excluded),
                       'match_groups': len(matches), 'multisource_matches': len(clean),
                       'exclusion_reasons': dict(sorted(reasons.items()))},
            'aggregate': {'source_coverage': dict(sorted(Counter(s for m in matches for s in m['included_sources']).items())),
                          'multisource_coverage': dict(sorted(Counter(s for m in clean for s in m['included_sources']).items())),
                          'linear_log_tv': {'n': len(distances), 'mean': math.fsum(distances)/len(distances) if distances else None,
                                            'p90': distances[max(0, math.ceil(.9*len(distances))-1)] if distances else None,
                                            'max': max(distances) if distances else None},
                          'pick_disagreements': sum(m['diagnostics']['linear_vs_log']['pick_flip'] for m in clean),
                          'gate_050_crossings': sum(m['diagnostics']['linear_vs_log']['gate_050_flip'] for m in clean),
                          'gate_060_crossings': sum(m['diagnostics']['linear_vs_log']['gate_060_flip'] for m in clean),
                          'clone_max_uncapped_tv': max((c['uncapped']['tv'] for c in clone_metrics), default=None),
                          'clone_max_capped_tv': max((c['capped']['tv'] for c in clone_metrics), default=None),
                          'clone_uncapped_gate_flips': sum(c['uncapped']['gate_050_flip'] for c in clone_metrics),
                          'veto_log_pick_flips': sum(v['log_by_epsilon']['1e-06']['pick_flip'] for v in veto_metrics),
                          'loo_mean_tv': {s: math.fsum(v)/len(v) for s, v in sorted(influence.items())}},
            'matches': matches, 'excluded': sorted(excluded, key=lambda r: (r['row_id'] or '', r['source'] or ''))}
