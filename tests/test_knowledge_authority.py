from pathlib import Path
import pytest
from knowledge_capture.knowledge_records import KnowledgeRecords
from knowledge_capture.processing import Processor, AnalysisError
from knowledge_capture.wiki import Wiki, WikiError
from knowledge_capture import evidence_api
from test_portable import library
from test_knowledge_mcp import invoke, body, fingerprint
from test_wiki import Analyzer


@pytest.mark.parametrize('change',['expire','revise'])
def test_maintenance_authority_gates_legacy_search_analysis_and_wiki(tmp_path,change):
    store,source,analysis,topic,wiki=library(tmp_path)
    sid=source['source_id']; original=Path(source['path']).read_bytes()
    records=KnowledgeRecords(store)
    if change=='expire':
        current=records.annotate(sid,source['version_id'],status='expired',tags=['过期'])
    else:
        current=records.revise(sid,source['version_id'],markdown='人工修订：旧备份建议需要重新核实。')
    authority=store.source_knowledge_state(sid)
    assert not authority['source_eligible']
    assert authority['current_knowledge_version']==current['version_id']
    assert store.search('备份')==[]
    assert Processor(store).interest_records()==[]
    assert Processor(store)._topic_vocabulary()[0]==[]
    with pytest.raises(AnalysisError) as error:Processor(store).analyze(sid,Analyzer())
    assert error.value.code=='knowledge_inactive'
    assert Wiki(store).read(topic)['status']=='needs_review'
    assert Wiki(store).read(topic)['stale'] is True
    with pytest.raises(WikiError):Wiki(store).build(topic,client=object())
    assert evidence_api.read_analysis(store,analysis['id'])['stale'] is True
    historic=evidence_api.read_wiki(store,topic,wiki['version_id'])
    assert historic['status']=='needs_review' and historic['stale']
    assert Path(source['path']).read_bytes()==original
    # Older source-only MCP remains read-only and can return explicit history,
    # but cannot advertise it as current usable knowledge.
    before=fingerprint(store.root)
    assert body(invoke(store,'knowledge_search',{'query':'备份'}))['results']==[]
    read=body(invoke(store,'knowledge_read_source',{'source_id':sid,'version':source['version_id']}))
    assert read['source']['version_id']==source['version_id']
    assert read['is_current_knowledge_evidence'] is False
    assert read['knowledge_authority']['reason'] in {'knowledge_expired','knowledge_revised'}
    assert before==fingerprint(store.root)


def test_labels_only_overlay_keeps_unchanged_source_evidence_and_restoration(tmp_path):
    store,source,_,topic,_=library(tmp_path)
    records=KnowledgeRecords(store);sid=source['source_id']
    tagged=records.annotate(sid,source['version_id'],tags=['新增','运维'])
    assert store.source_knowledge_state(sid)['source_eligible']
    assert store.search('备份')
    assert Wiki(store).read(topic)['stale'] is False
    expired=records.annotate(sid,tagged['version_id'],status='expired')
    assert Wiki(store).read(topic)['status']=='needs_review'
    records.annotate(sid,expired['version_id'],status='active')
    assert store.source_knowledge_state(sid)['source_eligible']
    assert Wiki(store).read(topic)['status']=='complete'


def test_new_notes_are_searchable_but_not_fabricated_source_evidence(tmp_path):
    store,source,_,_,_=library(tmp_path)
    records=KnowledgeRecords(store)
    note=records.create('维护笔记','新笔记关键词')
    assert records.search('新笔记关键词')['results'][0]['record_id']==note['record_id']
    assert store.search('新笔记关键词')==[]
    assert all(item['source_id']!=note['record_id'] for item in Processor(store).interest_records())
    assert not store.source_knowledge_state(note['record_id'])['source_eligible']


def test_expiration_during_analysis_prevents_publication(tmp_path):
    store,source,_,_,_=library(tmp_path)
    class Expiring(Analyzer):
        def complete_json(self,system,payload):
            KnowledgeRecords(store).annotate(source['source_id'],source['version_id'],status='expired')
            return super().complete_json(system,payload)
    with pytest.raises(AnalysisError) as error:Processor(store).analyze(source['source_id'],Expiring())
    assert error.value.code=='knowledge_inactive'


def test_context_alerts_reject_inactive_sources_and_keep_flagged_history(tmp_path):
    from knowledge_capture.context_alerts import ContextAlerts, ContextError
    from test_context_alerts import Model
    store,source,_,_,_=library(tmp_path)
    context=ContextAlerts(store);context.set_fact('路由器','版本','待核实',status='confirmed')
    result=context.analyze([source['source_id']],client=Model())
    snapshot=evidence_api.read_alert(store,result['id'])['sources'][source['source_id']]['markdown']
    records=KnowledgeRecords(store);records.annotate(source['source_id'],source['version_id'],status='expired')
    with pytest.raises(ContextError) as error:context.analyze([source['source_id']],client=object())
    assert error.value.code=='knowledge_inactive'
    with pytest.raises(ContextError) as error:context.analyze(client=object())
    assert error.value.code=='sources_missing'
    old=context.read(result['id'])
    assert old['stale'] and old['is_current_knowledge_evidence'] is False
    assert old['record']['alerts'][0]['actionable'] is False
    assert old['knowledge_authority'][source['source_id']]['reason']=='knowledge_expired'
    assert evidence_api.read_alert(store,result['id'])['sources'][source['source_id']]['markdown']==snapshot


def test_context_default_scan_skips_expired_but_retains_active_sources(tmp_path):
    from knowledge_capture.context_alerts import ContextAlerts
    from test_context_alerts import Model, capture
    from knowledge_capture.store import Store
    store=Store(tmp_path)
    expired=store.ingest('https://example.org/old',capture_fn=capture('旧资料应被排除，不进入模型。'))
    active=store.ingest('https://example.org/current',capture_fn=capture('当前有效资料可供背景关联核实。'))
    KnowledgeRecords(store).annotate(expired['source_id'],expired['version_id'],status='expired')
    context=ContextAlerts(store);context.set_fact('设备','版本','待核实',status='confirmed')
    class Checking(Model):
        def complete_json(self,system,payload):
            assert [source['source_id'] for source in payload['sources']]==[active['source_id']]
            return super().complete_json(system,payload)
    assert context.analyze(client=Checking())['status']=='complete'
