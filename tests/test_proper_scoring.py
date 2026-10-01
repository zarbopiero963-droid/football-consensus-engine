from copy import deepcopy
import math

import pytest

from analysis.scoring.metrics import (ScorePolicy, multiclass_scores, scores_1x2,
    binary_scores, calibration, bootstrap_interval)
from analysis.scoring.report import report, dataset_from_rows

POLICY = ScorePolicy(1e-6,(0,.25,.5,.75,1),2,100,23,.95)
KNOWN = '2026-10-01T14:00:00Z'


def forecast(mid='a',method='fi',p=(.6,.25,.15)):
    return {'match_id':mid,'method':method,'probabilities':list(p),'market_id':'FT_1X2',
        'order':['1','X','2'],'raw_scale':1,'parse_status':'OK','vector_status':'FULL',
        'kickoff_utc':'2026-10-01T10:00:00Z','captured_at_utc':'2026-10-01T08:00:00Z',
        'sealed_at_utc':'2026-10-01T08:01:00Z','valid_prematch':True,
        'timezone_status':'EXPLICIT_OFFSET','matching_status':'ANCHOR_NATIVE',
        'role':'MODEL','provenance_verified':True}


def observations(mid='a',h=0,a=2):
    return [{'match_id':mid,'provider':provider,'observed_at_utc':'2026-10-01T12:00:00Z',
             'status':'FT','final_home_goals':h,'final_away_goals':a} for provider in ('source-a','source-b')]


def dataset():
    return {'forecasts':[forecast(mid,m) for mid in ('a','b') for m in ('fi','pin')],
            'observations':observations('a')+observations('b',2,0)}


def evaluate(data,methods=('fi','pin')):
    return report(data,known_at=KNOWN,methods=methods,policy=POLICY)


def test_uniform_known_multiclass_and_rps_values():
    r=scores_1x2([1/3]*3,'1',POLICY)
    assert r['log_loss']==pytest.approx(math.log(3))
    assert r['brier']==pytest.approx(2/3)
    assert r['brier']==sum(r['brier_components'])
    assert r['rps']==pytest.approx(5/18)
    assert scores_1x2([1/3]*3,'X',POLICY)['rps']==pytest.approx(1/9)


def test_issue_example_scores_componentwise():
    r=scores_1x2([.6,.25,.15],'1',POLICY)
    assert r['brier']==pytest.approx(.245)
    assert r['log_loss']==pytest.approx(-math.log(.6))
    assert r['rps']==pytest.approx((.4**2+.15**2)/2)


def test_real_zero_retained_and_clipped_only_for_log_score():
    raw=[0,.4,.6];r=scores_1x2(raw,'1',POLICY)
    assert raw==r['p_raw']==[0,.4,.6]
    assert r['extreme_probability'] and math.isfinite(r['log_loss'])
    assert sum(r['p_scored'])==pytest.approx(1)
    assert r['p_scored'][0]==pytest.approx(1e-6/(1+1e-6))
    assert r['brier']==pytest.approx(1.52)
    assert r['rps']==pytest.approx(.68)


@pytest.mark.parametrize('p',[[60,25,15],[.6,None,.4],[.6,.3,.2],[math.nan,0,1],[True,0,0],[-.1,.4,.7]])
def test_broken_scale_missing_nonfinite_vectors_rejected(p):
    with pytest.raises(ValueError):scores_1x2(p,'1',POLICY)


def test_binary_definition_and_extreme_zero():
    r=binary_scores(.7,1,POLICY)
    assert r['brier']==pytest.approx(.09)
    assert r['log_loss']==pytest.approx(-math.log(.7))
    assert binary_scores(0,1,POLICY)['brier']==1
    assert multiclass_scores([.7,.3],0,POLICY)['brier']==pytest.approx(.18)
    with pytest.raises(ValueError):binary_scores(.7,True,POLICY)


def test_order_cannot_silently_change_and_rps_is_reversal_symmetric():
    with pytest.raises(ValueError):scores_1x2([.6,.25,.15],'1',POLICY,order=('2','X','1'))
    assert scores_1x2([.6,.25,.15],'1',POLICY)['rps']==pytest.approx(
        scores_1x2([.15,.25,.6],'2',POLICY)['rps'])


def test_common_set_has_identical_denominators_and_paired_ci():
    data=dataset();data['forecasts'].append(forecast('extra','fi'))
    data['observations']+=observations('extra')
    r=evaluate(data)
    assert r['n']==2 and r['common_match_ids']==['a','b']
    assert {m['n'] for m in r['methods'].values()}=={2}
    assert r['methods']['fi']['eligible_forecasts']==3
    comparison=r['paired_comparisons'][0]['paired']['brier']
    assert comparison['mean']==comparison['ci']['low']==comparison['ci']['high']==0
    assert r['promotion_verdict'] is None and not r['production_eligible']
    assert r['clv']['status']=='NOT_COMPUTED'


def test_missing_required_method_keeps_empty_comparison_not_subset():
    r=evaluate(dataset(),('fi','pin','missing'))
    assert r['n']==0 and r['status']=='INSUFFICIENT_SAMPLE'
    assert all(m['metrics']['brier']['mean'] is None for m in r['methods'].values())


@pytest.mark.parametrize('change',[
    {'parse_status':'BROKEN'},{'vector_status':'PARTIAL'},{'raw_scale':100},
    {'matching_status':'FOUND_NAME_ONLY'},{'timezone_status':'VERIFIED_BROWSER_TZ'},
    {'sealed_at_utc':None},{'sealed_at_utc':'2026-10-01T10:00:00Z'},
    {'captured_at_utc':'2026-10-01T10:01:00Z'},{'provenance_verified':False},
    {'market_id':'FT_BTTS'},{'order':['2','X','1']},
])
def test_fail_closed_forecasts_without_changing_other_method_sample(change):
    data=dataset();data['forecasts'][0].update(change)
    r=evaluate(data)
    assert r['n']==1 and r['common_match_ids']==['b']
    assert all(m['n']==1 and m['metrics']['brier']['ci'] is None for m in r['methods'].values())


def test_real_zero_does_not_change_selection_when_outcome_changes():
    data=dataset();data['forecasts'][0]['probabilities']=[0,.4,.6]
    first=evaluate(data)
    data['observations']=observations('a',4,0)+observations('b',2,0)
    second=evaluate(data)
    assert first['common_match_ids']==second['common_match_ids']==['a','b']
    assert second['methods']['fi']['scored_rows'][0]['p_raw'][0]==0


def test_report_audits_nonfinite_raw_without_serializing_nan_or_scoring_it():
    import json
    data=dataset();data['forecasts'][0]['probabilities']=[math.nan,0,1]
    r=evaluate(data)
    assert r['common_match_ids']==['b'] and len(r['input_sha256'])==64
    json.dumps(r,allow_nan=False)


@pytest.mark.parametrize('provider_status',['single','conflict','pilot','nonfinal','future'])
def test_missing_conflict_pilot_nonfinal_and_unknown_results_never_score(provider_status):
    data=dataset();rows=data['observations'][:2]
    if provider_status=='single':data['observations'].pop(1)
    elif provider_status=='conflict':rows[1]['final_home_goals']=3
    elif provider_status=='pilot':rows[1]['provider']='pilot:other'
    elif provider_status=='nonfinal':rows[1]['status']='HT'
    elif provider_status=='future':rows[1]['observed_at_utc']='2026-10-01T15:00:00Z'
    assert evaluate(data)['common_match_ids']==['b']


def test_invalid_negative_score_and_latest_correction_are_not_verified():
    data=dataset();data['observations'][0]['final_home_goals']=-1
    assert evaluate(data)['common_match_ids']==['b']
    data=dataset();correction={**data['observations'][0],'final_home_goals':3,
                              'observed_at_utc':'2026-10-01T13:00:00Z'}
    data['observations'].append(correction)
    assert evaluate(data)['common_match_ids']==['b']


def test_same_time_ambiguity_is_input_order_independent():
    data=dataset();data['forecasts'].append(forecast('a','fi',(.2,.3,.5)))
    assert evaluate(data)['common_match_ids']==['b']
    data['forecasts'].reverse()
    assert evaluate(data)['common_match_ids']==['b']


def test_forecast_fixture_kickoff_mismatch_and_premature_results_rejected():
    data=dataset();data['forecasts'][1]['kickoff_utc']='2026-10-01T10:01:00Z'
    assert evaluate(data)['common_match_ids']==['b']
    data=dataset()
    for row in data['observations'][:2]:row['observed_at_utc']='2026-10-01T09:59:00Z'
    assert evaluate(data)['common_match_ids']==['b']


def test_calibration_murphy_is_estimate_with_residual_and_fixed_outcome_blind_bins():
    r=calibration([.3,.4,.6,.7],[0,1,1,0],POLICY)
    assert r['ece']==pytest.approx(.15)
    assert r['murphy']['kind']=='MURPHY_ESTIMATE'
    assert r['murphy']['binning_residual']!=0
    assert r['base_rate_role']=='IN_SAMPLE_BASE_RATE_REFERENCE_NOT_PREDICTIVE_BASELINE'
    assert calibration([.3],[0],POLICY)['murphy'] is None
    assert calibration([],[],POLICY)['ece'] is None
    one=calibration([0,1],[0,1],POLICY)
    assert one['bins'][0]['n']==one['bins'][-1]['n']==1


def test_bootstrap_reproducible_and_singleton_has_no_ci():
    assert bootstrap_interval([1,2,3],POLICY)==bootstrap_interval([1,2,3],POLICY)
    assert bootstrap_interval([1],POLICY) is None
    assert POLICY.audit()['sha256']==POLICY.audit()['sha256']


def test_policy_cannot_be_implicitly_or_invalidly_configured():
    config={'epsilon':1e-6,'bin_edges':(0,.5,1),'min_bin_count':2,
            'bootstrap_replicates':100,'bootstrap_seed':1,'confidence_level':.95}
    for key,value in [('epsilon',0),('bin_edges',(0,.5,.4,1)),('bootstrap_seed',None),
                      ('bootstrap_replicates',10001),('confidence_level',1),('min_bin_count',True)]:
        with pytest.raises(ValueError):ScorePolicy(**{**config,key:value})
    with pytest.raises(ValueError):report(dataset(),known_at='2026-10-01',methods=['fi'],policy=POLICY)
    with pytest.raises(ValueError):evaluate(dataset(),('fi','fi'))


def test_adapter_requires_real_source_timezone_evidence_and_native_identity():
    row={'match_id':'a','kickoff_utc':'2026-10-01T10:00:00Z',
        'snapshot_timestamp_utc':'2026-10-01T08:00:00Z','imported_at':'2026-10-01T08:01:00Z',
        'home_team':'Synthetic Home','away_team':'Synthetic Away','league':'Synthetic League',
        'valid_prematch':True,'payload':{'source_url':'https://footinsights.com/predictions/synthetic/',
            'p_home_normalized':.6,'p_draw_normalized':.25,'p_away_normalized':.15,
            'timezone_status':'EXPLICIT_OFFSET','kickoff_raw':'2026-10-01T10:00:00Z',
            'source_time_evidence':'SportsEvent.startDate'}}
    data=dataset_from_rows([row],observations(),known_at=KNOWN)
    assert evaluate(data,('footinsights',))['n']==1
    row['payload'].update(p_home_normalized='0.600001',p_draw_normalized='0.25',p_away_normalized='0.15')
    normalized=dataset_from_rows([row],observations(),known_at=KNOWN)
    scored=evaluate(normalized,('footinsights',))['methods']['footinsights']['scored_rows'][0]
    assert scored['source_components_raw']['1']=='0.600001'
    assert sum(scored['p_raw'])==pytest.approx(1)
    assert scored['adapter_normalization']
    row['payload'].pop('kickoff_raw')
    assert evaluate(dataset_from_rows([row],observations(),known_at=KNOWN),('footinsights',))['n']==0
    row['payload']['kickoff_raw']='2026-10-01T10:00:00Z';row['payload']['pilot_id']='pilot'
    assert evaluate(dataset_from_rows([row],observations(),known_at=KNOWN),('footinsights',))['n']==0


def test_adapter_rejects_historical_fixture_revisions():
    rows=[]
    for team in ('Synthetic A','Synthetic B'):
        rows.append({'match_id':'a','home_team':team,'away_team':'Other','league':'League',
            'kickoff_utc':'2026-10-01T10:00:00Z','snapshot_timestamp_utc':'2026-10-01T08:00:00Z',
            'imported_at':'2026-10-01T08:01:00Z','valid_prematch':True,
            'payload':{'source_url':'https://footinsights.com/predictions/synthetic/'}})
    data=dataset_from_rows(rows,observations(),known_at=KNOWN)
    assert all(f['matching_status']=='MATCH_UNRESOLVED' for f in data['forecasts'])
