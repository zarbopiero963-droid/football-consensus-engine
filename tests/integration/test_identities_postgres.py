"""Registry tests use a real isolated PostgreSQL schema; fixture names are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.identities import append_alias, create_entity, resolve
from app.migrations import apply_migrations
from test_decision_postgres import database


CATEGORY = 'MEN:SENIOR:FIRST'
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
PROVENANCE = {'type': 'SYNTHETIC_TEST', 'evidence': 'registry regression'}


def seed(cur, country='GB', category=CATEGORY, kind='TEAM', name='United', source='test'):
    entity_id = create_entity(cur, kind=kind, name=name, country=country,
                              category=category, provenance=PROVENANCE,
                              external_ids={'test-only': 'external-id'})
    alias = append_alias(cur, entity_id=entity_id, kind=kind, source=source, name=name,
                         country=country, category=category, valid_from=START, provenance=PROVENANCE)
    return entity_id, alias


def lookup(cur, name='United', **overrides):
    return resolve(cur, name, **dict(dict(kind='TEAM', source='test', country='GB',
                   category=CATEGORY, fixture_at=START, known_at=datetime.now(timezone.utc)), **overrides))


def test_exact_context_and_external_metadata_without_fuzzy(database):
    with database['connect']() as conn, conn.cursor() as cur:
        entity, alias = seed(cur)
        other, _ = seed(cur, country='US')
        women, _ = seed(cur, category='WOMEN:SENIOR:FIRST')
        competition, _ = seed(cur, kind='COMPETITION')
        result = lookup(cur, name=' UNITED ')
        assert result['entity_id'] == entity and result['status'] == 'RESOLVED'
        assert result['aliases'] == [{'alias_id': alias['alias_id'], 'version': 1}]
        assert result['external_ids'] == {'test-only': 'external-id'}
        assert lookup(cur, country='US')['entity_id'] == other
        assert lookup(cur, category='WOMEN:SENIOR:FIRST')['entity_id'] == women
        assert lookup(cur, kind='COMPETITION')['entity_id'] == competition
        assert lookup(cur, source='unregistered')['status'] == 'UNKNOWN'
        assert lookup(cur, name='FC United')['status'] == 'UNKNOWN'
        assert lookup(cur, name='Unit')['status'] == 'UNKNOWN'
        assert lookup(cur, country='')['status'] == 'CONTEXT_UNRESOLVED'


def test_ambiguity_rejected_and_duplicate_aliases_do_not_add_votes(database):
    with database['connect']() as conn, conn.cursor() as cur:
        entity, _ = seed(cur)
        append_alias(cur, entity_id=entity, kind='TEAM', source='test', name=' UNITED ',
                     country='GB', category=CATEGORY, valid_from=START, provenance=PROVENANCE)
        assert lookup(cur)['entity_id'] == entity
        seed(cur)
        result = lookup(cur)
        assert result['status'] == 'AMBIGUOUS' and result['entity_id'] is None
        assert result['candidate_count'] == 2


def test_versioned_retraction_preserves_old_knowledge_and_validity(database):
    with database['connect']() as conn, conn.cursor() as cur:
        entity, first = seed(cur)
        before = datetime.now(timezone.utc)
        args = dict(entity_id=entity, kind='TEAM', source='test', name='United',
                    country='GB', category=CATEGORY, valid_from=START, provenance=PROVENANCE)
        append_alias(cur, **args, version=2, state='RETRACTED')
        assert lookup(cur)['status'] == 'UNKNOWN'
        assert lookup(cur, known_at=before)['entity_id'] == entity
        assert lookup(cur, known_at=first['recorded_at']-timedelta(microseconds=1))['status'] == 'UNKNOWN'
        end = START + timedelta(days=1)
        append_alias(cur, **args, version=3, valid_until=end)
        assert lookup(cur)['entity_id'] == entity
        assert lookup(cur, fixture_at=end)['status'] == 'UNKNOWN'
        assert lookup(cur, fixture_at=START-timedelta(seconds=1))['status'] == 'UNKNOWN'
        assert conn.execute('SELECT count(*) AS n FROM canonical_alias_versions').fetchone()['n'] == 3


def test_database_enforces_immutability_and_revision_identity(database):
    with database['connect']() as conn, conn.cursor() as cur:
        entity, first = seed(cur)
        args = dict(entity_id=entity, kind='TEAM', source='test', name='United',
                    country='GB', category=CATEGORY, valid_from=START, provenance=PROVENANCE)
        for table in ['canonical_entities', 'canonical_alias_versions']:
            # Include FK dependents so the TRUNCATE trigger, rather than an
            # earlier PostgreSQL foreign-key refusal, is actually exercised.
            for sql in [f'DELETE FROM {table}', f'TRUNCATE {table} CASCADE',
                        f'UPDATE {table} SET recorded_at=NOW()']:
                with pytest.raises(psycopg.errors.RaiseException, match='append-only'):
                    with conn.transaction():
                        cur.execute(sql)
        with pytest.raises(psycopg.errors.RaiseException, match='sequence'):
            with conn.transaction():
                append_alias(cur, **args, version=3)
        with pytest.raises(psycopg.errors.RaiseException, match='identity'):
            with conn.transaction():
                cur.execute('''INSERT INTO canonical_alias_versions SELECT alias_id,2,entity_id,
                    kind,'other',alias,normalized_alias,country_code,category,valid_from,
                    valid_until,state,provenance,recorded_at FROM canonical_alias_versions''')
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                append_alias(cur, **{**args, 'country': 'US'})
        assert lookup(cur)['entity_id'] == entity


def test_concurrent_same_revision_one_commit_and_replay_migration_preserves_records(database):
    with database['connect']() as conn, conn.cursor() as cur:
        entity, _ = seed(cur)
    def revise(_):
        try:
            with database['connect']() as conn, conn.cursor() as cur:
                append_alias(cur, entity_id=entity, kind='TEAM', source='test', name='United',
                             country='GB', category=CATEGORY, valid_from=START,
                             provenance=PROVENANCE, version=2)
            return 'committed'
        except psycopg.errors.RaiseException:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(revise, range(4)))
    assert results.count('committed') == 1 and results.count('rejected') == 3
    apply_migrations(database['connect'])
    with database['connect']() as conn:
        assert conn.execute('SELECT count(*) AS n FROM canonical_entities').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) AS n FROM canonical_alias_versions').fetchone()['n'] == 2
