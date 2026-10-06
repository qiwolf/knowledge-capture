import threading
from types import SimpleNamespace

import pytest
import requests

from knowledge_capture.store import Store
from knowledge_capture.gateway import create_server
from knowledge_capture.knowledge_api import KnowledgeAPI
from test_gateway import done
from test_wiki import capture


@pytest.fixture
def api_gateway(tmp_path):
    store = Store(tmp_path / 'library')
    server = create_server(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    session.headers['Authorization'] = 'Bearer ' + (store.root / '.api-token').read_text()
    yield SimpleNamespace(store=store, server=server, session=session, url=f'http://127.0.0.1:{server.server_port}')
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    session.close()


def test_rest_note_revision_label_history_and_conflicts(api_gateway):
    g = api_gateway
    url = g.url + '/api/v1/knowledge'
    body = {'kind':'note','title':'中文备份知识','markdown':'备份测试材料','tags':['运维']}
    assert g.session.post(url,json=body).status_code == 400
    headers={'Idempotency-Key':'create-1'}
    response = g.session.post(url,json=body,headers=headers)
    assert response.status_code == 201, response.text
    first = response.json()
    assert g.session.post(url,json=body,headers=headers).json() == first
    assert g.session.post(url,json={**body,'title':'changed'},headers=headers).status_code == 409
    rid=first['record_id']; original=first['version_id']
    revised = g.session.post(url+'/'+rid+'/revisions',json={'expected_version':original,'markdown':'修订内容'},headers={'Idempotency-Key':'revise-1'})
    assert revised.status_code == 200, revised.text
    second=revised.json()
    stale=g.session.post(url+'/'+rid+'/labels',json={'expected_version':original,'status':'expired'},headers={'Idempotency-Key':'label-old'})
    assert stale.status_code == 409 and stale.json()['error']['code']=='version_conflict'
    expired=g.session.post(url+'/'+rid+'/labels',json={'expected_version':second['version_id'],'status':'expired'},headers={'Idempotency-Key':'label-1'})
    assert expired.status_code == 200, expired.text
    assert g.session.get(url,params={'tags':'运维'}).json()['results']==[]
    assert len(g.session.get(url,params={'tags':'运维','include_expired':'true'}).json()['results'])==1
    assert '备份测试材料' in g.session.get(url+'/'+rid,params={'version':original}).json()['markdown']
    history=g.session.get(url+'/'+rid+'/versions',params={'limit':1}).json()
    assert len(history['versions'])==1 and history['next_cursor']
    assert len(g.session.get(url+'/'+rid+'/versions',params={'limit':1,'cursor':history['next_cursor']}).json()['versions'])==1
    assert g.session.get(url+'/'+('a'*32)).status_code==404
    schema=g.session.get(g.url+'/api/v1/openapi.json').json()
    assert schema['security'] and schema['paths']['/knowledge/{record_id}/refresh']['post']['requestBody']


def test_rest_capture_idempotency_and_refresh_commit_guard(api_gateway, monkeypatch):
    g=api_gateway
    source=g.store.ingest('https://example.com/api',capture_fn=capture('原文证据'))
    pending=[]
    monkeypatch.setattr(g.server.executor,'submit',lambda fn,*args: pending.append((fn,args)))
    payload={'expected_version':source['version_id']}
    url=g.url+'/api/v1/knowledge/'+source['source_id']+'/refresh'
    response=g.session.post(url,json=payload,headers={'Idempotency-Key':'refresh-1'})
    assert response.status_code==202, response.text
    assert len(pending)==1
    api=KnowledgeAPI(g.store)
    api.post('/api/v1/knowledge/'+source['source_id']+'/labels',{'expected_version':source['version_id'],'tags':['人工确认']},'edit-overlay')
    monkeypatch.setattr(g.server,'configuration',lambda:None)
    original_ingest=g.store.ingest
    monkeypatch.setattr(g.store,'ingest',lambda **kwargs: original_ingest(**kwargs,capture_fn=capture('新版本')))
    fn,args=pending.pop();fn(*args)
    result=done(g,response.json()['id'])
    assert result['status']=='failed' and result['error']['code']=='version_conflict',result
    assert g.store.read(source['source_id'])['metadata']['version_id']==source['version_id']
    replay=g.session.post(url,json=payload,headers={'Idempotency-Key':'refresh-1'})
    assert replay.status_code==202 and replay.json()['id']==result['id']
    assert not pending
    assert g.session.post(url,json=payload,headers={'Idempotency-Key':'refresh-2'}).status_code==409


def test_retrieval_api_delegates_and_reindex_requires_key(tmp_path,monkeypatch):
    from knowledge_capture.retrieval import Retriever
    from knowledge_capture.knowledge_records import KnowledgeRecordError
    calls=[]
    monkeypatch.setattr(Retriever,'search',lambda self,query,**kwargs: calls.append((query,kwargs)) or {'results':[],'next_cursor':None,'total':0,'retrieval':{'mode':'lexical','degraded':False}})
    api=KnowledgeAPI(Store(tmp_path/'retrieval'))
    code,result=api.get('/api/v1/knowledge?query=中文&tags=运维&limit=3')
    assert code==200 and result['retrieval']['mode']=='lexical'
    assert calls[0][0]=='中文' and calls[0][1]['tags']==['运维']
    with pytest.raises(KnowledgeRecordError): api.post('/api/v1/retrieval/reindex',{},None)
    code,result=api.post('/api/v1/retrieval/reindex',{},'reindex-1')
    assert code==200 and result['status']=='disabled'
