"""Competition label coverage with explicit denominators and verification gates."""
from collections import Counter, defaultdict
from datetime import timedelta
import math
import re

from app.identities import normalize_alias
from app.market_reference import utc
from app.market_vectors import make_vector
from app.market_engine import required_result_fields
from app.result_validation import compute_consensus, FIELDS

SAFE_MATCH = {'ANCHOR_NATIVE', 'EXACT_PAIR_TIME_CONTEXT', 'CANONICAL_PAIR_TIME_CONTEXT'}
SOURCE_TIME = {'EXPLICIT_OFFSET', 'VERIFIED_SOURCE_TZ'}
FINAL_STATUS = {'FT', 'FINISHED', 'COMPLETED', 'FINAL', 'FULL_TIME', 'SETTLED'}


def _group(row):
    # Label grouping is descriptive, never a canonical competition binding.
    return (normalize_alias(row.get('league')) or 'UNRESOLVED',
            normalize_alias(row.get('country')), normalize_alias(row.get('category')))


def _integer(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0 and value == int(value))


def _verified(result, market):
    if not re.fullmatch(r'(?:(?:FT|HT)_(?:1X2|BTTS|CORRECT_SCORE|(?:CORNERS_)?OU(?:_[0-9]+(?:\.[0-9]+)?)?)|FT_DOUBLE_CHANCE|HTFT_1X2)', market):
        return False
    fields = required_result_fields(market)
    return bool(fields) and all(_integer(result.get(f)) and
        result['field_verification'].get(f, {}).get('status') == 'VERIFIED' for f in fields)


def report(dataset, *, known_at):
    known = utc(known_at)
    if known is None:
        raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    start = known - timedelta(days=7)
    latest, excluded = {}, Counter()
    for row in dataset['predictions']:
        capture, seal = utc(row.get('captured_at_utc')), utc(row.get('sealed_at_utc'))
        if capture is None or capture > known or (seal and seal > known):
            excluded['NOT_OBSERVED_AT_CUTOFF'] += 1
            continue
        key = (row['source'], row['source_match_key'])
        if key not in latest or capture > utc(latest[key]['captured_at_utc']):
            latest[key] = row
        elif capture == utc(latest[key]['captured_at_utc']) and row != latest[key]:
            # Ambiguity cannot be resolved by input iteration order.
            latest[key] = {**row, 'matching_status': 'AMBIGUOUS', 'parse_status': 'BROKEN'}
    observation_groups, provider_sets, pilot_provider_sets = defaultdict(list), defaultdict(set), defaultdict(set)
    selected_obs = {}
    for obs in dataset['observations']:
        at = utc(obs.get('observed_at_utc'))
        if at is None or at > known:
            excluded['RESULT_NOT_OBSERVED_AT_CUTOFF'] += 1
            continue
        key = (obs['match_id'], obs['provider'])
        previous = selected_obs.get(key)
        if previous is None or at > utc(previous['observed_at_utc']):
            selected_obs[key] = obs
        elif at == utc(previous['observed_at_utc']) and obs != previous:
            selected_obs[key] = {**obs, 'status': 'AMBIGUOUS'}
    for obs in selected_obs.values():
        mid, provider = obs['match_id'], obs['provider']
        if provider.startswith('pilot:'):
            pilot_provider_sets[mid].add(provider)
            continue
        provider_sets[mid].add(provider)
        if str(obs.get('status', '')).upper() not in FINAL_STATUS:
            excluded['NONFINAL_OR_AMBIGUOUS_RESULT'] += 1
            continue
        observation_groups[mid].append({**obs, **{f:obs.get(f) if _integer(obs.get(f)) else None for f in FIELDS}})
    results = {}
    for mid, observations in observation_groups.items():
        values, verification, _ = compute_consensus(observations)
        results[mid] = {**values, 'field_verification': verification}
    groups = defaultdict(list)
    for row in latest.values():
        groups[_group(row)].append(row)
    tickets = defaultdict(list)
    for ticket in dataset['market_tickets']:
        tickets[_group(ticket)].append(ticket)
    competitions = []
    for group in sorted(set(groups) | set(tickets)):
        rows, market_tickets = groups[group], tickets[group]
        by_source, anchors, coverage_providers, pilot_providers = defaultdict(list), {}, set(), set()
        sources = []
        for row in rows:
            by_source[row['source']].append(row)
            if row.get('source') == 'footinsights' and row.get('is_anchor') is True:
                anchors[row['match_id']] = row
        for source in sorted(by_source):
            full, timezone, safe, sealed = 0, 0, 0, 0
            for row in by_source[source]:
                probabilities = row.get('probabilities')
                shape_ok = isinstance(probabilities, (list, tuple)) and len(probabilities) == 3
                probabilities = probabilities if shape_ok else [None] * 3
                vector = make_vector('FT_1X2', period='FT', source=source,
                    components=dict(zip(('1','X','2'), probabilities)), raw_scale=row.get('raw_scale'),
                    parse_status=row.get('parse_status', 'BROKEN') if shape_ok else 'BROKEN')
                full += vector['vector_status'] == 'FULL'
                kick, capture, seal = (utc(row.get(f)) for f in
                                       ('kickoff_utc','captured_at_utc','sealed_at_utc'))
                tz = kick is not None and row.get('timezone_status') in SOURCE_TIME
                timezone += tz
                prematch = bool(kick and capture and seal and capture <= seal < kick
                                and row.get('valid_prematch') is True)
                sealed += prematch
                matched = row.get('matching_status') in SAFE_MATCH and row.get('fixture_versions', 1) == 1
                if row.get('matching_status') == 'ANCHOR_NATIVE' and row.get('is_anchor') is not True:
                    matched = False
                safe += matched and tz and prematch
            n = len(by_source[source])
            sources.append({'source':source,'latest_source_rows':n,'full_vectors':full,
                'full_vector_rate':full/n,'timezone_resolved':timezone,'timezone_resolved_rate':timezone/n,
                'sealed_prematch':sealed,'safe_matches':safe,'safe_match_rate':safe/n,
                'production_eligible':False})
        verified_week = set()
        for mid, row in anchors.items():
            coverage_providers.update(provider_sets[mid]); pilot_providers.update(pilot_provider_sets[mid])
            kick = utc(row.get('kickoff_utc'))
            if kick and start <= kick <= known and _verified(results.get(mid, {}), 'FT_1X2'):
                verified_week.add(mid)
        settled, invalid_settled = defaultdict(set), 0
        for ticket in market_tickets:
            kick, updated = utc(ticket.get('kickoff_utc')), utc(ticket.get('updated_at'))
            if not kick or not updated or not start <= kick <= known or updated > known:
                continue
            if ticket.get('status') not in ('WON','LOST','PUSH'):
                continue
            if ticket.get('source') != 'footinsights' or not _verified(results.get(ticket['match_id'], {}), ticket['market_id']):
                invalid_settled += 1
                continue
            settled[ticket['market_id']].add(ticket['match_id'])
        blockers = ['NO_APPROVED_COMPETITION_COVERAGE_POLICY', 'CANONICAL_BINDING_NOT_ASSERTED']
        if not verified_week: blockers.append('NO_VERIFIED_FT_IN_LAST_7D')
        if not any(s['safe_matches'] for s in sources): blockers.append('NO_SAFE_SOURCE_ROWS')
        if invalid_settled: blockers.append('UNVERIFIED_SETTLED_TICKETS')
        competitions.append({'competition_label':group[0], 'country':group[1] or None,
            'source_labels_observed':sorted({r.get('league') or '' for r in rows+market_tickets}),
            'category':group[2] or None, 'canonical_competition_id':None,
            'label_scope':'DESCRIPTIVE_EXACT_LABEL_ONLY', 'sources':sources,
            'result_providers_observed':sorted(coverage_providers),
            'pilot_result_providers_observed':sorted(pilot_providers),
            'verified_ft_fixtures_last7d':len(verified_week),
            'settled_fixtures_last7d':len(set().union(*settled.values())) if settled else 0,
            'settled_fixtures_by_market':{m:len(ids) for m,ids in sorted(settled.items())},
            'unverified_settled_tickets':invalid_settled,
            'state':'MONITORING_ONLY' if rows else 'INSUFFICIENT',
            'production_eligible':False,'blockers':blockers})
    return {'report_version':'competition-coverage-v1','mode':'READ_ONLY_DIAGNOSTIC',
            'known_at':known.isoformat(),'week_start_utc':start.isoformat(),
            'weekly_definition':'DISTINCT_FIXTURES_WITH_KICKOFF_IN_TRAILING_7D',
            'production_eligible':False,'competitions':competitions,
            'denominators':{'input_prediction_rows':len(dataset['predictions']),
                            'latest_source_rows':len(latest),'competition_labels':len(competitions)},
            'excluded':dict(excluded)}
