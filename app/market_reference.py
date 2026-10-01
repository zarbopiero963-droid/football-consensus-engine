"""Read-only observed Pinnacle references; never reconstruct missing prices."""
from datetime import datetime, timezone
import math
import re

from app.db import connect
from app.identities import normalize_alias

MATCH_CONTRACT = 'EXACT_PAIR_TIME_CONTEXT_V1'


def utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def market_match_rejection(model, quote, captured_at):
    """First failed gate in EXACT_PAIR_TIME_CONTEXT_V1; None means accepted."""
    kickoff, start, captured = utc(model.get('kickoff')), utc(quote.get('start')), utc(captured_at)
    if not all((kickoff, start, captured)):
        return 'TIMEZONE_OR_TIME_MISSING'
    if captured >= min(kickoff, start):
        return 'CAPTURE_NOT_PREMATCH'
    if abs((kickoff - start).total_seconds()) > 15 * 60:
        return 'KICKOFF_MISMATCH'
    for side in ('home', 'away'):
        name = normalize_alias(model.get(side))
        if not name or name != normalize_alias(quote.get(side)):
            return 'PAIR_MISMATCH'
    # A competition is required. Missing country/category is still missing;
    # any metadata supplied by either adapter must agree on both sides.
    if not normalize_alias(model.get('league')) or not normalize_alias(quote.get('league')):
        return 'COMPETITION_MISSING'
    for field in ('league', 'country', 'category'):
        left, right = normalize_alias(model.get(field)), normalize_alias(quote.get(field))
        if (left or right) and left != right:
            return field.upper() + '_MISMATCH'
    return None


def exact_market_match(model, quote, captured_at):
    """Exact source names only until evidenced cross-source aliases are bound."""
    return market_match_rejection(model, quote, captured_at) is None


def audit_market_candidates(model, quotes):
    """Explain exact ordered-name candidates; never propose aliases or prices.

    Counts refer to quote candidates, not distinct fixtures. Each rejection is
    the first failed gate in contract order; unrelated/reversed pairs are not
    evidence of a missing competition alias.
    """
    pair = tuple(normalize_alias(model.get(side)) for side in ('home', 'away'))
    candidates, rejected, matched = [], {}, 0
    for quote in quotes:
        if not all(pair) or pair != tuple(normalize_alias(quote.get(side)) for side in ('home', 'away')):
            continue
        reason = market_match_rejection(model, quote, quote.get('captured_at'))
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
        else:
            matched += 1
        candidates.append({
            'provider_match_id': quote.get('matchup_id'),
            'league': quote.get('league'), 'country': quote.get('country'),
            'category': quote.get('category'),
            'kickoff_utc': utc(quote.get('start')).isoformat() if utc(quote.get('start')) else None,
            'captured_at_utc': utc(quote.get('captured_at')).isoformat() if utc(quote.get('captured_at')) else None,
            'raw_file': quote.get('raw_file'), 'transport': quote.get('transport'),
            'first_rejection': reason,
        })
    status = ('UNIQUE_EXACT_MATCH' if matched == 1 else 'AMBIGUOUS_EXACT_MATCH' if matched > 1
              else 'EXACT_PAIR_REJECTED' if candidates else 'NO_EXACT_PAIR' if quotes else 'NO_MARKET_QUOTES')
    return {'contract': MATCH_CONTRACT, 'status': status,
            'exact_pair_candidates': len(candidates), 'matched_candidates': matched,
            'candidate_first_rejections': rejected, 'candidates': candidates}


def _quote(row, known_at):
    p = row['payload']
    kickoff = utc(row.get('kickoff_utc'))
    pin_kickoff = utc(p.get('pin_kickoff_utc'))
    model_at = utc(row.get('snapshot_timestamp_utc'))
    captured = utc(p.get('pin_snapshot_timestamp_utc'))
    persisted = utc(row.get('imported_at'))
    if not all((kickoff, pin_kickoff, model_at, captured, persisted)):
        return None, 'TIMEZONE_OR_AUDIT_MISSING'
    if captured > known_at or model_at > known_at or persisted > known_at:
        return None, 'NOT_KNOWN_AT'
    if max(model_at, captured, persisted) >= min(kickoff, pin_kickoff):
        return None, 'NOT_SEALED_PRE_KICKOFF'
    if captured < model_at or persisted < captured:
        return None, 'INVALID_AUDIT_ORDER'
    if row.get('valid_prematch') is not True:
        return None, 'INVALID_PREMATCH'
    if abs((kickoff - pin_kickoff).total_seconds()) > 15 * 60:
        return None, 'KICKOFF_MISMATCH'
    for side in ('home', 'away'):
        name = normalize_alias(row.get(side + '_team'))
        if not name or name != normalize_alias(p.get('pin_' + side)):
            return None, 'PAIR_MISMATCH'
    if not p.get('pin_matchup_id') or not re.fullmatch(r'[a-f0-9]{64}', p.get('sha256_odds_raw', '')):
        return None, 'PROVENANCE_MISSING'
    try:
        odds = [float(p.get('odds_' + s)) for s in ('home', 'draw', 'away')]
    except (ValueError, TypeError):
        return None, 'INVALID_ODDS'
    if not all(math.isfinite(o) and o > 1 for o in odds):
        return None, 'INVALID_ODDS'
    inverse = [1 / o for o in odds]
    overround = sum(inverse)
    # Explicit market derivation, never a source/model probability.
    fair = [v / overround for v in inverse]
    certified = p.get('name_score_note') == MATCH_CONTRACT
    if certified and not exact_market_match(
            {'home': row['home_team'], 'away': row['away_team'], 'kickoff': kickoff,
             'league': row['league'], 'country': p.get('country')},
            {'home': p['pin_home'], 'away': p['pin_away'], 'start': pin_kickoff,
             'league': p.get('pin_league')}, captured):
        return None, 'CONTEXT_MISMATCH'
    return {
        'match_id': row['match_id'], 'kickoff_utc': kickoff.isoformat(),
        'provider': 'pinnacle', 'provider_match_id': str(p['pin_matchup_id']),
        'market': 'FT_1X2', 'captured_at_utc': captured.isoformat(),
        'persisted_at_utc': persisted.isoformat(),
        'model_snapshot_at_utc': model_at.isoformat(),
        'odds': dict(zip(('1', 'X', '2'), odds)),
        'fair': dict(zip(('1', 'X', '2'), fair)), 'overround': overround,
        'fair_method': 'PROPORTIONAL_INVERSE_ODDS',
        'market_version': p.get('market_version'), 'raw_sha256': p['sha256_odds_raw'],
        'matching': MATCH_CONTRACT if certified else 'LEGACY_UNCERTIFIED',
        'seconds_before_kickoff': (min(kickoff, pin_kickoff) - captured).total_seconds(),
    }, None


def select_references(rows, *, known_at):
    known_at = utc(known_at)
    if known_at is None:
        raise ValueError('TIMEZONE_UNRESOLVED')
    quotes, rejected = [], {}
    for row in rows:
        quote, reason = _quote(row, known_at)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
        else:
            quotes.append(quote)
    # Never blend separate provider fixture IDs or kickoff revisions.
    identities = {(q['provider_match_id'], q['kickoff_utc']) for q in quotes}
    result = {'role': 'MARKET_BENCHMARK', 'known_at_utc': known_at.isoformat(),
              'q_open': None, 'q_close': None, 'latest_pre_kickoff': None,
              'legacy_diagnostic': None, 'rejected': rejected, 'status': 'MISSING',
              'open_definition': 'FIRST_OBSERVED_PRE_KICKOFF',
              'close_definition': 'LATEST_OBSERVED_PRE_KICKOFF_PROXY'}
    if len(identities) > 1:
        result['status'] = 'AMBIGUOUS_SERIES'
        return result
    certified = [q for q in quotes if q['matching'] == MATCH_CONTRACT]
    legacy = [q for q in quotes if q['matching'] != MATCH_CONTRACT]
    for series in (certified, legacy):
        series.sort(key=lambda q: (q['captured_at_utc'], q['model_snapshot_at_utc']))
        # Same capture time with different prices cannot be ordered safely.
        seen = {}
        for q in series:
            key = q['captured_at_utc']
            signature = (q['raw_sha256'], tuple(q['odds'].values()))
            if key in seen and seen[key] != signature:
                result['status'] = 'AMBIGUOUS_SERIES'
                return result
            seen[key] = signature
    if legacy:
        result['legacy_diagnostic'] = {'first_observed': legacy[0], 'latest_observed': legacy[-1],
                                       'production_eligible': False}
        result['status'] = 'LEGACY_UNCERTIFIED'
    if certified:
        result['q_open'] = certified[0]
        result['latest_pre_kickoff'] = certified[-1]
        if known_at >= utc(certified[-1]['kickoff_utc']):
            result['q_close'] = certified[-1]
            result['status'] = 'CLOSED_OBSERVED_PROXY'
        else:
            result['status'] = 'PROVISIONAL'
    return result


def get_market_reference(match_id, *, known_at=None):
    known_at = known_at or datetime.now(timezone.utc)
    with connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT match_id,kickoff_utc,snapshot_timestamp_utc,home_team,
                              away_team,league,valid_prematch,imported_at,payload
                       FROM model_snapshots WHERE match_id=%s
                       ORDER BY snapshot_timestamp_utc''', (match_id,))
        return {'match_id': match_id, **select_references(cur.fetchall(), known_at=known_at)}
