"""Frozen shown-ticket scoring. Snapshot probability/price drift cannot affect it."""
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo
import hashlib
import json
import math

from app.market_reference import utc, select_references
from app.result_validation import FIELDS, compute_consensus, result_1x2
from analysis.coverage.report import FINAL_STATUS, SOURCE_TIME, _integer
from analysis.scoring.metrics import simplex, scores_1x2, calibration
from analysis.scoring.report import _audit_value
from analysis.decisions.protocol import day_interval, verdict

ORDER = ('1', 'X', '2')
# API freezes six-decimal components. Accept their rounding, never repair them.
FROZEN_SUM_TOLERANCE = 1e-5


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def ticket_failure(row, known):
    view = row.get('snapshot_view')
    original = row.get('original_snapshot')
    if not isinstance(view, dict) or not isinstance(original, dict):
        return 'MISSING_FROZEN_VIEW_OR_ORIGINAL_AUDIT'
    payload = original.get('payload') or {}
    kick, snap, requested, created, batch_created, seal = (utc(v) for v in (
        row.get('kickoff_utc'), row.get('snapshot_timestamp_utc'), row.get('requested_at_utc'),
        row.get('created_at'), row.get('batch_created_at'), original.get('imported_at')))
    if not all((kick, snap, requested, created, batch_created, seal)):
        return 'TIME_OR_SEAL_UNRESOLVED'
    if not snap <= seal <= requested < kick or not max(created, batch_created) < kick:
        return 'NOT_FROZEN_PREMATCH'
    if max(requested, created, batch_created, seal) > known:
        return 'NOT_KNOWN_AT'
    if (view.get('match_id') != row.get('match_id') or original.get('match_id') != row.get('match_id') or
            utc(view.get('kickoff_utc')) != kick or utc(original.get('kickoff_utc')) != kick or
            utc(view.get('snapshot_timestamp_utc')) != snap or utc(original.get('snapshot_timestamp_utc')) != snap or
            any(not row.get(f) or row[f] != original.get(f) or row[f] != view.get(v)
                for f, v in (('home_team','home'), ('away_team','away'), ('league','league')))):
        return 'IDENTITY_OR_REFERENCED_SNAPSHOT_MISMATCH'
    if (original.get('valid_prematch') is not True or payload.get('source', 'footinsights') != 'footinsights' or
            not str(payload.get('source_url', '')).startswith('https://footinsights.com/predictions/') or
            payload.get('pilot_id') or payload.get('channel') == 'PILOT'):
        return 'PROVENANCE_OR_PRODUCTION_UNRESOLVED'
    if (payload.get('timezone_status') not in SOURCE_TIME or
            payload.get('source_time_evidence') != 'SportsEvent.startDate' or utc(payload.get('kickoff_raw')) != kick):
        return 'SOURCE_TIME_UNRESOLVED'
    if (view.get('status') != 'BET' or row.get('display_status') != 'BET' or
            view.get('strategies') != row.get('strategies') or not view.get('strategies')):
        return 'FROZEN_COHORT_MISMATCH'
    try:
        p = simplex([view.get(k) for k in ('p1','px','p2')], sum_tolerance=FROZEN_SUM_TOLERANCE)
    except (ValueError, TypeError):
        return 'INVALID_OR_INCOMPLETE_FROZEN_VECTOR'
    pick, probability = view.get('model_pick'), view.get('model_probability')
    if (pick not in ORDER or row.get('model_pick') != pick or not _finite(probability) or
            probability != row.get('model_probability') or probability != p[ORDER.index(pick)] or
            view.get('picked_odds') != row.get('picked_odds')):
        return 'FROZEN_PICK_OR_VALUE_MISMATCH'
    return None


def _market(row):
    """Frozen values only; the exact original row certifies quote provenance."""
    view = row['snapshot_view']
    fair = view.get('pinnacle_fair')
    prices = view.get('pinnacle_odds')
    quote = select_references([row['original_snapshot']], known_at=row['requested_at_utc'])['q_open']
    if not quote or not isinstance(prices, dict):
        return None, None
    odds = view.get('picked_odds')
    picked = view['model_pick']
    price_ok = (all(_finite(prices.get(x)) and prices[x] > 1 and
                    abs(prices[x]-quote['odds'][x]) <= 1e-5 for x in ORDER) and
                _finite(odds) and odds > 1 and odds == prices.get(picked))
    benchmark = None
    if isinstance(fair, dict):
        try:
            values = simplex([fair.get(x) for x in ORDER], sum_tolerance=FROZEN_SUM_TOLERANCE)
            if all(abs(values[i]-quote['fair'][x]) <= 1e-5 for i, x in enumerate(ORDER)):
                benchmark = values
        except (ValueError, TypeError):
            pass
    return benchmark, odds if price_ok else None


def _outcomes(observations, known, tickets, excluded):
    latest = {}; ambiguous = set()
    for row in observations:
        at = utc(row.get('observed_at_utc'))
        key = (row['match_id'], row['provider'])
        if at is None or at > known: continue
        prior = latest.get(key)
        if prior is None or at > utc(prior['observed_at_utc']):
            latest[key] = row; ambiguous.discard(key)
        elif at == utc(prior['observed_at_utc']) and row != prior:
            ambiguous.add(key)
    groups = defaultdict(list)
    invalid = set()
    for key, row in latest.items():
        mid = row['match_id']
        if mid not in tickets or str(row['provider']).startswith('pilot:'): continue
        if key in ambiguous:
            invalid.add(mid); continue
        kick = utc(tickets[mid]['kickoff_utc'])
        result_kick = utc(row.get('kickoff_utc'))
        if (utc(row['observed_at_utc']) < kick or (result_kick is not None and result_kick != kick)):
            invalid.add(mid); continue
        if str(row.get('status', '')).upper() not in FINAL_STATUS:
            continue
        groups[mid].append({**row, **{f: row.get(f) if _integer(row.get(f)) else None for f in FIELDS}})
    outcomes = {}
    for mid in tickets:
        if mid in invalid:
            excluded['result:AMBIGUOUS_OR_KICKOFF_MISMATCH'] += 1; continue
        values, fields, _ = compute_consensus(groups[mid])
        if all(fields[f]['status'] == 'VERIFIED' for f in ('final_home_goals', 'final_away_goals')):
            outcomes[mid] = result_1x2(values['final_home_goals'], values['final_away_goals'])
        else:
            excluded['result:FT_NOT_VERIFIED'] += 1
    return outcomes


def report(dataset, *, known_at, protocol, registration=None):
    known = utc(known_at)
    if known is None: raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    # Freeze terminal evidence at the predetermined grace deadline. Later corrections
    # can be diagnosed separately, but cannot change this experiment's verdict.
    cutoff = min(known, protocol.final_cutoff())
    start, end = utc(protocol.window_start), utc(protocol.window_end)
    excluded = Counter(); groups = defaultdict(list)
    for row in dataset['tickets']:
        requested = utc(row.get('requested_at_utc')); kick = utc(row.get('kickoff_utc'))
        if (row.get('shown_to_user') is not True or row.get('display_status') != 'BET' or
                protocol.strategy not in (row.get('strategies') or [])):
            continue
        if not requested or not kick:
            excluded['cohort:TIME_UNRESOLVED'] += 1; continue
        if not start <= requested < end or not start <= kick < end or requested > cutoff:
            continue
        groups[row['match_id']].append(row)
    first = {}
    # Pick BEFORE reading probabilities, prices, provenance or outcomes. An invalid
    # first ticket is never replaced with a convenient later one.
    for mid, rows in sorted(groups.items()):
        rows.sort(key=lambda r: (utc(r['requested_at_utc']), r['batch_id']))
        original = rows[0]
        contexts = {(utc(r['kickoff_utc']), r.get('home_team'), r.get('away_team'), r.get('league')) for r in rows}
        tied = [r for r in rows if utc(r['requested_at_utc']) == utc(original['requested_at_utc'])]
        if len(contexts) != 1 or any(r['snapshot_view'] != original['snapshot_view'] for r in tied):
            excluded['ticket:AMBIGUOUS_FIRST_OR_CONTEXT'] += 1; continue
        reason = ticket_failure(original, cutoff)
        if reason: excluded['ticket:'+reason] += 1; continue
        first[mid] = original
    outcomes = _outcomes(dataset['observations'], cutoff, first, excluded)
    score_rows = []; paired = []; priced = []
    for mid, row in sorted(first.items()):
        if mid not in outcomes: continue
        view = row['snapshot_view']; outcome = outcomes[mid]
        day = utc(row['kickoff_utc']).astimezone(ZoneInfo('Europe/Rome')).date().isoformat()
        scores = scores_1x2([view[k] for k in ('p1','px','p2')], outcome, protocol.score_policy,
                            sum_tolerance=FROZEN_SUM_TOLERANCE)
        score_rows.append({'match_id': mid, 'batch_id': row['batch_id'], 'day': day,
            'outcome': outcome, 'model_pick': view['model_pick'],
            'model_probability': view['model_probability'], 'picked_odds': view.get('picked_odds'),
            'pick_correct': view['model_pick'] == outcome, **scores})
        benchmark, odds = _market(row)
        if benchmark is None:
            excluded['benchmark:FROZEN_CERTIFIED_QUOTE_MISSING'] += 1
        else:
            reference = scores_1x2(benchmark, outcome, protocol.score_policy, sum_tolerance=FROZEN_SUM_TOLERANCE)
            paired.append({'match_id': mid, 'batch_id': row['batch_id'], 'day': day,
                'model': scores, 'benchmark': reference,
                'delta': {m: scores[m]-reference[m] for m in ('log_loss','brier','rps')}})
        if odds is None:
            excluded['roi:FROZEN_CERTIFIED_PRICE_MISSING'] += 1
        else:
            profit = (odds-1)*(1-protocol.commission) if view['model_pick'] == outcome else -1
            priced.append({'match_id': mid, 'batch_id': row['batch_id'], 'day': day,
                          'picked_odds': odds, 'stake': 1, 'net_profit': profit})
    n = len(score_rows)
    def metric(rows, name):
        return {'mean': sum(r[name] for r in rows)/len(rows) if rows else None,
                'ci': day_interval([(r['day'], r[name]) for r in rows], protocol.score_policy)}
    quality = verdict(protocol, registration, known_at=known, denominator=len(groups),
        rows=[(r['day'],r['delta']['log_loss']) for r in paired], kind='quality')
    roi = verdict(protocol, registration, known_at=known, denominator=len(groups),
        rows=[(r['day'],r['net_profit']) for r in priced], kind='roi')
    return {'report_version': 'frozen-ticket-v1', 'mode': 'READ_ONLY', 'market_id': 'FT_1X2',
        'known_at': known.isoformat(), 'evidence_cutoff': cutoff.isoformat(),
        'protocol': json.loads(protocol.canonical()), 'protocol_sha256': protocol.sha256(),
        'registration': registration, 'probability_source': 'decision_tickets.snapshot_view',
        'snapshot_use': 'EXACT_REFERENCED_ROW_AUDIT_ONLY_NO_VALUE_REFRESH',
        'selection': 'FIRST_SHOWN_BET_PER_FIXTURE_IN_STRATEGY_AND_WINDOW_BEFORE_VALIDATION',
        'input_tickets': len(dataset['tickets']),
        'repeat_tickets_ignored': sum(len(rows)-1 for rows in groups.values()),
        'cohort_fixtures': len(groups), 'eligible_first_tickets': len(first), 'n': n,
        'input_sha256': hashlib.sha256(json.dumps(_audit_value(dataset), sort_keys=True, default=str,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
        'model': {'metrics': {m: metric(score_rows,m) for m in ('log_loss','brier','rps')},
            'calibration': {x: calibration([r['p_raw'][i] for r in score_rows],
                [int(r['outcome']==x) for r in score_rows],protocol.score_policy) for i,x in enumerate(ORDER)},
            'pick_calibration': calibration([r['model_probability'] for r in score_rows],
                [int(r['pick_correct']) for r in score_rows],protocol.score_policy),
            'hit_rate': sum(r['pick_correct'] for r in score_rows)/n if n else None,
            'scored_rows': score_rows},
        'benchmark': {'role': 'MARKET_BENCHMARK_FROZEN_AT_SHOWING', 'n': len(paired),
            'difference': 'MODEL_MINUS_FROZEN_BENCHMARK_LOWER_IS_BETTER',
            'paired_metrics': {m: metric([{'day':r['day'],**r['delta']} for r in paired],m)
                               for m in ('log_loss','brier','rps')}, 'rows': paired, 'verdict': quality},
        'roi': {'kind': 'VIRTUAL_FLAT_ONE_UNIT_AT_FROZEN_PRICE', 'commission': protocol.commission,
            'n': len(priced), 'stake': len(priced),
            'net_profit': sum(r['net_profit'] for r in priced) if priced else None,
            'roi': roi['mean'], 'rows': priced, 'verdict': roi,
            'cost_scope': 'PRICE_AND_DECLARED_WINNING_COMMISSION_ONLY; INFRASTRUCTURE_COSTS_NOT_INCLUDED'},
        'excluded': dict(sorted(excluded.items())), 'production_eligible': False,
        'promotion_verdict': None,
        'rounding_policy': {'sum_tolerance': FROZEN_SUM_TOLERANCE, 'raw_values_renormalized': False,
            'log_loss_only': 'EXPLICIT_CLIP_THEN_RENORMALIZE', 'epsilon': protocol.score_policy.epsilon}}
