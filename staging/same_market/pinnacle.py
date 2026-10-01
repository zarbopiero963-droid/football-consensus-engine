"""Observed Pinnacle FT goal totals. No ingestion, promotion or market proxy."""
import base64
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import zlib

from app.market_reference import exact_market_match, utc

VERSION = 'pinnacle-same-market-v1'
MAX_BYTES = 5 * 1024 * 1024


def _id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _document(document, league_id, kind):
    suffix = 'matchups?brandId=0' if kind == 'matchups' else 'markets/straight'
    url = f'https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/{suffix}'
    if (document.get('requested_url') != url or document.get('final_url') != url
            or document.get('http_status') != 200 or document.get('truncated') is not False
            or document.get('raw_format') not in ('DOM_BODY_INNER_TEXT_JSON_UTF8', 'HTTP_RESPONSE_JSON_UTF8')):
        raise ValueError('DOCUMENT_PROVENANCE_INVALID')
    try:
        compressed = base64.b64decode(document['raw_gzip_base64'], validate=True)
        if len(compressed) > MAX_BYTES:
            raise ValueError
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(compressed, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError
        if document['bytes_utf8'] != len(raw) or document['sha256'] != hashlib.sha256(raw).hexdigest():
            raise ValueError
        rows = json.loads(raw.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError, zlib.error):
        raise ValueError('DOCUMENT_RAW_INVALID') from None
    return rows


def _line(value):
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('INVALID_LINE') from None
    return result


def _decimal(price):
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        raise ValueError('INVALID_PRICE')
    try:
        valid = math.isfinite(price) and abs(price) >= 100
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('INVALID_PRICE')
    return 1 + (price / 100 if price > 0 else 100 / -price)


def normalize(bundle, *, known_at):
    """Replay a full source capture; all output remains staging and unsealed."""
    captured, known = utc(bundle.get('captured_at_utc')), utc(known_at)
    lid = bundle.get('league_id')
    if not _id(lid) or not captured or not known or captured > known:
        raise ValueError('CAPTURE_OR_KNOWLEDGE_INVALID')
    if bundle.get('session_stopped') is not True:
        raise ValueError('STOP_UNCONFIRMED')
    docs = bundle['documents']
    matchups = _document(docs['matchups'], lid, 'matchups')
    markets = _document(docs['markets'], lid, 'markets')
    rejected = Counter()
    by_id = defaultdict(list)
    for matchup in matchups:
        if _id(matchup.get('id')):
            by_id[matchup['id']].append(matchup)
    candidates = defaultdict(list)
    for market in markets:
        try:
            if market.get('type') != 'total' or type(market.get('period')) is not int or market['period'] != 0:
                raise ValueError('UNSUPPORTED_MARKET_OR_PERIOD')
            if market.get('status') != 'open' or type(market.get('isAlternate')) is not bool:
                raise ValueError('MARKET_NOT_OPEN_OR_UNAUDITED')
            key = re.fullmatch(r's;0;ou;([0-9]+(?:\.[0-9]+)?)', str(market.get('key', '')))
            if key is None:
                raise ValueError('MARKET_KEY_INVALID')
            line = _line(key[1])
            if line % 1 != Decimal('0.5'):
                raise ValueError('PUSH_SPLIT_LINE_UNSUPPORTED')
            if not _id(market.get('matchupId')):
                raise ValueError('MATCHUP_ID_INVALID')
            prices = market.get('prices')
            if (not isinstance(prices, list) or len(prices) != 2
                    or not all(isinstance(p, dict) for p in prices)
                    or {p.get('designation') for p in prices} != {'over', 'under'}):
                raise ValueError('INCOMPLETE_OR_DUPLICATE_PRICES')
            if any(_line(p.get('points')) != line for p in prices):
                raise ValueError('SELECTION_LINE_MISMATCH')
            odds = {p['designation'].upper(): _decimal(p.get('price')) for p in prices}
            candidates[(market['matchupId'], line)].append((market, odds))
        except ValueError as exc:
            rejected[str(exc)] += 1
    quotes = []
    for (mid, line), entries in candidates.items():
        if len(entries) != 1:
            rejected['AMBIGUOUS_MARKET_SERIES'] += len(entries)
            continue
        source = by_id[mid]
        if len(source) != 1:
            rejected['MISSING_OR_DUPLICATE_MATCHUP'] += 1
            continue
        mu = source[0]
        market, odds = entries[0]
        league = mu.get('league') or {}
        if not isinstance(league, dict) or not isinstance(league.get('sport'), dict):
            rejected['UNSAFE_FIXTURE'] += 1
            continue
        start, cutoff = utc(mu.get('startTime')), utc(market.get('cutoffAt'))
        participants = mu.get('participants') or []
        if not isinstance(participants, list):
            rejected['UNSAFE_FIXTURE'] += 1
            continue
        pair = {side: [p.get('name') for p in participants if isinstance(p, dict) and p.get('alignment') == side]
                for side in ('home', 'away')}
        if (mu.get('type') != 'matchup' or mu.get('isLive') is not False
                or mu.get('parentId') or mu.get('parent') or mu.get('units') != 'Regular'
                or mu.get('status') != 'pending' or league.get('id') != lid
                or (league.get('sport') or {}).get('id') != 29
                or not isinstance(league.get('name'), str) or not league['name'].strip()
                or any(len(names) != 1 or not isinstance(names[0], str) or not names[0].strip() for names in pair.values())):
            rejected['UNSAFE_FIXTURE'] += 1
            continue
        if not start or not cutoff or captured >= min(start, cutoff) or cutoff > start:
            rejected['NOT_PREMATCH_OR_TIME_UNRESOLVED'] += 1
            continue
        inverse = {side: 1 / price for side, price in odds.items()}
        overround = sum(inverse.values())
        quotes.append({
            'provider': 'pinnacle', 'provider_match_id': str(mid), 'league_id': lid,
            'market_id': f'FT_OU_{format(line.normalize(), "f")}', 'period': 'FT',
            'line': format(line.normalize(), 'f'), 'is_alternate': market['isAlternate'],
            'home': pair['home'][0], 'away': pair['away'][0], 'league': league['name'],
            'country': None, 'category': None, 'start': start.isoformat(),
            'cutoff_at_utc': cutoff.isoformat(), 'captured_at_utc': captured.isoformat(),
            'odds': odds, 'american_prices': {p['designation'].upper(): p['price'] for p in market['prices']},
            'fair': {side: value / overround for side, value in inverse.items()},
            'overround': overround, 'fair_method': 'PROPORTIONAL_INVERSE_ODDS',
            'market_version': market.get('version'), 'role': 'MARKET_BENCHMARK',
            'matching': 'PROVIDER_ID_ONLY', 'raw_sha256': {kind: docs[kind]['sha256'] for kind in docs},
            'raw_format': {kind: docs[kind]['raw_format'] for kind in docs},
            'server_sealed': False, 'prematch_certified': False,
            'production_eligible': False, 'adds_source_vote': False,
        })
    quotes.sort(key=lambda q: (q['provider_match_id'], Decimal(q['line'])))
    return {'contract': VERSION, 'role': 'MARKET_BENCHMARK', 'lifecycle': 'STAGING',
            'known_at_utc': known.isoformat(), 'captured_at_utc': captured.isoformat(),
            'quotes': quotes, 'quote_count': len(quotes), 'rejected': dict(sorted(rejected.items())),
            'btts_status': 'NOT_OBSERVED_NO_ADAPTER', 'production_eligible': False,
            'adds_source_vote': False, 'q_open': None, 'q_close': None}


def select_same_market(vector, fixture, quotes, *, known_at):
    """Exact-market diagnostic join. FULL alone never certifies the fixture."""
    if vector.get('vector_status') != 'FULL':
        return {'status': 'MODEL_VECTOR_NOT_FULL', 'quote': None}
    if vector.get('kickoff_timezone_status') not in ('EXPLICIT_OFFSET', 'VERIFIED_SOURCE_TZ'):
        return {'status': 'TIMEZONE_UNRESOLVED', 'quote': None}
    if vector.get('matching_status') not in ('EXACT_PAIR_KICKOFF_COMPETITION', 'EXACT_PAIR_TIME_CONTEXT_V1'):
        return {'status': 'MATCH_UNRESOLVED', 'quote': None}
    model_at, kickoff = utc(vector.get('source_timestamp_utc')), utc(fixture.get('kickoff'))
    known = utc(known_at)
    if not known or not model_at or not kickoff or model_at >= kickoff or model_at > known:
        return {'status': 'MODEL_NOT_PREMATCH', 'quote': None}
    candidates = [q for q in quotes if q.get('provider') == 'pinnacle' and q.get('role') == 'MARKET_BENCHMARK'
                  and utc(q.get('captured_at_utc')) is not None and utc(q['captured_at_utc']) <= known
                  and q.get('market_id') == vector.get('market_id')
                  and q.get('period') == vector.get('period') and q.get('line') == vector.get('line')
                  and exact_market_match(fixture, q, q.get('captured_at_utc'))]
    if len(candidates) != 1:
        return {'status': 'AMBIGUOUS' if candidates else 'NO_SAME_MARKET_QUALIFIED_MATCH', 'quote': None}
    return {'status': 'STAGING_EXACT_MATCH_UNSEALED', 'quote': candidates[0],
            'production_eligible': False, 'adds_source_vote': False}
