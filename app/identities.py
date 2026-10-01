"""Exact, contextual, point-in-time identities. No source promotion or fuzzy joins."""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import unicodedata
import uuid

from psycopg.types.json import Jsonb


def normalize_alias(value):
    # Preserve club prefixes, numbers, accents and punctuation: variants need an
    # explicit evidenced alias, rather than destructive canon() normalization.
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


@dataclass(frozen=True)
class Category:
    gender: str = 'UNKNOWN'
    age: str = 'UNKNOWN'
    squad: str = 'UNKNOWN'

    @property
    def key(self):
        return ':'.join((self.gender, self.age, self.squad))


def category_markers(name):
    """Lexical hints only. Absence of markers never proves senior/men/first."""
    n = normalize_alias(name)
    gender = 'WOMEN' if re.search(r'\b(women|woman|ladies|femenino|femminile|frauen)\b', n) else 'UNKNOWN'
    ages = set(re.findall(r'\bu[ -]?(\d{2})\b', n))
    age = 'U' + next(iter(ages)) if len(ages) == 1 else 'UNKNOWN'
    # B/II in the beginning or middle (B 93, B 1903) is not a reserve marker.
    match = re.search(r'\s+(b|ii|reserves?|reserve team)\s*$', n)
    squad = {'b': 'B', 'ii': 'II'}.get(match[1], 'RESERVE') if match else 'UNKNOWN'
    return Category(gender, age, squad)


def category_flags(name):
    markers = category_markers(name)
    flags = ['women'] if markers.gender == 'WOMEN' else []
    if markers.age != 'UNKNOWN' or markers.squad != 'UNKNOWN':
        flags.append('youth_or_b')
    return ','.join(flags)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('TIMEZONE_UNRESOLVED')
    return value.astimezone(timezone.utc)


def _context(kind, source, country, category):
    return (kind in ('TEAM', 'COMPETITION') and bool(source and source.strip()) and source != '*'
            and bool(re.fullmatch(r'[A-Z]{2,3}', country or ''))
            and bool(re.fullmatch(r'(MEN|WOMEN|MIXED):(SENIOR|U\d{2}):(FIRST|B|II|RESERVE)', category or '')))


RESOLVE_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (alias_id) * FROM canonical_alias_versions
    WHERE kind=%s AND source=%s AND normalized_alias=%s AND country_code=%s
      AND category=%s AND recorded_at<=%s
    ORDER BY alias_id,version DESC
)
SELECT a.entity_id,a.alias_id,a.version,e.canonical_name,e.external_ids
FROM latest a JOIN canonical_entities e USING(entity_id)
WHERE a.state='ACTIVE' AND a.valid_from<=%s
  AND (a.valid_until IS NULL OR %s<a.valid_until)
ORDER BY a.entity_id,a.alias_id
"""


def resolve(cur, name, *, kind, source, country, category, fixture_at, known_at):
    fixture_at, known_at = _utc(fixture_at), _utc(known_at)
    if not _context(kind, source, country, category) or not normalize_alias(name):
        return {'status': 'CONTEXT_UNRESOLVED', 'entity_id': None, 'aliases': []}
    cur.execute(RESOLVE_SQL, (kind, source, normalize_alias(name), country, category,
                              known_at, fixture_at, fixture_at))
    rows = cur.fetchall()
    ids = {str(r['entity_id']) for r in rows}
    if len(ids) != 1:
        return {'status': 'AMBIGUOUS' if ids else 'UNKNOWN', 'entity_id': None,
                'candidate_count': len(ids), 'aliases': []}
    return {'status': 'RESOLVED', 'entity_id': next(iter(ids)),
            'aliases': [{'alias_id': r['alias_id'], 'version': r['version']} for r in rows],
            'canonical_name': rows[0]['canonical_name'], 'external_ids': rows[0]['external_ids']}


def create_entity(cur, *, kind, name, country, category, provenance, external_ids=None, entity_id=None):
    if not _context(kind, 'registry', country, category) or not normalize_alias(name) or not provenance:
        raise ValueError('Explicit entity context and provenance are required')
    entity_id = str(uuid.UUID(str(entity_id))) if entity_id else str(uuid.uuid4())
    cur.execute('''INSERT INTO canonical_entities
        (entity_id,kind,canonical_name,country_code,category,provenance,external_ids)
        VALUES (%s,%s,%s,%s,%s,%s,%s)''',
        (entity_id, kind, name, country, category, Jsonb(provenance), Jsonb(external_ids or {})))
    return entity_id


def append_alias(cur, *, entity_id, kind, source, name, country, category,
                 valid_from, provenance, version=1, valid_until=None, state='ACTIVE'):
    if not _context(kind, source, country, category) or not normalize_alias(name) or not provenance:
        raise ValueError('Explicit alias context and provenance are required')
    valid_from = _utc(valid_from)
    valid_until = _utc(valid_until) if valid_until is not None else None
    if valid_until is not None and valid_until <= valid_from:
        raise ValueError('Invalid alias validity interval')
    entity_id = str(uuid.UUID(str(entity_id)))
    identity = [entity_id, kind, source, name, normalize_alias(name), country, category]
    alias_id = sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    cur.execute('''INSERT INTO canonical_alias_versions
        (alias_id,version,entity_id,kind,source,alias,normalized_alias,country_code,category,
         valid_from,valid_until,state,provenance)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING recorded_at''',
        (alias_id, version, entity_id, kind, source, name, normalize_alias(name), country,
         category, valid_from, valid_until, state, Jsonb(provenance)))
    return {'alias_id': alias_id, 'version': version, 'recorded_at': cur.fetchone()['recorded_at']}
