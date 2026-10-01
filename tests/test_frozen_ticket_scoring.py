from copy import deepcopy
from dataclasses import replace
import math

import pytest

from analysis.decisions.protocol import Protocol, day_interval, verdict
from analysis.decisions.report import report
from analysis.scoring.metrics import ScorePolicy
from app.market_reference import MATCH_CONTRACT

POLICY = ScorePolicy(1e-6,(0,.25,.5,.75,1),2,1000,23,.95)
PROTOCOL = Protocol('2026-10-01T00:00:00Z','2026-10-29T00:00:00Z',
                    'S1',2,2,.9,.01,.02,7,0,POLICY)
KNOWN = '2026-11-05T00:00:00Z'


def ticket(mid='a', *, day=1, batch='first', p=(.6,.25,.15)):
    kick=f'2026-10-{day:02d}T10:00:00Z'; capture=f'2026-10-{day:02d}T07:00:00Z'
    request=f'2026-10-{day:02d}T08:00:00Z'; created=f'2026-10-{day:02d}T08:00:01Z'
    payload={'source_url':'https://footinsights.com/predictions/native/',
        'source_time_evidence':'SportsEvent.startDate','timezone_status':'EXPLICIT_OFFSET','kickoff_raw':kick,
        'pin_matchup_id':'pin-'+mid,'sha256_odds_raw':'a'*64,'name_score_note':MATCH_CONTRACT,
        'pin_home':'Home','pin_away':'Away','pin_league':'League','pin_kickoff_utc':kick,
        'pin_snapshot_timestamp_utc':f'2026-10-{day:02d}T07:00:01Z',
        'odds_home':2,'odds_draw':4,'odds_away':4,
        'p_home_normalized':p[0],'p_draw_normalized':p[1],'p_away_normalized':p[2]}
    original={'match_id':mid,'kickoff_utc':kick,'snapshot_timestamp_utc':capture,
        'home_team':'Home','away_team':'Away','league':'League','payload':payload,
        'imported_at':f'2026-10-{day:02d}T07:00:02Z','valid_prematch':True}
    view={'match_id':mid,'kickoff_utc':kick,'snapshot_timestamp_utc':capture,
        'home':'Home','away':'Away','league':'League','status':'BET','strategies':['S1','S2'],
        'model_pick':'1','model_probability':p[0],'p1':p[0],'px':p[1],'p2':p[2],
        'picked_odds':2,'pinnacle_odds':{'1':2,'X':4,'2':4},'pinnacle_fair':{'1':.5,'X':.25,'2':.25}}
    return {'batch_id':batch,'match_id':mid,'requested_at_utc':request,'created_at':created,
        'batch_created_at':created,'kickoff_utc':kick,'snapshot_timestamp_utc':capture,
        'home_team':'Home','away_team':'Away','league':'League','display_status':'BET',
        'strategies':['S1','S2'],'shown_to_user':True,'model_pick':'1','model_probability':p[0],
        'picked_odds':2,'snapshot_view':view,'original_snapshot':original}


def observations(mid='a', *, day=1, h=2, a=0):
    return [{'match_id':mid,'provider':provider,'observed_at_utc':f'2026-10-{day:02d}T12:00:00Z',
        'kickoff_utc':f'2026-10-{day:02d}T10:00:00Z','status':'FT',
        'final_home_goals':h,'final_away_goals':a} for provider in ('source-a','source-b')]


def evaluate(tickets=None, obs=None, **kwargs):
    return report({'tickets':tickets if tickets is not None else [ticket()],
                   'observations':obs if obs is not None else observations()},
                  known_at=kwargs.pop('known_at',KNOWN),protocol=kwargs.pop('protocol',PROTOCOL),**kwargs)


def registration(protocol=PROTOCOL):
    return {'protocol_id':'registered','contract_version':'frozen-ticket-v1',
        'policy_json':protocol.canonical(),'policy_sha256':protocol.sha256(),
        'window_start':protocol.window_start,'window_end':protocol.window_end,
        'sealed_at_utc':'2026-09-30T00:00:00Z'}


def test_exact_displayed_60_not_later_80_and_readonly():
    first=ticket(); late=ticket(batch='later',p=(.8,.1,.1))
    late['requested_at_utc']='2026-10-01T09:00:00Z'
    # Provenance rows may carry different model values. They must never rescore the view.
    first['original_snapshot']['payload']['p_home_normalized']=.9
    before=deepcopy([first,late]); r=evaluate([first,late])
    assert r['n']==1 and r['cohort_fixtures']==1
    row=r['model']['scored_rows'][0]
    assert row['p_raw']==[.6,.25,.15] and row['model_probability']==.6
    assert row['log_loss']==pytest.approx(-math.log(.6))
    assert r['model']['pick_calibration']['bins'][2]['mean_probability']==.6
    assert r['roi']['roi']==1 and r['roi']['rows'][0]['picked_odds']==2
    assert [first,late]==before
    assert r['benchmark']['rows'][0]['benchmark']['p_raw']==[.5,.25,.25]


def test_first_invalid_not_replaced_by_valid_later_and_no_bet_not_counted():
    first=ticket(); first['snapshot_view']['p2']=None
    late=ticket(batch='later'); late['requested_at_utc']='2026-10-01T09:00:00Z'
    no_bet=ticket('no-bet'); no_bet['display_status']='NO_BET'
    r=evaluate([first,late,no_bet])
    assert r['n']==0 and r['cohort_fixtures']==1
    assert r['excluded']['ticket:INVALID_OR_INCOMPLETE_FROZEN_VECTOR']==1


@pytest.mark.parametrize('change,reason',[
    (lambda t:t.update(original_snapshot=None),'MISSING_FROZEN_VIEW_OR_ORIGINAL_AUDIT'),
    (lambda t:t['original_snapshot']['payload'].update(pilot_id='pilot'),'PROVENANCE_OR_PRODUCTION_UNRESOLVED'),
    (lambda t:t['original_snapshot']['payload'].update(source_time_evidence=None),'SOURCE_TIME_UNRESOLVED'),
    (lambda t:t['snapshot_view'].update(away='Other'),'IDENTITY_OR_REFERENCED_SNAPSHOT_MISMATCH'),
    (lambda t:t.update(created_at='2026-10-01T10:00:00Z'),'NOT_FROZEN_PREMATCH'),
    (lambda t:t['original_snapshot'].update(imported_at='2026-10-01T08:00:01Z'),'NOT_FROZEN_PREMATCH'),
    (lambda t:t.update(model_probability=.8),'FROZEN_PICK_OR_VALUE_MISMATCH'),
    (lambda t:t['snapshot_view'].update(p1=True),'INVALID_OR_INCOMPLETE_FROZEN_VECTOR'),
    (lambda t:t['snapshot_view'].update(p1=float('nan')),'INVALID_OR_INCOMPLETE_FROZEN_VECTOR'),
    (lambda t:t['snapshot_view'].update(p1=.60002),'INVALID_OR_INCOMPLETE_FROZEN_VECTOR'),
])
def test_negative_ticket_gates(change,reason):
    t=ticket(); change(t); r=evaluate([t])
    assert r['n']==0 and r['excluded']['ticket:'+reason]==1


def test_raw_zero_and_six_decimal_rounding_not_repaired():
    r=evaluate([ticket(p=(0,.400001,.6))],observations(h=0,a=0))
    assert r['n']==1
    scored=r['model']['scored_rows'][0]
    assert scored['p_raw']==[0,.400001,.6] and scored['extreme_probability']
    assert scored['p_scored'][0]>0
    assert scored['brier']==pytest.approx((.400001-1)**2+.6**2)


def test_tied_different_views_and_context_revisions_reject():
    for second in (ticket(batch='second',p=(.8,.1,.1)), ticket(batch='second')):
        if second['model_probability']==.6: second['home_team']='Revised'
        r=evaluate([ticket(),second])
        assert r['n']==0 and r['excluded']['ticket:AMBIGUOUS_FIRST_OR_CONTEXT']==1


def test_missing_quote_excludes_roi_not_model_and_missing_is_not_zero():
    t=ticket(); t['snapshot_view']['picked_odds']=None; t['picked_odds']=None
    t['snapshot_view']['pinnacle_fair']={'1':None,'X':None,'2':None}
    r=evaluate([t]); assert r['n']==1 and r['roi']['n']==0 and r['benchmark']['n']==0
    assert r['roi']['net_profit'] is None and r['roi']['roi'] is None
    assert r['roi']['verdict']['status']=='DATI_INSUFFICIENTI'


def test_later_original_prices_do_not_replace_ticket_and_mismatch_excludes_roi():
    t=ticket(); t['original_snapshot']['payload']['odds_home']=3
    r=evaluate([t]); assert r['n']==1 and r['roi']['n']==0 and r['benchmark']['n']==0


@pytest.mark.parametrize('obs',[
    observations()[:1],
    [{**r,'provider':'pilot:'+r['provider']} for r in observations()],
    [{**r,'final_away_goals':None} for r in observations()],
    [{**r,'final_home_goals':-1} for r in observations()],
    [{**r,'status':'LIVE'} for r in observations()],
    [{**r,'kickoff_utc':'2026-10-01T11:00:00Z'} for r in observations()],
    [{**r,'observed_at_utc':'2026-10-01T09:00:00Z'} for r in observations()],
    [*observations(),{**observations()[0],'final_home_goals':0}],
])
def test_only_final_both_fields_verified_nonpilot_unambiguous(obs):
    assert evaluate(obs=obs)['n']==0


def test_future_correction_and_terminal_freeze_and_zero_zero():
    obs=observations(h=0,a=0)
    correction={**obs[0],'observed_at_utc':'2026-11-06T00:00:00Z','final_home_goals':2}
    r=evaluate(obs=obs+[correction],known_at='2026-11-10T00:00:00Z')
    assert r['n']==1 and r['model']['scored_rows'][0]['outcome']=='X'
    assert r['evidence_cutoff'].startswith('2026-11-05')
    assert evaluate(known_at='2026-10-01T11:00:00Z')['n']==0


def test_pairing_same_fixture_subset_and_separate_denominators():
    a=ticket(); b=ticket('b',day=2)
    b['snapshot_view']['pinnacle_fair']={}
    r=evaluate([a,b],observations()+observations('b',day=2))
    assert r['n']==2 and r['benchmark']['n']==1 and r['roi']['n']==2
    assert len(r['benchmark']['rows'])==1
    assert r['benchmark']['verdict']['status']=='DATI_INSUFFICIENTI'


def test_fixed_protocol_verdicts_no_interim_or_retroactive_claim():
    rows=[('2026-10-01',-.05),('2026-10-02',-.05)]
    kwargs=dict(denominator=2,rows=rows,kind='quality')
    assert verdict(PROTOCOL,None,known_at=KNOWN,**kwargs)['status']=='DATI_INSUFFICIENTI'
    assert verdict(PROTOCOL,registration(),known_at='2026-10-20T00:00:00Z',**kwargs)['status']=='DATI_INSUFFICIENTI'
    assert verdict(PROTOCOL,registration(),known_at=KNOWN,**kwargs)['status']=='VANTAGGIO_DIMOSTRATO_NEL_CAMPIONE'
    bad=registration(); bad['sealed_at_utc']=PROTOCOL.window_start
    assert verdict(PROTOCOL,bad,known_at=KNOWN,**kwargs)['reasons']==['INVALID_PREREGISTRATION']
    bad=registration(); bad['policy_sha256']='0'*64
    assert verdict(PROTOCOL,bad,known_at=KNOWN,**kwargs)['status']=='DATI_INSUFFICIENTI'
    for value,status in ((0,'VANTAGGIO_NON_DIMOSTRATO'),(-.005,'VANTAGGIO_NON_DIMOSTRATO'),(.1,'PEGGIORE_DEL_BENCHMARK')):
        r=verdict(PROTOCOL,registration(),known_at=KNOWN,denominator=2,
            rows=[(d,value) for d,_ in rows],kind='quality')
        assert r['status']==status and r['action'].startswith('STOP_ADVANTAGE_CLAIM')
    assert verdict(PROTOCOL,registration(),known_at=KNOWN,denominator=3,rows=rows,kind='quality')['status']=='DATI_INSUFFICIENTI'


def test_roi_interval_margin_negative_and_minimum_days():
    for value,status in ((.03,'VANTAGGIO_DIMOSTRATO_NEL_CAMPIONE'),(.01,'VANTAGGIO_NON_DIMOSTRATO'),(-1,'RENDIMENTO_NEGATIVO')):
        assert verdict(PROTOCOL,registration(),known_at=KNOWN,denominator=2,
            rows=[('day1',value),('day2',value)],kind='roi')['status']==status
    r=verdict(PROTOCOL,registration(),known_at=KNOWN,denominator=2,
            rows=[('same-day',1),('same-day',1)],kind='roi')
    assert r['status']=='DATI_INSUFFICIENTI' and r['ci'] is None


def test_day_block_bootstrap_preserves_intra_day_cluster_and_is_repeatable():
    rows=[('a',1)]*50+[('b',-1)]*50
    interval=day_interval(rows,POLICY)
    assert interval==day_interval(rows,POLICY)
    assert interval['low']==-1 and interval['high']==1


@pytest.mark.parametrize('changes',[
    {'window_start':'2026-10-30T00:00:00Z'}, {'min_fixtures':True}, {'min_days':3},
    {'min_coverage':0}, {'min_log_loss_gain':0}, {'min_roi':float('nan')},
    {'strategy':'S10'}, {'score_policy':replace(POLICY,bootstrap_replicates=10)},
    {'score_policy':replace(POLICY,confidence_level=.9)},
])
def test_incomplete_invalid_or_underpowered_policy_fails(changes):
    with pytest.raises(ValueError): replace(PROTOCOL,**changes)


def test_explicit_configuration_roundtrip_and_unknown_field_rejected():
    import json
    assert Protocol.from_dict(json.loads(PROTOCOL.canonical()))==PROTOCOL
    with pytest.raises(TypeError): Protocol.from_dict({**json.loads(PROTOCOL.canonical()),'tuned_after_test':True})
