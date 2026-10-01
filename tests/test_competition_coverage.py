from copy import deepcopy
import pytest

from analysis.coverage.report import report

KNOWN = '2026-10-01T13:00:00Z'


def pred(**kwargs):
    return {'source':'footinsights','source_match_key':'synthetic','match_id':'synthetic',
            'league':'Synthetic League','country':None,'category':None,'is_anchor':True,
            'captured_at_utc':'2026-09-29T12:00:00Z','sealed_at_utc':'2026-09-29T12:01:00Z',
            'kickoff_utc':'2026-09-30T16:00:00Z','timezone_status':'EXPLICIT_OFFSET',
            'matching_status':'ANCHOR_NATIVE','fixture_versions':1,'valid_prematch':True,
            'probabilities':[.6,.25,.15],'raw_scale':1,'parse_status':'OK',**kwargs}


def obs(provider='synthetic-a', **kwargs):
    return {'match_id':'synthetic','provider':provider,'observed_at_utc':'2026-09-30T18:00:00Z',
            'status':'FT','final_home_goals':0,'final_away_goals':2,**kwargs}


def ticket(**kwargs):
    return {'match_id':'synthetic','league':'Synthetic League','source':'footinsights',
            'market_id':'FT_1X2','status':'LOST','kickoff_utc':'2026-09-30T16:00:00Z',
            'updated_at':'2026-09-30T19:00:00Z',**kwargs}


def run(predictions=None, observations=None, tickets=None):
    return report({'predictions':predictions or [],'observations':observations or [],
                   'market_tickets':tickets or []},known_at=KNOWN)


def test_latest_rows_rates_no_promotion_and_input_immutable():
    data={'predictions':[pred(),pred(captured_at_utc='2026-09-29T13:00:00Z',
         sealed_at_utc='2026-09-29T13:01:00Z')],'observations':[],'market_tickets':[]}
    before=deepcopy(data);result=report(data,known_at=KNOWN);c=result['competitions'][0]
    assert result['denominators']['latest_source_rows']==1
    assert c['sources'][0]['full_vector_rate']==1 and c['sources'][0]['safe_match_rate']==1
    assert c['sources'][0]['timezone_resolved_rate']==1 and c['state']=='MONITORING_ONLY'
    assert c['canonical_competition_id'] is None and not c['production_eligible']
    assert c['country'] is None and c['category'] is None and data==before


def test_weekly_verified_fields_and_distinct_fixture_denominator():
    result=run([pred()],[obs(),obs('synthetic-b')],[ticket(),ticket(market_id='FT_BTTS')])
    c=result['competitions'][0]
    assert c['verified_ft_fixtures_last7d']==1 and c['settled_fixtures_last7d']==1
    assert c['settled_fixtures_by_market']=={'FT_1X2':1,'FT_BTTS':1}
    assert c['result_providers_observed']==['synthetic-a','synthetic-b']
    assert not c['production_eligible']


@pytest.mark.parametrize('kind',['single','pilot','conflict','future','nonfinal','missing','negative','duplicate_provider'])
def test_unverified_results_never_count_settled(kind):
    observations=[obs(),obs('synthetic-b')]
    if kind=='single':observations=observations[:1]
    if kind=='pilot':observations[1]['provider']='pilot:synthetic-b'
    if kind=='conflict':observations[1]['final_home_goals']=1
    if kind=='future':observations[1]['observed_at_utc']='2026-10-02T12:00:00Z'
    if kind=='nonfinal':observations[1]['status']='HT'
    if kind=='missing':observations[1]['final_home_goals']=None
    if kind=='negative':observations[1]['final_home_goals']=-1
    if kind=='duplicate_provider':observations[1]['provider']='synthetic-a'
    c=run([pred()],observations,[ticket()])['competitions'][0]
    assert c['verified_ft_fixtures_last7d']==0 and c['settled_fixtures_last7d']==0
    assert c['unverified_settled_tickets']==1


def test_ft_does_not_certify_ht_or_corners_and_unpriced_does_not_matter():
    c=run([pred()],[obs(),obs('synthetic-b')],
          [ticket(),ticket(market_id='HT_OU_0.5'),ticket(market_id='FT_CORNERS_OU_9.5')])['competitions'][0]
    assert c['settled_fixtures_by_market']=={'FT_1X2':1}
    assert c['unverified_settled_tickets']==2
    unknown=run([pred()],[obs(),obs('synthetic-b')],[ticket(market_id='FT_UNKNOWN')])['competitions'][0]
    assert unknown['settled_fixtures_last7d']==0 and unknown['unverified_settled_tickets']==1


@pytest.mark.parametrize('changes',[
    {'matching_status':'FOUND_NAME_ONLY'}, {'matching_status':'FOUND'},
    {'timezone_status':'VERIFIED_BROWSER_TZ'}, {'sealed_at_utc':None},
    {'sealed_at_utc':'2026-10-01T14:00:00Z'}, {'fixture_versions':2},
    {'valid_prematch':False}, {'is_anchor':False},
    {'captured_at_utc':'2026-09-30T16:00:00Z','sealed_at_utc':'2026-09-30T16:01:00Z'},
])
def test_unsafe_source_not_counted(changes):
    result=run([pred(**changes)])
    assert not result['competitions'] or result['competitions'][0]['sources'][0]['safe_matches']==0


def test_group_context_and_unknown_competition_not_invented():
    rows=[pred(country='IT'),pred(source_match_key='other',country='GB'),
          pred(source='pilot-model',source_match_key='blank',league='',is_anchor=False)]
    result=run(rows)
    assert len(result['competitions'])==3
    assert all(not c['production_eligible'] for c in result['competitions'])
    assert any(c['competition_label']=='UNRESOLVED' for c in result['competitions'])


def test_future_and_ambiguous_predictions_and_latest_result_revision():
    assert not run([pred(captured_at_utc='2026-10-02T12:00:00Z')])['competitions']
    row=pred();other=pred(probabilities=[.2,.3,.5])
    a=run([row,other]);b=run([other,row])
    assert a==b and a['competitions'][0]['sources'][0]['full_vectors']==0
    observations=[obs(),obs('synthetic-b'),obs(observed_at_utc='2026-10-01T11:00:00Z',final_home_goals=1)]
    assert run([pred()],observations)['competitions'][0]['verified_ft_fixtures_last7d']==0


def test_shapes_week_boundaries_empty_denominators_and_cutoff():
    for probabilities in ([.6,.25,.15,.1],[],[.6,None,.4],[60,25,15]):
        assert run([pred(probabilities=probabilities)])['competitions'][0]['sources'][0]['full_vectors']==0
    c=run([pred(kickoff_utc='2026-09-23T12:00:00Z')],[obs(),obs('synthetic-b')],
          [ticket(kickoff_utc='2026-09-23T12:00:00Z')])['competitions'][0]
    assert c['verified_ft_fixtures_last7d']==0 and c['settled_fixtures_last7d']==0
    assert run()['denominators']=={'input_prediction_rows':0,'latest_source_rows':0,'competition_labels':0}
    c=run(tickets=[ticket(status='PENDING')])['competitions'][0]
    assert c['state']=='INSUFFICIENT' and c['sources']==[]
    with pytest.raises(ValueError,match='EXPLICIT_KNOWLEDGE_TIME_REQUIRED'):
        report({'predictions':[]},known_at='2026-10-01T13:00:00')
