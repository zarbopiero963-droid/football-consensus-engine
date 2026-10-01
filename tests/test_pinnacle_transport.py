import asyncio
import hashlib
import json
import gzip
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from bridge import server, browser_use, session_leases
from bridge.browser_use import BrowserUseBridge, Session
from bridge.security import UnsafeURL
from prospective import collect


def document(lid,kind,body='[]'):
    suffix='matchups?brandId=0' if kind=='matchups' else 'markets/straight'
    url=f'https://guest.api.arcadia.pinnacle.com/0.1/leagues/{lid}/{suffix}'
    return {'http_status':200,'requested_url':url,'final_url':url,'body':body,
        'bytes_utf8':len(body.encode()),'sha256':hashlib.sha256(body.encode()).hexdigest(),
        'truncated':False,'captured_at_utc':'2026-10-01T14:00:00Z'}


@pytest.fixture
def internal(monkeypatch):
    monkeypatch.setenv('COLLECTOR_BRIDGE_TOKEN','synthetic-token')
    fake=SimpleNamespace(create=AsyncMock(return_value={'session_id':'synthetic'}),
        stop=AsyncMock(return_value={'status':'stopped'}),
        json_document=AsyncMock(side_effect=lambda sid,url:document(int(url.split('/leagues/')[1].split('/')[0]),
            'matchups' if '/matchups?' in url else 'markets')))
    monkeypatch.setattr(server,'bridge',fake)
    return TestClient(server.app),fake,{'X-Collector-Token':'synthetic-token'}


def test_batch_auth_before_browser_allocation(internal,monkeypatch):
    client,fake,headers=internal
    assert client.get('/internal/pinnacle/league-batch?ids=1').status_code==401
    monkeypatch.delenv('COLLECTOR_BRIDGE_TOKEN')
    assert client.get('/internal/pinnacle/league-batch?ids=1',headers=headers).status_code==401
    fake.create.assert_not_awaited()


@pytest.mark.parametrize('ids',['','0','-1','true','1,1','1,2,','1?url=http://127.0.0.1','9999999999',
                                ','.join(map(str,range(1,14)))])
def test_ids_and_paths_cannot_be_invented_or_unbounded(internal,ids):
    client,fake,headers=internal
    assert client.get('/internal/pinnacle/league-batch',params={'ids':ids},headers=headers).status_code==400
    fake.create.assert_not_awaited()


def test_one_session_for_batch_fixed_paths_and_confirmed_stop(internal):
    client,fake,headers=internal
    r=client.get('/internal/pinnacle/league-batch?ids=2117,200727',headers=headers)
    assert r.status_code==200 and r.json()['session_stopped'] is True
    assert [i['id'] for i in r.json()['items']]==[2117,200727]
    fake.create.assert_awaited_once();fake.stop.assert_awaited_once_with('synthetic')
    assert fake.json_document.await_count==4
    assert all(call.args[1].startswith('https://guest.api.arcadia.pinnacle.com/0.1/leagues/')
               for call in fake.json_document.await_args_list)


def test_bad_league_is_visible_without_hiding_other_batch_items(internal):
    client,fake,headers=internal
    fake.json_document.side_effect=[RuntimeError('private capability must not be printed'),
                                   document(2,'matchups'),document(2,'markets')]
    r=client.get('/internal/pinnacle/league-batch?ids=1,2',headers=headers)
    assert r.json()['items'][0]=={'id':1,'error':'RuntimeError'}
    assert r.json()['items'][1]['markets']['http_status']==200
    assert 'private capability' not in r.text
    fake.stop.assert_awaited_once()


@pytest.mark.parametrize('stop',[RuntimeError('secret-capability'),{'status':'active'}])
def test_failed_stop_prevents_success_response(internal,stop):
    client,fake,headers=internal
    if isinstance(stop,Exception):fake.stop.side_effect=stop
    else:fake.stop.return_value=stop
    r=client.get('/internal/pinnacle/league-batch?ids=1',headers=headers)
    assert r.status_code==502 and r.json()['detail']=='STOP_FAILED:RuntimeError'
    assert 'secret-capability' not in r.text


def test_navigation_failure_still_stops_allocated_browser(internal):
    client,fake,headers=internal
    fake.json_document.side_effect=TimeoutError('source timeout')
    r=client.get('/internal/pinnacle/league-batch?ids=1',headers=headers)
    assert r.json()['items'][0]['error']=='TimeoutError'
    fake.stop.assert_awaited_once()


def test_leagues_endpoint_also_requires_complete_json_and_confirmed_stop(internal):
    client,fake,headers=internal
    fake.json_document.side_effect=None
    fake.json_document.return_value={'http_status':200,'body':'[]','truncated':False}
    assert client.get('/internal/pinnacle/leagues',headers=headers).status_code==200
    fake.json_document.assert_awaited_once_with('synthetic',collect.PIN_LEAGUES)
    fake.stop.assert_awaited_once()
    fake.stop.return_value={'status':'active'}
    r=client.get('/internal/pinnacle/leagues',headers=headers)
    assert r.status_code==502 and r.json()['detail']=='STOP_FAILED:RuntimeError'


@pytest.fixture
def raw_document(monkeypatch):
    b=BrowserUseBridge();s=Session('synthetic','ephemeral-only',None);b._sessions[s.id]=s
    response=SimpleNamespace(status=200,headers={'content-type':'application/json'},body=AsyncMock(return_value=b'[]'))
    page=SimpleNamespace(url='https://public.example/json',goto=AsyncMock(return_value=response))
    monkeypatch.setattr(session_leases,'renew',lambda *args:None)
    monkeypatch.setattr(browser_use,'validate_public_http_url',lambda url:url)
    monkeypatch.setattr(b,'_page',AsyncMock(return_value=page))
    return b,page,response,s


def test_full_document_exceeds_dom_cap_without_truncation(raw_document):
    b,page,response,s=raw_document
    body=json.dumps([{'large':'x'*900000,'name':'Café'}],ensure_ascii=False).encode()
    response.body.return_value=body
    r=asyncio.run(b.json_document(s.id,page.url))
    assert r['body'].encode()==body and r['truncated'] is False
    assert r['sha256']==hashlib.sha256(body).hexdigest()
    assert r['bytes_utf8']==len(body)
    assert s.last_network==[]


@pytest.mark.parametrize('change',['status','redirect','content_type','large','bad_json','object','nan','advertised_large'])
def test_document_fail_closed_on_status_redirect_schema_type_size(raw_document,change):
    b,page,response,s=raw_document
    url=page.url
    if change=='status':response.status=403
    elif change=='redirect':page.url='https://public.example/challenge'
    elif change=='content_type':response.headers['content-type']='text/html'
    elif change=='large':response.body.return_value=b' '* (5*1024*1024+1)
    elif change=='bad_json':response.body.return_value=b'[truncated'
    elif change=='object':response.body.return_value=b'{"unexpected":1}'
    elif change=='nan':response.body.return_value=b'[{"price":NaN}]'
    elif change=='advertised_large':response.headers['content-length']=str(5*1024*1024+1)
    with pytest.raises((ValueError,RuntimeError)):asyncio.run(b.json_document(s.id,url))
    if change in ('status','redirect','content_type','advertised_large'):response.body.assert_not_awaited()


def test_public_network_guard_is_not_bypassed(raw_document,monkeypatch):
    b,page,response,s=raw_document
    def reject(url):raise UnsafeURL('private destination')
    monkeypatch.setattr(browser_use,'validate_public_http_url',reject)
    with pytest.raises(UnsafeURL):asyncio.run(b.json_document(s.id,page.url))
    page.goto.assert_not_awaited()


class HTTPResponse:
    def __init__(self,payload):self.raw=json.dumps(payload).encode();self.status=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n=None):return self.raw[:n]


def payload():
    return {'items':[{'id':2117,'matchups':document(2117,'matchups'),
        'markets':document(2117,'markets')}],'transport':'BROWSER_JSON_DOCUMENT','session_stopped':True}


def test_collector_verifies_hash_identity_whole_document_and_stop(monkeypatch):
    monkeypatch.setenv('COLLECTOR_BRIDGE_TOKEN','synthetic-token');p=payload();calls=[]
    def open(req,timeout):calls.append((req,timeout));return HTTPResponse(p)
    monkeypatch.setattr(collect,'urlopen',open)
    rows=collect.fetch_pinnacle_batch_via_bridge([2117])
    assert rows[0]['err'] is None and rows[0]['matchups']==(200,b'[]')
    assert rows[0]['transport']=='browser_bridge_fallback'
    assert calls[0][1]==210 and 'league-batch?ids=2117' in calls[0][0].full_url


@pytest.mark.parametrize('field,value',[('sha256','a'*64),('truncated',True),('http_status',403),
    ('bytes_utf8',999),('final_url','https://evil.example/'),('body','[{"price":NaN}]')])
def test_invalid_fallback_document_never_becomes_a_quote(monkeypatch,field,value):
    monkeypatch.setenv('COLLECTOR_BRIDGE_TOKEN','synthetic-token');p=payload()
    p['items'][0]['markets'][field]=value
    monkeypatch.setattr(collect,'urlopen',lambda *args,**kwargs:HTTPResponse(p))
    assert collect.fetch_pinnacle_batch_via_bridge([2117])[0]['err']


@pytest.mark.parametrize('change',['active','wrong_id','duplicate','missing','not_dict'])
def test_fallback_envelope_identity_and_stop_are_required(monkeypatch,change):
    monkeypatch.setenv('COLLECTOR_BRIDGE_TOKEN','synthetic-token');p=payload()
    if change=='active':p['session_stopped']=False
    elif change=='wrong_id':p['items'][0]['id']=2
    elif change=='duplicate':p['items']*=2
    elif change=='missing':p['items']=[]
    elif change=='not_dict':p['items']=[None]
    monkeypatch.setattr(collect,'urlopen',lambda *args,**kwargs:HTTPResponse(p))
    with pytest.raises(RuntimeError):collect.fetch_pinnacle_batch_via_bridge([2117])


@pytest.mark.parametrize('ids',[[],[0],[True],[1,1],list(range(1,14))])
def test_collector_invalid_ids_rejected_before_io(monkeypatch,ids):
    monkeypatch.setattr(collect,'urlopen',lambda *args,**kwargs:pytest.fail('unexpected network'))
    with pytest.raises(ValueError):collect.fetch_pinnacle_batch_via_bridge(ids)


def test_missing_bridge_token_rejected_before_io(monkeypatch):
    monkeypatch.delenv('COLLECTOR_BRIDGE_TOKEN',raising=False)
    with pytest.raises(RuntimeError):collect.fetch_pinnacle_batch_via_bridge([2117])


def test_bridge_503_retries_are_bounded_audited_and_never_backdate_quotes(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from io import BytesIO
    from urllib.error import HTTPError
    monkeypatch.setenv('COLLECTOR_BRIDGE_TOKEN', 'synthetic-token')
    start = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
    clock = [start]
    waits = []
    def sleep(seconds):
        waits.append(seconds); clock[0] += timedelta(seconds=seconds)
    calls = []
    errors = []
    def open(req, timeout):
        calls.append(timeout)
        if len(calls) <= 3:
            exc = HTTPError(req.full_url, 503, 'warming', {}, BytesIO(b'private body'))
            errors.append(exc); raise exc
        return HTTPResponse(payload())
    monkeypatch.setattr(collect, 'urlopen', open)
    monkeypatch.setattr(collect.time, 'sleep', sleep)
    monkeypatch.setattr(collect, 'utcnow', lambda: clock[0])
    row = collect.fetch_pinnacle_batch_via_bridge([2117])[0]
    assert waits == [3, 10, 30] and calls == [210] * 4
    assert all(e.fp.closed for e in errors)
    assert [a['http_status'] for a in row['bridge_request_audit']] == [503, 503, 503, 200]
    assert 'private body' not in json.dumps(row['bridge_request_audit'])
    assert 'synthetic-token' not in json.dumps(row['bridge_request_audit'])
    assert row['captured_at'] == start + timedelta(seconds=43)
    model = {'home':'Home','away':'Away','league':'League','kickoff':start+timedelta(seconds=15)}
    quote = {'home':'Home','away':'Away','league':'League','start':model['kickoff']}
    assert not collect.exact_market_match(model, quote, row['captured_at'])


@pytest.mark.parametrize('code', [401, 403, 502, 503])
def test_bridge_failure_never_hides_auth_stop_failure_or_exhausted_503(monkeypatch, code):
    from io import BytesIO
    from urllib.error import HTTPError
    monkeypatch.setenv('COLLECTOR_BRIDGE_TOKEN', 'synthetic-token')
    calls = []; waits = []; audit = []
    def open(req, timeout):
        calls.append(timeout)
        raise HTTPError(req.full_url, code, 'failure', {}, BytesIO(b'private body'))
    monkeypatch.setattr(collect, 'urlopen', open)
    monkeypatch.setattr(collect.time, 'sleep', waits.append)
    with pytest.raises(HTTPError) as failure:
        collect.fetch_pinnacle_leagues_via_bridge(audit=audit)
    assert failure.value.code == code
    expected = 4 if code == 503 else 1
    assert len(calls) == len(audit) == expected
    assert waits == ([3, 10, 30] if code == 503 else [])
    assert all(a['http_status'] == code for a in audit)


@pytest.mark.parametrize('failure', [TimeoutError('unknown delivery'), ValueError('bad response')])
def test_bridge_does_not_retry_unknown_delivery_or_bad_body(monkeypatch, failure):
    monkeypatch.setenv('COLLECTOR_BRIDGE_TOKEN', 'synthetic-token')
    calls = []
    def open(req, timeout):
        calls.append(timeout)
        if isinstance(failure, TimeoutError):raise failure
        return HTTPResponse({'http_status':403,'body':'[]','truncated':False})
    monkeypatch.setattr(collect, 'urlopen', open)
    monkeypatch.setattr(collect.time, 'sleep', lambda seconds:pytest.fail('unexpected retry'))
    with pytest.raises((TimeoutError, RuntimeError)):
        collect.fetch_pinnacle_leagues_via_bridge()
    assert len(calls) == 1


@pytest.mark.parametrize('pair',[(200,b'[]'),(200,b'[{"id":1}]')])
def test_valid_empty_response_is_not_transport_failure(pair):
    assert collect.valid_pinnacle_response(pair)


@pytest.mark.parametrize('pair',[(403,b'[]'),(200,b'<html>challenge</html>'),(200,b'[null]'),(200,b'{}')])
def test_invalid_direct_document_requests_fallback(pair):
    assert not collect.valid_pinnacle_response(pair)


def test_frozen_actual_browser_documents_are_complete_hash_checked_and_not_price_snapshots():
    p=Path(__file__).parents[1]/'staging/pinnacle_transport/live_probe_20261001.json'
    probe=json.loads(p.read_text())
    assert probe['production_eligible'] is False
    assert probe['kind']=='LIVE_SOURCE_CAPABILITY_PROBE_NOT_PROSPECTIVE_PRICE_SNAPSHOT'
    for document in probe['documents'].values():
        raw=gzip.decompress(base64.b64decode(document['raw_gzip_base64']))
        assert len(raw)==document['raw_bytes']
        assert hashlib.sha256(raw).hexdigest()==document['raw_sha256']
        assert len(json.loads(raw))==document['rows']
        assert collect.valid_pinnacle_response((200,raw))
    assert probe['documents']['matchups']['raw_bytes']>500000
