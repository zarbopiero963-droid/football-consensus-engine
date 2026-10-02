"""Validate observed Pinnacle soccer FT 1X2 documents before price matching."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import math


CONTRACT = 'PINNACLE_FT1X2_EXPLICIT_PREMATCH_V1'
SIDES = ('home', 'draw', 'away')


def _id(value):
    return type(value) is int and value > 0


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        return None


def _decimal(value):
    if type(value) not in (int, float):
        raise ValueError('INVALID_PRICES')
    try:
        if not math.isfinite(value) or abs(value) < 100:
            raise ValueError('INVALID_PRICES')
        numeric = float(value)
        # Preserve the collector's frozen four-decimal convention; hardening
        # must not subtly change market probabilities or strategy edges.
        odds = round(1 + (numeric / 100.0 if numeric > 0 else 100.0 / -numeric), 4)
    except OverflowError:
        raise ValueError('INVALID_PRICES') from None
    if not math.isfinite(odds) or odds <= 1:
        raise ValueError('INVALID_PRICES')
    return odds


def parse_moneylines(matchups, markets, *, league_id, league_name, captured_at):
    """Return validated quotes and reason counts; ambiguity never selects a price.

    Source timestamps need explicit offsets. Raw documents and this audit are
    sealed by the caller; this function never writes or fetches data.
    """
    captured = _utc(captured_at)
    if not captured or not _id(league_id) or not isinstance(league_name, str) or not league_name.strip():
        raise ValueError('INVALID_CAPTURE_CONTEXT')
    rejected = Counter()
    audit = {'contract': CONTRACT, 'league_id': league_id,
             'captured_at_utc': captured.isoformat(), 'rejected': {}}
    if not isinstance(matchups, list) or not isinstance(markets, list):
        audit['rejected'] = {'INVALID_DOCUMENT_CONTAINER': 1}
        audit['accepted'] = 0
        return [], audit
    audit.update(matchup_rows=len(matchups), market_rows=len(markets))
    fixtures, candidates = defaultdict(list), defaultdict(list)
    for row in matchups:
        if not isinstance(row, dict) or not _id(row.get('id')):
            rejected['INVALID_MATCHUP_ROW'] += 1
        else:
            fixtures[row['id']].append(row)
    for row in markets:
        if not isinstance(row, dict):
            rejected['INVALID_MARKET_ROW'] += 1
        elif row.get('type') != 'moneyline':
            rejected['OTHER_MARKET'] += 1
        elif row.get('period') != 0 or row.get('isAlternate') is True:
            rejected['OTHER_PERIOD_OR_ALTERNATE'] += 1
        elif not _id(row.get('matchupId')):
            rejected['INVALID_MARKET_MATCHUP_ID'] += 1
        else:
            # Group before validating prices/key/status. A damaged duplicate
            # cannot disappear and leave a conveniently selected good row.
            candidates[row['matchupId']].append(row)
    quotes = []
    for mid, rows in candidates.items():
        try:
            if len(rows) != 1:
                raise ValueError('DUPLICATE_MONEYLINE')
            market = rows[0]
            if (type(market.get('period')) is not int or market['period'] != 0
                    or market.get('isAlternate') is not False or market.get('key') != 's;0;m'):
                raise ValueError('INVALID_MARKET_CONTRACT')
            if market.get('status') != 'open':
                raise ValueError('MARKET_NOT_OPEN')
            matches = fixtures.get(mid, [])
            if len(matches) != 1:
                raise ValueError('MISSING_OR_DUPLICATE_MATCHUP')
            fixture = matches[0]
            if (fixture.get('type') != 'matchup' or fixture.get('isLive') is not False
                    or fixture.get('units') != 'Regular' or fixture.get('status') != 'pending'
                    or fixture.get('parentId') is not None or fixture.get('parent') is not None):
                raise ValueError('MATCHUP_NOT_EXPLICIT_PREMATCH')
            league = fixture.get('league')
            sport = league.get('sport') if isinstance(league, dict) else None
            if (not isinstance(league, dict) or type(league.get('id')) is not int
                    or league['id'] != league_id or league.get('name') != league_name
                    or not isinstance(sport, dict) or type(sport.get('id')) is not int or sport['id'] != 29):
                raise ValueError('SOURCE_LEAGUE_OR_SPORT_MISMATCH')
            start, cutoff = _utc(fixture.get('startTime')), _utc(market.get('cutoffAt'))
            if not start or not cutoff or not captured < cutoff <= start or not captured < start:
                raise ValueError('INVALID_PREMATCH_TIME_OR_CUTOFF')
            parts = fixture.get('participants')
            if (not isinstance(parts, list) or len(parts) != 2
                    or any(not isinstance(p, dict) for p in parts)
                    or Counter(p.get('alignment') for p in parts if isinstance(p.get('alignment'), str))
                    != Counter(('home', 'away'))
                    or any(not isinstance(p.get('name'), str) or not p['name'].strip() for p in parts)):
                raise ValueError('INVALID_PARTICIPANTS')
            names = {p['alignment']: p['name'] for p in parts}
            prices = market.get('prices')
            if (not isinstance(prices, list) or len(prices) != 3
                    or any(not isinstance(p, dict) for p in prices)
                    or Counter(p.get('designation') for p in prices if isinstance(p.get('designation'), str))
                    != Counter(SIDES)):
                raise ValueError('INVALID_PRICES')
            values = {p['designation']: p.get('price') for p in prices}
            dec = [_decimal(values[side]) for side in SIDES]
            implied = [1 / price for price in dec]
            total = sum(implied)
            quotes.append({'matchup_id': mid, 'captured_at': captured,
                           'league_id': league_id, 'league': league_name,
                           'home': names['home'], 'away': names['away'], 'start': start,
                           'dec': dec, 'fair': [p / total for p in implied],
                           'overround': total, 'american': [values[s] for s in SIDES],
                           'market_version': market.get('version'), 'cutoff': market['cutoffAt']})
        except ValueError as exc:
            rejected[str(exc)] += len(rows)
    audit.update(accepted=len(quotes), rejected=dict(sorted(rejected.items())))
    return quotes, audit
