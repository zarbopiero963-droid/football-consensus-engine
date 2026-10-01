import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from staging.result_sources import pilot48 as pilot
from staging.result_sources.worldfootball import parse_document, parse_html

EVIDENCE = Path(__file__).resolve().parents[1] / 'staging/result_sources/evidence'
UTC = timezone.utc
NOW = datetime(2026, 10, 1, 21, 45, tzinfo=UTC)


def world_html():
    data = json.loads((EVIDENCE / 'worldfootball_20261001.json').read_text())
    html = gzip.decompress(base64.b64decode(data['fragment_gzip_base64']))
    assert len(html) == data['fragment_bytes']
    assert hashlib.sha256(html).hexdigest() == data['fragment_sha256']
    return html.decode()


def smart_rows():
    data = json.loads((EVIDENCE / 'smart_20261001.json').read_text())
    text = data['visible_text']
    assert hashlib.sha256(text.encode()).hexdigest() == data['visible_text_sha256']
    return pilot.parse_smart_soccer_stats_results(text)


def anchor(row):
    return {'match_id': 'controlled-anchor', 'home_team': row['home_team'],
            'away_team': row['away_team'], 'league': pilot.competition_name(row),
            'kickoff_utc': row.get('source_kickoff_utc') or datetime(2026, 9, 30, 16, tzinfo=UTC)}


def test_live_worldfootball_replay_published_lifecycle_and_utc_without_inferred_fields():
    parsed = parse_document(world_html())
    assert parsed['audit']['cards_seen'] == 380
    assert parsed['audit']['rows_accepted'] == 50
    assert parsed['audit']['rejection_counts'] == {'NOT_FINISHED': 330}
    row = parsed['rows'][0]
    assert (row['home_team'], row['away_team']) == ('Inter', 'AC Monza')
    assert (row['final_home_goals'], row['final_away_goals']) == (4, 1)
    assert row['source_match_id'] == '12130828'
    assert row['source_kickoff_utc'] == datetime(2026, 8, 22, 16, 30, tzinfo=UTC)
    assert row['source_end_utc'] == datetime(2026, 8, 22, 18, 24, 2, tzinfo=UTC)
    assert row['competition'] == 'Serie A' and row['source_gender'] == 'male'
    assert row['timezone_status'] == 'EXPLICIT_OFFSET' and not row['production_eligible']
    assert any(r['final_away_goals'] == 0 for r in parsed['rows'])
    assert all(r[f] is None for r in parsed['rows'] for f in ('ht_home_goals', 'corners_home', 'ht_corners_home'))


def test_published_smart_rows_require_literal_competition_and_never_supply_source_utc():
    rows = smart_rows()
    assert len(rows) == 20
    row = next(r for r in rows if r['home_team'] == 'Lithuania')
    assert row['final_away_goals'] == row['ht_away_goals'] == 0
    a = anchor(row)
    assert pilot.match_result_anchor(row, [a])[1] == 'EXACT_PAIR_DATE_COMPETITION_DIAGNOSTIC'
    assert row.get('source_kickoff_utc') is None
    assert pilot.match_result_anchor(row, [{**a, 'league': 'UEFA Nations League'}]) == (None, 'COMPETITION_MISMATCH')
    assert pilot.match_result_anchor(row, [{**a, 'league': 'International Friendlies'}]) == (None, 'COMPETITION_MISMATCH')


@pytest.mark.parametrize('old,new,reason', [
    ('class="first odd finished match match-1"', 'class="first odd live match match-1"', 'NOT_FINISHED'),
    ('>Ended</div>', '>HT</div>', 'NOT_FINISHED'),
    ('data-datetime="2026-08-22T16:30:00Z"', 'data-datetime="2026-08-22T16:30:00"', 'TIME_UNRESOLVED_OR_INVALID_END'),
    ('data-datetime_end="2026-08-22T18:24:02Z"', 'data-datetime_end="2026-08-22T15:00:00Z"', 'TIME_UNRESOLVED_OR_INVALID_END'),
    ('data-match_id="12130828"', 'data-match_id=""', 'INVALID_ID_PAIR_OR_SCORE'),
    ('>4:1</a>', '>09:00</a>', None),  # Negative lifecycle control below still required.
])
def test_mutated_real_card_cannot_borrow_evidence_from_neighbours(old, new, reason):
    html = world_html()
    assert old in html
    parsed = parse_document(html.replace(old, new, 1))
    if reason is not None:
        assert len(parsed['rows']) == 49
        assert not any(r['source_match_id'] == '12130828' for r in parsed['rows'])
        assert parsed['audit']['rejection_counts'][reason] >= 1
    else:
        # A score's size is not a lifecycle rule. A scheduled clock is rejected
        # by status, even when its components look like a possible score.
        html = html.replace(old, new, 1).replace('first odd finished match match-1', 'first odd scheduled match match-1', 1)
        assert len(parse_html(html)) == 49


def test_plaintext_clock_or_score_alone_cannot_prove_ft():
    for text in ('10.10.2026\nFuture Home\n09:00\nFuture Away', '22.08.2026\nInter\n4:1\nAC Monza'):
        assert pilot.parse_worldfootball_results(text) == []


@pytest.mark.parametrize('change,reason', [
    ({'league': 'Other league'}, 'COMPETITION_MISMATCH'),
    ({'league': ''}, 'ANCHOR_COMPETITION_UNRESOLVED'),
    ({'home_team': 'Inter FC'}, 'NOT_FOUND'),
    ({'home_team': 'Ínter'}, 'NOT_FOUND'),
    ({'home_team': 'AC Monza', 'away_team': 'Inter'}, 'NOT_FOUND'),
    ({'kickoff_utc': datetime(2026, 8, 22, 16, 46, tzinfo=UTC)}, 'KICKOFF_MISMATCH'),
    ({'kickoff_utc': datetime(2026, 8, 22, 16, 30)}, 'ANCHOR_TIME_UNRESOLVED'),
])
def test_exact_context_rejects_legacy_canon_and_time_shortcuts(change, reason):
    row = parse_html(world_html())[0]
    assert pilot.match_result_anchor(row, [{**anchor(row), **change}]) == (None, reason)


def test_native_source_utc_crosses_rome_midnight_without_date_inference():
    row = deepcopy(parse_html(world_html())[0])
    row['source_kickoff_utc'] = datetime(2026, 9, 30, 23, tzinfo=UTC)
    row['source_end_utc'] = datetime(2026, 10, 1, 1, tzinfo=UTC)
    row['source_date'] = row['source_kickoff_utc'].date()
    a = anchor(row)
    assert a['kickoff_utc'].astimezone(pilot.ROME).date() != row['source_date']
    assert pilot.result_match(row, [a], NOW)[1] == 'EXACT_PAIR_KICKOFF_COMPETITION_DIAGNOSTIC'
    assert pilot.result_match(row, [a, {**a, 'match_id': 'ambiguous'}], NOW) == (None, 'AMBIGUOUS')
    assert pilot.result_match(row, [a], row['source_end_utc'] - timedelta(seconds=1)) == (None, 'END_NOT_OBSERVED')


@pytest.mark.parametrize('field,value,reason', [
    ('status', 'LIVE', 'NOT_FINISHED'), ('final_away_goals', None, 'INVALID_FT_SCORE'),
    ('final_home_goals', -1, 'INVALID_FT_SCORE'), ('final_home_goals', True, 'INVALID_FT_SCORE'),
    ('source_end_utc', NOW + timedelta(seconds=1), 'END_NOT_OBSERVED'),
])
def test_bad_lifecycle_score_and_future_final_never_reach_pilot_observations(field, value, reason):
    row = {**parse_html(world_html())[0], field: value}
    assert pilot.result_match(row, [anchor(row)], NOW) == (None, reason)


def test_missing_competition_and_date_are_explicit_rejections():
    row = smart_rows()[0]
    assert pilot.match_result_anchor({**row, 'competition': ''}, []) == (None, 'COMPETITION_UNRESOLVED')
    assert pilot.match_result_anchor({**row, 'source_date': None}, []) == (None, 'DATE_UNRESOLVED')


def controlled_card(sid='1', score='0:2', status='finished'):
    # Explicitly synthetic, same structure as the captured publisher cards.
    return f'''<div class="match {status}" data-match_id="{sid}" data-competition_id="111"
       data-gender="male" data-datetime="2026-09-30T16:00:00Z" data-datetime_end="2026-09-30T18:00:00Z">
       <div class="team-name-home">Home</div><div class="team-name-away">Away</div>
       <div class="match-result">{score}</div><div class="match-status">Ended</div></div>'''


def controlled_document(cards):
    return '''<h2 class="item-competition" data-gender="male"><div class="competition-name_extended">Serie A</div></h2>
       <div class="module-gameplan"><div class="round-head" data-competition_id="111"></div>''' + cards + '</div>'


def test_duplicate_conflicting_id_and_ambiguous_fixture_are_fail_closed():
    card = controlled_card()
    p = parse_document(controlled_document(card + card))
    assert len(p['rows']) == 1 and p['audit']['identical_duplicates'] == 1
    for other, reason in [(controlled_card(score='1:2'), 'CONFLICTING_ID'),
                          (controlled_card(status='live'), 'CONFLICTING_ID'),
                          (controlled_card(sid='2'), 'AMBIGUOUS_FIXTURE_ID')]:
        p = parse_document(controlled_document(card + other))
        assert p['rows'] == [] and p['audit']['rejection_counts'][reason] >= 1


@pytest.mark.parametrize('old,new', [('>Ended<', '>Ended a.e.t.<'), ('>0:2<', '>0:2 (3:4 pens)<'),
                                   ('data-competition_id="111"', 'data-competition_id="other"'),
                                   ('data-gender="male"', 'data-gender="female"')])
def test_regulation_result_and_own_card_context_required(old, new):
    p = parse_document(controlled_document(controlled_card().replace(old, new)))
    assert p['rows'] == []


def test_unicode_normalization_preserves_meaningful_names_and_invalid_source_time_is_unresolved():
    assert pilot.exact_label('  Železničar\t Pančevo ') == pilot.exact_label('Z\u030celeznic\u030car Panc\u030cevo')
    assert pilot.exact_label('Inter FC') != pilot.exact_label('Inter')
    row = {**parse_html(world_html())[0], 'source_kickoff_utc': '2026-08-22T16:30:00Z'}
    assert pilot.result_match(row, [], NOW) == (None, 'TIMEZONE_UNRESOLVED')
