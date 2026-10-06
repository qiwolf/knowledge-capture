import json
from contextlib import closing
from unittest.mock import patch
import pytest
from knowledge_capture.store import Store
from knowledge_capture.settings import Settings, SettingsError
from knowledge_capture.knowledge_records import KnowledgeRecords, KnowledgeRecordError
from knowledge_capture.retrieval import Retriever, EmbeddingClient, RerankClient, RetrievalError
from workbench_ui_fixture import fixture_capture, BASE_URL


class FixedVectors:
    """Hand-coded test vectors, not measurements of a production embedding model."""
    def __init__(self): self.calls=[]
    def embed(self,texts):
        self.calls.append(texts)
        result=[]
        for text in texts:
            if '版本核对' in text or '确认当前固件' in text: index=0
            elif '配置备份' in text or '保存设置副本' in text: index=1
            elif '兼容性检查' in text or '插件能否运行' in text: index=2
            elif '回滚演练' in text or '恢复旧系统' in text: index=3
            else: index=4
            result.append([float(index==i) for i in range(5)])
        return result


def configured(tmp_path):
    store=Store(tmp_path)
    Settings(store).save_retrieval('embedding','https://embed.example/v1/embeddings','fixture-model','private-key')
    return store


def test_settings_optional_private_and_cross_origin_key_clear(tmp_path):
    settings=Settings(tmp_path)
    assert not settings.public_view()['retrieval']['embedding']['enabled']
    view=settings.save_retrieval('embedding','https://one.example/embeddings','模型','private-key')
    assert view['retrieval']['embedding']['key_configured']
    assert 'private-key' not in json.dumps(view)
    settings.save_retrieval('embedding','https://two.example/embeddings','模型')
    assert not settings.public_view()['retrieval']['embedding']['key_configured']
    settings.save_retrieval('embedding',enabled=False)
    assert not settings.public_view()['retrieval']['embedding']['enabled']
    with pytest.raises(SettingsError):settings.save_retrieval('rerank','http://public.example/rerank','x')


def test_disabled_no_requests_and_shared_keyword_search(tmp_path):
    store=Store(tmp_path);record=KnowledgeRecords(store).create('测试知识','中文关键词')
    retrieval=Retriever(store)
    result=retrieval.search('关键词')
    assert result['results'][0]['record_id']==record['record_id']
    assert result['retrieval']=={'mode':'lexical','degraded':False,'reasons':[], 'index_state':'disabled','semantic_is_evidence':False,'candidate_limit':None,'candidate_truncated':False}
    assert retrieval.reindex()['status']=='disabled'


def test_existing_chinese_fixture_offline_metrics(tmp_path):
    store=configured(tmp_path)
    relevant=[]
    for suffix in ('1','2','3','4'):
        relevant.append(store.ingest(BASE_URL+suffix,capture_fn=fixture_capture)['source_id'])
    vector=FixedVectors();retriever=Retriever(store,embedding_client=vector)
    assert retriever.reindex(idempotency_key='fixture')['indexed_documents']==4
    queries=['确认当前固件','保存设置副本','插件能否运行','恢复旧系统']
    hits=[];recalls=[];rr=[]
    for query,target in zip(queries,relevant):
        result=retriever.search(query,limit=2)
        ids=[row['record_id'] for row in result['results']]
        hits.append(float(target in ids));recalls.append(float(target in ids))
        rr.append(1/(ids.index(target)+1) if target in ids else 0)
        assert all(row['version_id'] and row['similarity_is_truth'] is False for row in result['results'])
    metrics={'Hit@2':sum(hits)/4,'Recall@2':sum(recalls)/4,'MRR@2':sum(rr)/4}
    assert metrics=={'Hit@2':1.0,'Recall@2':1.0,'MRR@2':1.0}
    assert len(vector.calls)>=5


def test_version_changes_and_expired_never_use_old_vector(tmp_path):
    store=configured(tmp_path);records=KnowledgeRecords(store)
    first=records.create('配置备份','保存数据')
    retriever=Retriever(store,embedding_client=FixedVectors());retriever.reindex()
    revised=records.revise(first['record_id'],first['version_id'],title='另一个主题',markdown='新内容')
    result=retriever.search('保存设置副本')
    assert result['results']==[] and 'index_stale' in result['retrieval']['reasons']
    records.annotate(first['record_id'],revised['version_id'],status='expired')
    assert retriever.search('另一个主题')['results']==[]


def test_model_identity_new_space_missing_not_old_vectors(tmp_path):
    store=configured(tmp_path);KnowledgeRecords(store).create('配置备份','普通正文')
    first=Retriever(store,embedding_client=FixedVectors());assert first.reindex()['status']=='complete'
    Settings(store).save_retrieval('embedding','https://embed.example/v1/embeddings','different-model')
    result=Retriever(store,embedding_client=FixedVectors()).search('保存设置副本')
    assert result['results']==[] and result['retrieval']['index_state']=='missing'


def test_dimension_mismatch_and_network_failure_degrade_to_keyword(tmp_path):
    store=configured(tmp_path);KnowledgeRecords(store).create('配置备份','备份关键词')
    retriever=Retriever(store,embedding_client=FixedVectors());retriever.reindex()
    class Wrong:
        def embed(self,texts):return [[1,0]]*len(texts)
    result=Retriever(store,embedding_client=Wrong()).search('备份')
    assert result['results'] and 'dimension_mismatch' in result['retrieval']['reasons']
    class Failed:
        def embed(self,texts):raise RuntimeError('private upstream error')
    result=Retriever(store,embedding_client=Failed()).search('备份')
    assert result['results'] and result['retrieval']['degraded']
    assert 'private' not in json.dumps(result)


def test_failed_rebuild_keeps_completed_generation_and_idempotency(tmp_path):
    store=configured(tmp_path);KnowledgeRecords(store).create('配置备份','正文')
    vectors=FixedVectors();retriever=Retriever(store,embedding_client=vectors)
    first=retriever.reindex(idempotency_key='index-key')
    calls=len(vectors.calls)
    assert retriever.reindex(idempotency_key='index-key')==first and len(vectors.calls)==calls
    class Bad:
        def embed(self,texts):return []
    failed=Retriever(store,embedding_client=Bad()).reindex(idempotency_key='fail-key')
    assert failed['status']=='failed'
    assert retriever.search('保存设置副本')['results']
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM retrieval_runs WHERE status='complete'").fetchone()[0]==1


def test_index_detects_changes_during_embed(tmp_path):
    store=configured(tmp_path);records=KnowledgeRecords(store);first=records.create('配置备份','正文')
    class Changing(FixedVectors):
        def embed(self,texts):
            records.revise(first['record_id'],first['version_id'],markdown='新的正文')
            return super().embed(texts)
    result=Retriever(store,embedding_client=Changing()).reindex()
    assert result['status']=='failed' and result['error_code']=='source_changed'


def test_rerank_bounded_candidates_and_reject_invented_index(tmp_path):
    store=Store(tmp_path);records=KnowledgeRecords(store)
    records.create('测试甲','关键词');records.create('测试乙','关键词')
    Settings(store).save_retrieval('rerank','https://rerank.example/v1/rerank','fixed')
    class Good:
        def rerank(self,query,documents):
            assert len(documents)==2
            return [{'index':1,'relevance_score':0.9},{'index':0,'relevance_score':0.1}]
    good=Retriever(store,rerank_client=Good()).search('关键词')
    assert 'rerank' in good['retrieval']['mode'] and not good['retrieval']['degraded']
    class Bad:
        def rerank(self,query,documents):return [{'index':9,'relevance_score':1}]*2
    bad=Retriever(store,rerank_client=Bad()).search('关键词')
    assert len(bad['results'])==2 and 'rerank_invalid' in bad['retrieval']['reasons']


def test_explicit_embedding_and_rerank_payloads():
    config={'endpoint':'https://service.example/v1/embeddings','model':'fixed','api_key':'secret'}
    with patch('knowledge_capture.retrieval.Connector') as connector:
        connector.return_value.call.return_value={'data':[{'index':1,'embedding':[0,1]},{'index':0,'embedding':[1,0]}]}
        assert EmbeddingClient(config).embed(['甲','乙'])==[[1.,0.],[0.,1.]]
        payload=connector.return_value.call.call_args.args[0]
        assert payload=={'model':'fixed','input':['甲','乙'],'encoding_format':'float'}
        connector.return_value.call.return_value={'results':[{'index':0,'relevance_score':1}]}
        RerankClient(config).rerank('问',['文'])
        assert connector.return_value.call.call_args.args[0]=={'model':'fixed','query':'问','documents':['文'],'top_n':1}
