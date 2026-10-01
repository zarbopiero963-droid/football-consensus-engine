"""Strict match diagnostics, including independently captured public sources."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from app.market_reference import audit_market_candidates, exact_market_match, market_match_rejection, utc
from prospective.collect import parse_fi_html

KICK = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)


def pair():
    return ({'home': 'United', 'away': 'Town', 'kickoff': KICK, 'league': 'Test league'},
            {'home': 'United', 'away': 'Town', 'start': KICK, 'league': 'Test league'})


@pytest.mark.parametrize('changes,reason', [
    ({'start': KICK.replace(tzinfo=None)}, 'TIMEZONE_OR_TIME_MISSING'),
    ({'start': KICK + timedelta(minutes=15, seconds=1)}, 'KICKOFF_MISMATCH'),
    ({'home': 'FC United'}, 'PAIR_MISMATCH'),
    ({'away': 'Town Women'}, 'PAIR_MISMATCH'),
    ({'league': ''}, 'COMPETITION_MISSING'),
    ({'league': 'Other league'}, 'LEAGUE_MISMATCH'),
    ({'country': 'GB'}, 'COUNTRY_MISMATCH'),
    ({'category': 'MEN:SENIOR:FIRST'}, 'CATEGORY_MISMATCH'),
])
def test_rejections_share_the_production_gate(changes, reason):
    model, quote = pair()
    quote.update(changes)
    before = deepcopy((model, quote))
    assert market_match_rejection(model, quote, KICK - timedelta(hours=1)) == reason
    assert not exact_market_match(model, quote, KICK - timedelta(hours=1))
    assert (model, quote) == before


def test_capture_gate_precedes_kickoff_and_context_rejections():
    model, quote = pair()
    quote.update(league='Other league', start=KICK + timedelta(hours=1))
    assert market_match_rejection(model, quote, KICK) == 'CAPTURE_NOT_PREMATCH'
    assert market_match_rejection(model, quote, 'naive') == 'TIMEZONE_OR_TIME_MISSING'


def test_audit_distinguishes_missing_rejected_unique_and_ambiguous_without_prices():
    model, quote = pair()
    quote.update(captured_at=KICK - timedelta(hours=1), matchup_id=123, raw_file='markets_1.json', dec=[2, 3, 4])
    original = deepcopy(quote)
    assert audit_market_candidates(model, [])['status'] == 'NO_MARKET_QUOTES'
    assert audit_market_candidates(model, [dict(quote, home='Town', away='United')])['status'] == 'NO_EXACT_PAIR'
    assert audit_market_candidates(dict(model, home=''), [quote])['status'] == 'NO_EXACT_PAIR'
    rejected = audit_market_candidates(model, [dict(quote, league='Other league')])
    assert rejected['status'] == 'EXACT_PAIR_REJECTED'
    assert rejected['candidate_first_rejections'] == {'LEAGUE_MISMATCH': 1}
    unique = audit_market_candidates(model, [dict(quote, league='Other league'), quote])
    assert unique['status'] == 'UNIQUE_EXACT_MATCH' and unique['matched_candidates'] == 1
    assert unique['exact_pair_candidates'] == 2
    assert audit_market_candidates(model, [quote, quote])['status'] == 'AMBIGUOUS_EXACT_MATCH'
    assert 'dec' not in unique['candidates'][1]
    assert quote == original


def test_live_argentina_pair_is_rejected_for_source_competition_names():
    pack = json.loads((Path(__file__).parents[1] / 'staging/market_matching/live_probe_20261001.json').read_text())
    docs = {}
    for name, doc in pack['documents'].items():
        raw = gzip.decompress(base64.b64decode(doc['raw_gzip_base64']))
        assert len(raw) == doc['bytes_utf8']
        assert hashlib.sha256(raw).hexdigest() == doc['sha256']
        docs[name] = raw.decode()
    model = parse_fi_html(docs['footinsights'], pack['documents']['footinsights']['url'])
    matchups = json.loads(docs['pinnacle'])
    top = [m for m in matchups if m.get('type') == 'matchup' and not m.get('parentId')
           and not m.get('parent') and not m.get('isLive')]
    assert len(matchups) == 206 and len(top) == 8
    quotes = []
    for m in top:
        names = {p['alignment']: p['name'] for p in m['participants']}
        quotes.append({'home': names['home'], 'away': names['away'], 'league': m['league']['name'],
                       'start': m['startTime'], 'captured_at': pack['known_at_utc'], 'matchup_id': m['id']})
    audit = audit_market_candidates(model, quotes)
    assert model['home'] == 'Argentina' and model['away'] == 'Burkina Faso'
    assert model['kickoff'] == utc('2026-10-04T00:00:00Z')
    assert model['league'] == 'International Friendlies'
    assert audit['status'] == 'EXACT_PAIR_REJECTED' and audit['matched_candidates'] == 0
    assert audit['candidate_first_rejections'] == {'LEAGUE_MISMATCH': 1}
    assert audit['candidates'][0]['provider_match_id'] == 1637113313
    assert audit['candidates'][0]['league'] == 'International - Friendlies'
    assert audit['candidates'][0]['kickoff_utc'] == model['kickoff'].isoformat()
    assert pack['production_eligible'] is False and pack['neon_lease_state'] == 'STOPPED'
