from datetime import datetime, timezone

import pytest

from app.identities import category_markers, normalize_alias, resolve
from prospective.collect import women_or_youth


@pytest.mark.parametrize('name', ['B 93 Copenhagen', 'B 1903 Copenhagen', 'II de Mayo', 'B', 'Migori Youth'])
def test_club_tokens_do_not_prove_reserve_or_age(name):
    assert women_or_youth(name) == ''
    assert category_markers(name).key == 'UNKNOWN:UNKNOWN:UNKNOWN'


@pytest.mark.parametrize('name,expected', [
    ('Schalke 04 II', 'UNKNOWN:UNKNOWN:II'), ('Barcelona B', 'UNKNOWN:UNKNOWN:B'),
    ('Italy U-21 Women', 'WOMEN:U21:UNKNOWN'), ('France U23', 'UNKNOWN:U23:UNKNOWN'),
    ('Chelsea Reserves', 'UNKNOWN:UNKNOWN:RESERVE'),
    ('France', 'UNKNOWN:UNKNOWN:UNKNOWN')])
def test_explicit_markers_are_separate_hints(name, expected):
    assert category_markers(name).key == expected


def test_alias_normalization_does_not_collapse_identity():
    assert normalize_alias('  FC\u00a0United ') == 'fc united'
    assert normalize_alias('FC United') != normalize_alias('United')
    assert normalize_alias('São Paulo') != normalize_alias('Sao Paulo')
    assert normalize_alias('Club II') != normalize_alias('Club')


def test_missing_context_never_queries_or_resolves():
    class Cursor:
        def execute(self, *args):
            pytest.fail('Missing context must fail before DB query')
    stamp = datetime.now(timezone.utc)
    args = dict(kind='TEAM', source='source', country='', category='MEN:SENIOR:FIRST',
                fixture_at=stamp, known_at=stamp)
    assert resolve(Cursor(), 'United', **args)['status'] == 'CONTEXT_UNRESOLVED'
    args.update(country='GB', category='UNKNOWN:UNKNOWN:UNKNOWN')
    assert resolve(Cursor(), 'United', **args)['status'] == 'CONTEXT_UNRESOLVED'


def test_naive_time_rejected_even_with_exact_name():
    stamp = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match='TIMEZONE_UNRESOLVED'):
        resolve(None, 'United', kind='TEAM', source='source', country='GB',
                category='MEN:SENIOR:FIRST', fixture_at=stamp.replace(tzinfo=None), known_at=stamp)
