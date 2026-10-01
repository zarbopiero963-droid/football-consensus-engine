"""Read native probabilities from hash-verified StatsUltra cards, never complements."""
import base64
from collections import Counter, defaultdict
import gzip
import hashlib
from html.parser import HTMLParser
import re

from app.identities import normalize_alias
from app.market_reference import utc
from app.market_vectors import make_vector

VERSION = 'statsultra-native-cards-v1'
MAX_RAW_BYTES = 10 * 1024 * 1024
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr'}


class Node:
    def __init__(self, tag='', classes=()):
        self.tag, self.classes, self.children = tag, set(classes), []

    def text(self):
        return ''.join(c.text() if isinstance(c, Node) else c for c in self.children).strip()

    def find(self, cls):
        found = [self] if cls in self.classes else []
        for child in self.children:
            if isinstance(child, Node):
                found.extend(child.find(cls))
        return found


class Cards(HTMLParser):
    # Only keep actual match-card subtrees; scripts, templates and unrelated
    # footer text cannot supply probabilities for a card.
    def __init__(self):
        super().__init__()
        self.cards, self.stack, self.ignored = [], [], []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get('class', '').split()
        if tag in ('script', 'style', 'template') or self.ignored:
            if tag not in VOID:
                self.ignored.append(tag)
            return
        if not self.stack and 'match-card' not in classes:
            return
        node = Node(tag, classes)
        if self.stack:
            self.stack[-1].children.append(node)
        else:
            self.cards.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.ignored:
            if self.ignored[-1] == tag:
                self.ignored.pop()
            return
        if self.stack and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and not self.ignored:
            self.stack[-1].children.append(data)


def published_cards(html):
    parser = Cards()
    parser.feed(html)
    if parser.stack or parser.ignored:
        raise ValueError('INCOMPLETE_HTML')
    rows = []
    for card in parser.cards:
        if len(card.find('match-card')) != 1:
            raise ValueError('NESTED_MATCH_CARD')
        names = card.find('match-card-long')
        if len(names) != 2 or any(not n.text() for n in names):
            continue
        values, errors = {}, []
        for stat in card.find('match-card-stat'):
            keys, numbers = stat.find('match-card-stat-key'), stat.find('match-card-stat-value')
            if len(keys) != 1:
                errors.append('STAT_KEY_AMBIGUOUS')
                continue
            # These are the labels actually observed in source HTML. No
            # assumed complementary label or 1X2 projection is supported.
            key = {'over 2.5': 'OVER', 'both score': 'GG'}.get(keys[0].text().casefold())
            if key is None:
                continue
            if key in values or len(numbers) != 1:
                errors.append('DUPLICATE_OR_AMBIGUOUS_STAT')
                continue
            raw = numbers[0].text()
            match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)%', raw)
            values[key] = {'published_text': raw,
                           'value': float(match[1]) if match else raw,
                           'error': None if match else 'INVALID_PUBLISHED_PERCENT'}
        rows.append({'home_team': names[0].text(), 'away_team': names[1].text(),
                     'values': values, 'errors': errors})
    return rows


def _raw(run):
    try:
        encoded = re.sub(r'\s+', '', run['html_gzip_base64'])
        compressed = base64.b64decode(encoded, validate=True)
        # Bound the expanded artifact, including malformed/truncated gzip.
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_RAW_BYTES + 1)
        if len(raw) > MAX_RAW_BYTES:
            raise ValueError('RAW_TOO_LARGE')
        if hashlib.sha256(raw).hexdigest() != run.get('html_sha256'):
            raise ValueError('RAW_HASH_MISMATCH')
        return raw.decode('utf-8')
    except (KeyError, TypeError, OSError, EOFError, UnicodeError) as exc:
        raise ValueError('RAW_UNREADABLE') from exc


def normalize(dataset, *, known_at):
    known = utc(known_at)
    if known is None:
        raise ValueError('EXPLICIT_KNOWLEDGE_TIME_REQUIRED')
    by_run = defaultdict(list)
    for row in dataset['rows']:
        by_run[row['run_id']].append(row)
    ids = [r['run_id'] for r in dataset['runs']]
    if len(set(ids)) != len(ids):
        raise ValueError('DUPLICATE_RUN')
    records, excluded = [], []
    for run in dataset['runs']:
        run_id = run['run_id']
        try:
            if run.get('source') != 'statsultra' or run.get('final_url') != 'https://statsultra.com/':
                raise ValueError('UNSUPPORTED_SOURCE_OR_URL')
            if run.get('status') != 'OK' or run.get('http_status') != 200:
                raise ValueError('RUN_NOT_OK')
            stamp = utc(run.get('finished_at_utc'))
            run_seal = utc(run.get('sealed_at_utc'))
            if stamp is None or stamp > known or (run_seal and run_seal > known):
                raise ValueError('CAPTURE_TIME_UNAVAILABLE')
            cards = published_cards(_raw(run))
        except ValueError as exc:
            excluded.append({'run_id': run_id, 'reason': str(exc)})
            continue
        pairs = defaultdict(list)
        for card in cards:
            pairs[(normalize_alias(card['home_team']), normalize_alias(card['away_team']))].append(card)
        seen = Counter((normalize_alias(r.get('home_team')), normalize_alias(r.get('away_team')))
                       for r in by_run[run_id])
        for row in by_run[run_id]:
            key = (normalize_alias(row.get('home_team')), normalize_alias(row.get('away_team')))
            candidates = pairs[key]
            row_id = run_id + ':' + row['source_match_key']
            row_seal = utc(row.get('sealed_at_utc'))
            if row_seal and row_seal > known:
                excluded.append({'row_id':row_id,'reason':'NOT_SEALED_AT_CUTOFF'})
                continue
            if row.get('source') != 'statsultra' or len(candidates) != 1 or seen[key] != 1:
                excluded.append({'row_id': row_id, 'reason': 'SOURCE_OR_CARD_AMBIGUOUS'})
                continue
            card = candidates[0]
            for market, source_key, stored_key in [('FT_OU_2.5', 'OVER', 'over25'),
                                                  ('FT_BTTS', 'GG', 'btts_yes')]:
                native = card['values'].get(source_key, {})
                value = native.get('value')
                vector = make_vector(market, period='FT', components={source_key: value},
                    raw_scale=100, source='statsultra', source_timestamp=stamp,
                    parse_status='BROKEN' if card['errors'] or native.get('error') else 'OK',
                    matching_status=row.get('match_status') or 'MATCH_UNRESOLVED',
                    kickoff_timezone_status=row.get('timezone_status') or 'TIMEZONE_UNRESOLVED',
                    origin={'run_id': run_id, 'source_match_key': row['source_match_key'],
                            'html_sha256': run['html_sha256'], 'parser_version': VERSION})
                opposite = 'under25' if source_key == 'OVER' else 'btts_no'
                # Legacy values are comparison diagnostics, never evidence of
                # publication. Old inferred Under is not imported or rewritten.
                records.append({'row_id': row_id, 'vector': vector,
                    'published_text': native.get('published_text'), 'card_errors': card['errors'],
                    'stored_value': row.get(stored_key), 'stored_opposite': row.get(opposite),
                    'stored_value_matches_raw': row.get(stored_key) == value,
                    'unpublished_opposite_in_storage': row.get(opposite) is not None,
                    'prematch_certified': False,
                    'sealed_at_utc':row_seal.isoformat() if row_seal else None,
                    'sealing_status':'SERVER_SEALED' if run_seal and row_seal else 'SERVER_SEAL_UNAVAILABLE',
                    'capture_time_basis': 'RUN_FINISHED_CONSERVATIVE',
                    'legacy_parse_status': row.get('parse_status')})
    missing_runs = set(by_run) - set(ids)
    excluded.extend({'run_id': r, 'reason': 'RUN_ARTIFACT_MISSING'} for r in sorted(missing_runs))
    return {'report_version': VERSION, 'mode': 'STAGING_READ_ONLY',
            'known_at': known.isoformat(), 'production_eligible': False,
            'prematch_certified': False, 'records': records, 'excluded': excluded,
            'status_counts': dict(Counter(r['vector']['vector_status'] for r in records)),
            'stored_mismatch_count': sum(not r['stored_value_matches_raw'] for r in records),
            'unpublished_opposite_count': sum(r['unpublished_opposite_in_storage'] for r in records)}
