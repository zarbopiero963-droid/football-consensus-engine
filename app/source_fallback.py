"""Qualification-gated result routing. A route is evidence, never a consensus vote."""
from datetime import datetime, timedelta, timezone

from app.result_validation import FIELDS


FT = frozenset(FIELDS[:2])
# Existing adapters only. Pilot qualification does not confer production rights.
QUALIFIED_FIELDS = {
    'PILOT': {'pilot:smart_soccer_stats': FT | frozenset(FIELDS[2:4]),
              'pilot:worldfootball': FT},
    'PRODUCTION': {'fifa-calendar-v3': FT},
}


def select_result_fallback(primary, candidates, *, channel, match_id, now):
    if channel not in QUALIFIED_FIELDS or now.tzinfo is None:
        raise ValueError('explicit channel and timezone required')
    accepted, rejected = [], []
    for row in candidates:
        provider = row.get('provider')
        reason = None
        allowed = QUALIFIED_FIELDS[channel].get(provider)
        try:
            observed = datetime.fromisoformat(row['observed_at_utc'])
        except (KeyError, TypeError, ValueError):
            observed = None
        if not allowed:
            reason = 'SOURCE_NOT_QUALIFIED'
        elif row.get('match_id') != match_id:
            reason = 'MATCH_MISMATCH'
        elif row.get('channel') != channel:
            reason = 'CHANNEL_MISMATCH'
        elif (observed is None or observed.tzinfo is None or
              not now - timedelta(hours=3) <= observed <= now):
            reason = 'STALE_OR_UNOBSERVED'
        elif row.get('http_status') != 200 or row.get('capture_state') != 'READY_PARSED':
            reason = 'CAPTURE_UNACCEPTED'
        elif not row.get('raw_page_sha256') or row.get('status') not in {'FT', 'FINISHED'}:
            reason = 'EVIDENCE_INCOMPLETE'
        elif channel == 'PILOT' and (row.get('match_status') not in {
                'EXACT_PAIR_DATE_COMPETITION_DIAGNOSTIC', 'EXACT_PAIR_KICKOFF_COMPETITION_DIAGNOSTIC'}
                or row.get('result_match_contract') != 'RESULT_PAIR_DATE_COMPETITION_V1'
                or not row.get('competition') or not row.get('home_team') or not row.get('away_team')):
            reason = 'MATCH_UNSAFE'
        elif channel == 'PRODUCTION' and (row.get('production_eligible') is not True or
                row.get('match_status') != 'EXACT_PAIR_KICKOFF_COMPETITION' or
                row.get('timezone_status') != 'VERIFIED_SOURCE_TZ'):
            reason = 'PRODUCTION_GATE_UNMET'
        if reason:
            rejected.append({'provider': provider, 'reason': reason})
        else:
            accepted.append((row, allowed))
    fields = {}
    for field in FIELDS:
        values = {}
        for row, allowed in accepted:
            value = row.get(field)
            if field in allowed and type(value) is int and value >= 0:
                values.setdefault(value, {})[row['provider']] = row
        if not values:
            fields[field] = {'status': 'NO_QUALIFIED_FALLBACK', 'verification': 'MISSING',
                             'value': None, 'sources': []}
        elif len(values) > 1:
            fields[field] = {'status': 'CONFLICT', 'verification': 'CONFLICT', 'value': None,
                             'sources': sorted({s for group in values.values() for s in group})}
        else:
            value, rows = next(iter(values.items()))
            provider = primary if primary in rows else sorted(rows)[0]
            fields[field] = {'status': 'PRIMARY' if provider == primary else 'FALLBACK_SELECTED',
                             'verification': 'VERIFIED' if len(rows) >= 2 else 'SINGLE_SOURCE',
                             'value': value, 'provider': provider, 'sources': sorted(rows),
                             'raw_page_sha256': rows[provider]['raw_page_sha256'],
                             'observed_at_utc': rows[provider]['observed_at_utc']}
    return {'contract': 'QUALIFIED_RESULT_FALLBACK_V1', 'channel': channel,
            'match_id': match_id, 'primary': primary, 'fields': fields, 'rejected': rejected,
            'production_eligible': False, 'creates_consensus_vote': False}


def pilot_fallback_report(candidates, now=None):
    now = now or datetime.now(timezone.utc)
    ids = sorted({row['match_id'] for row in candidates})
    return {'contract': 'QUALIFIED_RESULT_FALLBACK_V1', 'at_utc': now.isoformat(),
            'channel': 'PILOT', 'matches': [select_result_fallback(
                'pilot:smart_soccer_stats', [r for r in candidates if r['match_id'] == mid],
                channel='PILOT', match_id=mid, now=now) for mid in ids],
            'production': {'status': 'NO_QUALIFIED_FALLBACK',
                           'reason': 'No separately promoted alternative to fifa-calendar-v3'},
            'production_eligible': False, 'creates_consensus_vote': False}
