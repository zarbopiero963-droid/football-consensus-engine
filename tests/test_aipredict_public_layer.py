from copy import deepcopy
import base64
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from staging.issue29_pilot.pilot48 import parse_aipredict, SOURCES

PUBLIC = '''09:00
World · UEFA Nations League
FREE TIP OF THE DAY
Azerbaijan
3
:
0
AI PREDICTION
L
Liechtenstein
AI
1 · 56%
X · 27%
2 · 17%
TIP
1
Azerbaijan to win
56%
Full AI analysis →'''
LOCKED_ODDS = '''11:45
Germany
AI PREDICTION
S
Serbia
ODDS
71%
18%
11%
Tip
51%
AI'''


def test_actual_free_model_card_is_distinct_from_locked_market_bars():
    rows=parse_aipredict(PUBLIC+'\n'+LOCKED_ODDS)
    assert len(rows)==1
    row=rows[0]
    assert (row['home_team'],row['away_team'])==('Azerbaijan','Liechtenstein')
    assert (row['p1'],row['px'],row['p2'])==(56,27,17)
    assert row['kickoff_raw']=='09:00' and row['source_timezone']==''
    assert row['timezone_status']=='TIMEZONE_UNRESOLVED' and 'kickoff_utc' not in row
    assert row['payload_extra']['display_prediction_raw']==['3',':','0']
    assert row['payload_extra']['probability_evidence']=='PUBLIC_AI_LABELLED_1_X_2'
    assert SOURCES['aipredict_io']=='https://aipredict.io/football/'


def test_no_odds_distributions_become_models_in_either_layout():
    assert parse_aipredict(LOCKED_ODDS)==[]
    assert parse_aipredict('Argentina\n2:0\nAI PREDICTION\nB\nBolivia\nODDS\n70%\n18%\n12%')==[]


@pytest.mark.parametrize('change',[
    ('AI\n1 ·','ODDS\n1 ·'),('1 · 56%','1 · 156%'),('1 · 56%','1 · -1%'),
    ('1 · 56%','1 · NaN%'),('X · 27%',''),('2 · 17%','2 · 70%'),
    ('1 · 56%','X · 56%'),('AI PREDICTION','FT'),('AI PREDICTION','LIVE'),
    ('AI PREDICTION','HT'),('Full AI analysis →',''),
])
def test_broken_missing_live_or_ambiguous_card_does_not_fall_back_to_odds(change):
    assert parse_aipredict(PUBLIC.replace(*change)+'\n'+LOCKED_ODDS)==[]


def test_public_real_zero_is_not_missing_and_badge_is_not_team():
    rows=parse_aipredict(PUBLIC.replace('1 · 56%','1 · 0%').replace('X · 27%','X · 83%'))
    assert rows[0]['p1']==0 and rows[0]['away_team']=='Liechtenstein'


def test_duplicate_model_role_and_card_boundaries_are_ambiguous():
    assert parse_aipredict(PUBLIC.replace('AI\n1 ·','AI\nAI\n1 ·'))==[]
    assert parse_aipredict(PUBLIC.replace('Full AI analysis →','')+'\n'+PUBLIC)==[]
    assert parse_aipredict(PUBLIC.replace('TIP\n1','1 · 60%\nTIP\n1'))==[]


def test_historical_homepage_stays_zero_and_live_export_has_exactly_one_native_vector():
    path=Path(__file__).parent/'fixtures/aipredict_public_layer_20261001.json'
    probe=json.loads(path.read_text())
    assert probe['production_eligible'] is False
    text=gzip.decompress(base64.b64decode(probe['live_dom_gzip_base64']))
    assert hashlib.sha256(text).hexdigest()==probe['live_dom_sha256']
    rows=parse_aipredict(text.decode())
    assert len(rows)==1 and [rows[0][k] for k in ('p1','px','p2')]==[56,27,17]
    assert rows[0]['timezone_status']=='TIMEZONE_UNRESOLVED'
    assert parse_aipredict(probe['sealed_homepage_visible_excerpt'])==[]
