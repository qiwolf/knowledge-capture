import copy
from pathlib import Path
import pytest
from knowledge_capture.store import Store
from knowledge_capture.processing import Processor
from knowledge_capture.interest_inference import InterestInference,InterestInferenceError

class Analyze:
    identity={'model':'fixture','provider':'test'}
    def complete_json(self,system,payload):
        line=payload['lines'][0]
        return {'summary':[{'text':'说明','evidence':[{'start_line':1,'end_line':1,'quote':line['text']}]}],
            'key_points':[],'questions':[],'topics':[{'name':line['text'][:8],'reason':'具体学习内容','evidence':[{'start_line':1,'end_line':1,'quote':line['text']}]}]}
class Infer:
    identity={'model':'contract-fixture','provider':'test'}
    def __init__(self,mutate=None):self.calls=0;self.mutate=mutate
    def complete_json(self,system,payload):
        self.calls+=1
        evidence=[]
        for source in payload['sources']:
            cite=source['topics'][0]['evidence'][0]
            evidence.append({'source_id':source['source_id'],'version_id':source['version_id'],**{k:cite[k] for k in ['start_line','end_line','quote']}})
        result={'candidates':[{'name':'Python基础编程学习','scope':'学习Python基础表达式、流程控制及容器操作，不推断其他语言或高级领域兴趣。',
            'subtopics':[s['topics'][0]['name'] for s in payload['sources']], 'evidence':evidence}]}
        if self.mutate:self.mutate(result)
        return result

def add(store,i,text,origin='user'):
    def capture(url,directory):return {'title':'资料','markdown':text,'original_url':url,'final_url':url,'author':None,'published_at':None,'assets':[],'status':'complete','warnings':[]}
    result=store.ingest(f'https://example.org/{i}',origin=origin,capture_fn=capture)
    Processor(store).analyze(result['source_id'],Analyze(),infer_common=False)
    return result
@pytest.fixture
def ready(tmp_path):
    store=Store(tmp_path)
    docs=[add(store,i,t) for i,t in enumerate(['Python数字与字符串表达式入门。','Python条件和循环流程控制。','Python列表和字典数据结构。'])]
    return store,docs

def test_three_different_subtopics_infer_bounded_candidate_and_cache(ready):
    store,docs=ready
    assert len(Processor(store).interests())==3
    inference=InterestInference(store);client=Infer()
    output=inference.infer(client)
    assert output['candidates'][0]['state']=='candidate'
    assert output['candidates'][0]['eligible_for_discovery'] is False
    assert len({e['source_id'] for e in output['candidates'][0]['evidence']})==3
    assert inference.infer(client)['cached'] is True
    assert client.calls==1
    assert inference.valid_records()[0]['id']==output['id']
    assert len(Processor(store).interest_records())==3
    assert next(t for t in Processor(store).interests() if t['name']=='Python基础编程学习')['user_source_count']==3

def test_discovery_and_mirrors_cannot_supply_three_documents(tmp_path):
    store=Store(tmp_path)
    add(store,1,'Python学习相同内容。');add(store,2,'Python学习相同内容。')
    add(store,3,'Python补充不同内容。','discovery')
    client=Infer()
    assert InterestInference(store).infer(client)['status']=='insufficient_sources'
    assert client.calls==0

def test_dependency_change_invalidates_without_modifying_history(ready):
    store,docs=ready;inference=InterestInference(store)
    output=inference.infer(Infer());path=store.root/'interest_inferences'/output['id']/'inference.json';old=path.read_bytes()
    source=Path(store.read(docs[0]['source_id'])['path'])/'content.md';source.write_text(source.read_text()+'\n新增正文')
    assert inference.valid_records()==[]
    assert path.read_bytes()==old

@pytest.mark.parametrize('mutate',[
 lambda r:r['candidates'][0]['evidence'][0].update(quote='伪造原文摘录'),
 lambda r:r['candidates'][0].update(evidence=r['candidates'][0]['evidence'][:2]),
 lambda r:r['candidates'][0].update(subtopics=['不存在的细主题'])])
def test_invalid_candidates_not_published_or_retried(ready,mutate):
    store,_=ready;inference=InterestInference(store);client=Infer(mutate)
    with pytest.raises(InterestInferenceError):inference.infer(client)
    assert inference.valid_records()==[]
    with pytest.raises(InterestInferenceError,match='相同输入'):inference.infer(client)
    assert client.calls==1

def test_model_returning_empty_is_valid_no_forced_interest(ready):
    store,_=ready;inference=InterestInference(store)
    assert inference.infer(Infer(lambda r:r.update(candidates=[])))['candidates']==[]


def test_automatic_analysis_creates_stable_candidate_and_respects_feedback(tmp_path):
    store=Store(tmp_path);processor=Processor(store)
    class Combined(Analyze):
        def __init__(self):self.infer=Infer();self.analysis_calls=0
        def complete_json(self,system,payload):
            if 'sources' in payload:return self.infer.complete_json(system,payload)
            self.analysis_calls+=1
            return super().complete_json(system,payload)
    client=Combined()
    for i,text in enumerate(['Python数值和字符串基础。','Python分支循环流程控制。','Python容器和字典操作。']):
        source=add(store,i,text)
        result=processor.analyze(source['source_id'],client)
        if i<2:assert result['interest_inference']['status']=='insufficient_sources'
    assert client.analysis_calls==3 and client.infer.calls==1
    candidate=next(t for t in processor.interests() if t.get('inference_ids'))
    assert candidate['name']=='Python基础编程学习' and candidate['user_source_count']==3
    assert candidate['state']=='candidate' and not candidate['eligible_for_discovery']
    assert len(candidate['evidence'])==3
    identifier=candidate['id']
    assert processor.feedback(identifier,'followed')['eligible_for_discovery']
    assert processor.feedback(identifier,'paused')['state']=='paused'
    assert processor.feedback(identifier,'closed')['state']=='closed'
    processor.analyze(source['source_id'],client)
    assert client.infer.calls==1
    assert next(t for t in processor.interests() if t['id']==identifier)['state']=='closed'
    assert all(t['name']!='Python基础编程学习' for r in processor.interest_records() for t in r['topics'])


def test_automatic_inference_failure_does_not_mark_completed_analysis_failed(ready):
    store,docs=ready
    client=Analyze()  # Deliberately cannot speak the inference contract.
    result=Processor(store).analyze(docs[0]['source_id'],client)
    assert result['status']=='complete'
    assert result['interest_inference']['status']=='failed'
    assert Processor(store).read(result['id'])['record']['status']=='complete'


def test_explicit_failed_retry_retains_original_and_caches_latest(ready):
    from contextlib import closing
    store,_=ready;inference=InterestInference(store)
    with pytest.raises(InterestInferenceError):inference.infer(Infer(lambda r:r.update(candidates='bad')))
    with closing(store._connect()) as db:
        original=dict(db.execute('SELECT * FROM interest_inference_runs').fetchone())
    output=inference.retry_failed(original['id'],Infer())
    assert output['retry_of']==original['id'] and output['id']!=original['id']
    with closing(store._connect()) as db:
        assert dict(db.execute('SELECT * FROM interest_inference_runs WHERE id=?',(original['id'],)).fetchone())==original
        assert db.execute('SELECT COUNT(*) FROM interest_inference_runs').fetchone()[0]==2
    client=Infer()
    assert inference.infer(client)['id']==output['id'] and client.calls==0
    assert inference.valid_records()[0]['id']==output['id']
    with pytest.raises(InterestInferenceError):inference.retry_failed(original['id'],Infer())
    with pytest.raises(InterestInferenceError):inference.retry_failed(output['id'],Infer())


def test_running_attempt_cannot_be_explicitly_retried(ready):
    from contextlib import closing
    store,_=ready;inference=InterestInference(store);key=inference._inputs()[2]
    with closing(store._connect()) as db,db:
        db.execute('INSERT INTO interest_inference_runs VALUES (?,?,?,NULL,?,NULL)',('running',key,'running','2026-10-06'))
    with pytest.raises(InterestInferenceError):inference.retry_failed('running',Infer())


def test_discovery_analysis_cannot_trigger_inference_for_existing_user_corpus(ready):
    store, _ = ready
    discovered = add(store, 90, 'Python自动发现资料。', origin='discovery')
    class AnalysisOnly(Analyze):
        def complete_json(self, system, payload):
            assert 'sources' not in payload, 'discovery analysis must not call inference'
            return super().complete_json(system, payload)
    result = Processor(store).analyze(discovered['source_id'], AnalysisOnly())
    assert result['interest_inference'] == {'status': 'skipped', 'model_calls': 0}
    assert InterestInference(store).valid_records() == []
