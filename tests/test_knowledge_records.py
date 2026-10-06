from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import pytest
from knowledge_capture.store import Store
from knowledge_capture.knowledge_records import KnowledgeRecords, KnowledgeRecordError


def capture(store,body='原始中文资料正文',title='来源标题'):
    return store.ingest('https://example.org/source',capture_fn=lambda url,assets:{
        'title':title,'markdown':body,'status':'complete','warnings':[],
        'assets':[],'final_url':url})


def test_manual_markdown_revision_history_and_exact_old_versions(tmp_path):
    records=KnowledgeRecords(Store(tmp_path))
    first=records.create('人工知识','# 原文\n\n第一版内容',tags=['新增'],actor='user',idempotency_key='create-a')
    second=records.revise(first['record_id'],first['version_id'],markdown='# 原文\n\n修订内容',actor='agent:test',note='按新依据修订',idempotency_key='revise-a')
    assert second['metadata']['parent_version']==first['version_id']
    assert records.read(first['record_id'],first['version_id'])['markdown']==first['markdown']
    assert records.read(first['record_id'])['markdown']==second['markdown']
    assert [event['event'] for event in records.history(first['record_id'])['events']]==['created','revised']
    assert len(records.versions(first['record_id'])['versions'])==2
    assert not (tmp_path/'sources').exists()


def test_source_overlay_references_and_refresh_never_mutate_captured_source(tmp_path):
    store=Store(tmp_path);original=capture(store);records=KnowledgeRecords(store)
    path=Path(original['path']);before=path.read_bytes()
    source=records.read(original['source_id'])
    assert source['version_id']==original['version_id'] and source['kind']=='source'
    revised=records.revise(original['source_id'],original['version_id'],markdown='人工修订与原文分层',idempotency_key='overlay')
    assert revised['kind']=='source_overlay' and revised['source_version']==original['version_id']
    assert revised['metadata']['references'][0]['markdown_sha256']==hashlib.sha256(before).hexdigest()
    assert path.read_bytes()==before
    refreshed=capture(store,'网站更新后的原始资料','来源新标题')
    current=records.read(original['source_id'])
    assert current['markdown']=='人工修订与原文分层'
    assert current['stale'] is True and current['latest_source_version']==refreshed['version_id']
    assert records.read(original['source_id'],original['version_id'])['markdown']==source['markdown']
    assert path.read_bytes()==before


def test_source_expiration_does_not_resurrect_original_in_shared_search(tmp_path):
    store=Store(tmp_path);captured=capture(store,'原文检索词');records=KnowledgeRecords(store)
    note=records.create('笔记','相同检索词',tags=['新增'])
    assert len(records.search('检索词')['results'])==2
    expired=records.annotate(captured['source_id'],captured['version_id'],tags=['过期'],status='expired',idempotency_key='expire')
    assert expired['metadata']['status']=='expired'
    assert [r['record_id'] for r in records.search('检索词')['results']]==[note['record_id']]
    assert len(records.search('检索词',include_expired=True)['results'])==2
    assert records.list(include_expired=True,tags=['过期'])['results'][0]['record_id']==captured['source_id']
    active=records.annotate(captured['source_id'],expired['version_id'],status='active',tags=['修订'])
    assert active['metadata']['status']=='active'
    assert len(records.search('检索词')['results'])==2


def test_optimistic_lock_and_concurrent_revision_have_one_winner(tmp_path):
    records=KnowledgeRecords(Store(tmp_path));first=records.create('标题','最初正文')
    def revise(index):
        try:
            return records.revise(first['record_id'],first['version_id'],markdown=f'正文{index}',idempotency_key=f'r{index}')
        except KnowledgeRecordError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(revise,[1,2]))
    assert sum(isinstance(item,dict) for item in results)==1
    error=next(item for item in results if isinstance(item,KnowledgeRecordError))
    assert error.code=='version_conflict'
    assert error.current_version==records.read(first['record_id'])['version_id']
    assert len(records.history(first['record_id'])['events'])==2


def test_idempotency_same_request_replays_original_and_changed_request_conflicts(tmp_path):
    records=KnowledgeRecords(Store(tmp_path))
    first=records.create('标题','正文',idempotency_key='stable-key')
    records.revise(first['record_id'],first['version_id'],markdown='新正文')
    replay=records.create('标题','正文',idempotency_key='stable-key')
    assert replay==first
    with pytest.raises(KnowledgeRecordError) as error:
        records.create('标题','另一正文',idempotency_key='stable-key')
    assert error.value.code=='idempotency_conflict'
    assert records.list()['total']==1


def test_reference_versions_explicit_external_unverified_and_no_source_fabrication(tmp_path):
    store=Store(tmp_path);source=capture(store);records=KnowledgeRecords(store)
    note=records.create('引用','正文',references=[{'source_id':source['source_id'],'version_id':source['version_id']},
                                               {'url':'https://other.example/report','title':'外部报告'}])
    refs=note['metadata']['references']
    assert refs[0]['version_id']==source['version_id']
    assert refs[1]['evidence_status']=='external_reference_not_captured'
    for refs in [[{'source_id':source['source_id']}],[{'source_id':'a'*24,'version_id':'b'*24}],[{'url':'file:///etc/passwd'}],[{'url':'https://user:secret@example.org'}]]:
        with pytest.raises(KnowledgeRecordError):records.create('坏引用','正文',references=refs)


def test_pagination_filters_and_changed_snapshot_are_explicit(tmp_path):
    records=KnowledgeRecords(Store(tmp_path))
    for index in range(3):records.create(f'标题{index}','公共关键词',tags=['新增'])
    first=records.search('关键词',limit=1,tags=['新增'])
    second=records.search('关键词',limit=1,tags=['新增'],cursor=first['next_cursor'])
    assert first['results'][0]['record_id']!=second['results'][0]['record_id']
    records.create('新增条目','公共关键词',tags=['新增'])
    with pytest.raises(KnowledgeRecordError) as error:
        records.search('关键词',limit=1,tags=['新增'],cursor=second['next_cursor'])
    assert error.value.code=='cursor_stale'


def test_tamper_and_symlink_detected_before_read_or_idempotent_success(tmp_path):
    store=Store(tmp_path);records=KnowledgeRecords(store)
    first=records.create('标题','正文',idempotency_key='a')
    file=tmp_path/'records'/first['record_id']/'versions'/first['version_id']/'content.md'
    file.write_text('篡改',encoding='utf-8')
    for action in [lambda:records.read(first['record_id']),lambda:records.create('标题','正文',idempotency_key='a')]:
        with pytest.raises(KnowledgeRecordError) as error:action()
        assert error.value.code=='integrity_error'
    file.unlink();file.symlink_to(tmp_path/'index.sqlite3')
    with pytest.raises(KnowledgeRecordError):records.read(first['record_id'])


def test_failed_mutation_commits_neither_head_event_nor_idempotency(tmp_path,monkeypatch):
    store=Store(tmp_path);records=KnowledgeRecords(store)
    first=records.create('标题','原始正文')
    original=records._write
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('simulated crash before database commit')
    monkeypatch.setattr(records,'_write',fail)
    with pytest.raises(RuntimeError):records.revise(first['record_id'],first['version_id'],markdown='未提交',idempotency_key='failed')
    assert records.read(first['record_id'])['markdown']=='原始正文'
    assert len(records.history(first['record_id'])['events'])==1
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM knowledge_record_idempotency WHERE key=?',('failed',)).fetchone()[0]==0


def test_uncommitted_files_are_auditable_not_successful_records(tmp_path,monkeypatch):
    records=KnowledgeRecords(Store(tmp_path))
    original=records._write
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('crash')
    monkeypatch.setattr(records,'_write',fail)
    with pytest.raises(RuntimeError):records.create('未提交','尚未提交的正文',idempotency_key='crash')
    assert records.list()['total']==0
    audit=records.audit()
    assert audit['ok'] is False and len(audit['orphan_versions'])==1 and audit['invalid_versions']==[]


def test_revision_retry_replays_before_stale_expected_version_check(tmp_path):
    records=KnowledgeRecords(Store(tmp_path));first=records.create('标题','正文')
    arguments={'record_id':first['record_id'],'expected_version':first['version_id'],'markdown':'修订后','idempotency_key':'revision-1'}
    second=records.revise(**arguments)
    assert records.revise(**arguments)==second
    assert len(records.versions(first['record_id'])['versions'])==2


def test_status_and_tags_writes_require_the_effective_current_head(tmp_path):
    store=Store(tmp_path);source=capture(store);records=KnowledgeRecords(store)
    overlay=records.annotate(source['source_id'],source['version_id'],tags=['修订'])
    with pytest.raises(KnowledgeRecordError) as error:
        records.annotate(source['source_id'],source['version_id'],status='expired')
    assert error.value.code=='version_conflict' and error.value.current_version==overlay['version_id']
    with pytest.raises(KnowledgeRecordError):records.annotate(source['source_id'],overlay['version_id'],status='deleted')
    assert records.read(source['source_id'])['metadata']['status']=='active'
