from contextlib import closing
from pathlib import Path
import hashlib
import json
import pytest

from knowledge_capture.store import Store
from knowledge_capture.processing import Processor
from knowledge_capture.wiki import Wiki
from knowledge_capture.knowledge_records import KnowledgeRecords
from knowledge_capture.vault_export import sync_vault, VaultExportError, _front, _broken_links
from test_wiki import Analyzer, Synthesizer, capture


def fixture(tmp_path):
    store=Store(tmp_path/'data')
    from workbench_ui_fixture import png_fixture
    image=png_fixture()
    def captured(url,directory):
        result=capture('系统升级前需要备份设备配置。\n\n![本地图示](assets/chart.png)')(url,directory)
        (directory/'chart.png').write_bytes(image)
        result['assets']=[{'status':'complete','relative_path':'assets/chart.png','sha256':hashlib.sha256(image).hexdigest()}]
        return result
    item=store.ingest('https://example.org/first',capture_fn=captured)
    analysis=Processor(store).analyze(item['source_id'],Analyzer(),infer_common=False)
    topic=Processor(store).interests()[0]['id']
    wiki=Wiki(store).build(topic,Synthesizer(lambda result: result.update(differences=[])),protocol='quote-v1')
    records=KnowledgeRecords(store)
    record=records.create('维护说明','建议先保留配置备份。',references=[{'source_id':item['source_id'],'version_id':item['version_id']}],tags=['维护'])
    revised=records.revise(record['record_id'],record['version_id'],markdown='建议先保留配置备份，再验证恢复。')
    return store,item,analysis,wiki,record,revised


def files(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name.endswith(('.md','.png')) and '_conflicts' not in p.parts}


def test_complete_linked_vault_assets_frontmatter_and_revisions(tmp_path):
    store,item,analysis,wiki,record,revised=fixture(tmp_path)
    result=sync_vault(store)
    assert result['status']=='complete' and not result['broken_links']
    root=Path(result['path'])
    assert (root/'主页.md').is_file() and (root/'资料目录.md').is_file()
    assert not _broken_links(files(root),root)
    images=list(root.rglob('chart.png'))
    assert len(images)==3
    old=root/f'records/{record["record_id"]}/versions/{record["version_id"]}/content.md'
    new=root/f'records/{record["record_id"]}/versions/{revised["version_id"]}/content.md'
    assert revised['version_id'] in old.read_text() and record['version_id'] in new.read_text()
    assert _front(new.read_text())['修订'] is True
    ai=root/f'analyses/{analysis["id"]}/analysis.md'
    assert _front(ai.read_text())['ai_derived'] is True
    source=root/f'sources/{item["source_id"]}/versions/{item["version_id"]}/content.md'
    assert _front(source.read_text())['ai_derived'] is False
    original=store.read(item['source_id'])['markdown']
    second=sync_vault(store)
    assert second['written']==0 and not second['conflicts']
    assert store.read(item['source_id'])['markdown']==original


def test_user_edits_survive_and_complete_conflict_copy_has_valid_links(tmp_path):
    store,*_=fixture(tmp_path)
    first=sync_vault(store)
    root=Path(first['path'])
    home=root/'主页.md'
    home.write_text(home.read_text()+'\n用户手工笔记\n')
    result=sync_vault(store)
    assert result['status']=='needs_review' and '主页.md' in result['conflicts']
    assert '用户手工笔记' in home.read_text()
    candidate=Path(result['candidate_path'])
    assert (candidate/'主页.md').is_file()
    assert not _broken_links(files(candidate),candidate)
    again=sync_vault(store)
    assert again['candidate_path']==result['candidate_path']


def test_expired_source_overlay_propagates_to_source_ai_and_wiki(tmp_path):
    store,item,analysis,wiki,*_=fixture(tmp_path)
    records=KnowledgeRecords(store)
    records.annotate(item['source_id'],item['version_id'],status='expired')
    result=sync_vault(store)
    root=Path(result['path'])
    assert result['status']=='complete' and not result['broken_links']
    for path in [root/f'sources/{item["source_id"]}/versions/{item["version_id"]}/content.md',
                 root/f'analyses/{analysis["id"]}/analysis.md',
                 root/f'wiki/{wiki["topic_id"]}/{wiki["version_id"]}/page.md']:
        assert _front(path.read_text())['expired'] is True


def test_disappeared_record_is_marked_withdrawn_never_deleted(tmp_path):
    store,*_,record,revised=fixture(tmp_path)
    sync_vault(store)
    with closing(store._connect()) as db,db:
        db.execute('DELETE FROM knowledge_record_versions WHERE record_id=?',(record['record_id'],))
        db.execute('DELETE FROM knowledge_records WHERE id=?',(record['record_id'],))
    result=sync_vault(store)
    path=Path(result['path'])/f'records/{record["record_id"]}/versions/{revised["version_id"]}/content.md'
    assert path.is_file() and _front(path.read_text())['withdrawn'] is True
    assert result['withdrawn']


def test_user_files_and_symlinks_not_overwritten(tmp_path):
    store=Store(tmp_path/'data')
    output=tmp_path/'vault'
    output.mkdir()
    (output/'主页.md').write_text('自己的主页')
    result=sync_vault(store,output)
    assert (output/'主页.md').read_text()=='自己的主页'
    assert result['conflicts']==['主页.md']
    forbidden=tmp_path/'data'/'sources'
    with pytest.raises(VaultExportError):sync_vault(store,forbidden)
    linked=tmp_path/'link'
    linked.symlink_to(output,target_is_directory=True)
    with pytest.raises(VaultExportError):sync_vault(store,linked)


def test_cli_vault_sync(tmp_path,monkeypatch,capsys):
    from knowledge_capture.cli import main
    monkeypatch.setattr('sys.argv',['kc','--data',str(tmp_path/'data'),'vault-sync','--output',str(tmp_path/'vault')])
    assert main()==0
    result=json.loads(capsys.readouterr().out)
    assert Path(result['home']).is_file()


def test_new_source_version_marks_prior_analysis_and_wiki_stale(tmp_path):
    store,item,analysis,wiki,*_=fixture(tmp_path)
    store.ingest('https://example.org/first',capture_fn=capture('新版本需要备份设备配置并验证兼容性。'))
    result=sync_vault(store)
    root=Path(result['path'])
    for path in [root/f'analyses/{analysis["id"]}/analysis.md',root/f'wiki/{wiki["topic_id"]}/{wiki["version_id"]}/page.md']:
        assert _front(path.read_text())['stale'] is True
    assert not result['broken_links']


def test_source_expiration_propagates_to_referencing_maintained_record(tmp_path):
    store,item,*rest=fixture(tmp_path)
    record,revised=rest[-2:]
    KnowledgeRecords(store).annotate(item['source_id'],item['version_id'],status='expired')
    result=sync_vault(store)
    path=Path(result['path'])/f'records/{record["record_id"]}/versions/{revised["version_id"]}/content.md'
    assert _front(path.read_text())['expired'] is True


def test_cli_mirror_does_not_require_valid_engine_credentials(tmp_path,monkeypatch,capsys):
    from knowledge_capture.cli import main
    def fail(*args,**kwargs): pytest.fail('vault sync must not read engine settings')
    monkeypatch.setattr('knowledge_capture.settings.Settings.provider_configuration',fail)
    monkeypatch.setattr('sys.argv',['kc','--data',str(tmp_path/'data'),'vault-sync'])
    assert main()==0
    assert json.loads(capsys.readouterr().out)['status']=='complete'


def test_existing_manual_wiki_edit_is_not_hidden_in_mirror(tmp_path):
    store,_,_,wiki,*_=fixture(tmp_path)
    published=Path(wiki['path'])
    published.write_text(published.read_text()+'\n用户在主库Wiki的手工补充。\n')
    result=sync_vault(store)
    assert not result['broken_links']
    mirrored=Path(result['path'])/f'wiki/{wiki["topic_id"]}/published.md'
    assert '用户在主库Wiki的手工补充。' in mirrored.read_text()
    assert _front(mirrored.read_text())['publication_status']=='modified_needs_review'


def test_all_automatic_hooks_run_after_commit_and_failure_never_rolls_back(tmp_path,monkeypatch):
    import knowledge_capture.vault_export as module
    store=Store(tmp_path/'data')
    calls=[]
    def failed_sync(active_store, output=None):
        # A write transaction here would fail if a source/record/model publisher
        # invoked the hook while still holding its authoritative transaction.
        with closing(active_store._connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            calls.append(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0])
        raise VaultExportError('测试镜像失败，主库应保留')
    monkeypatch.setattr(module,'sync_vault',failed_sync)
    source=store.ingest('https://example.org/hooks',capture_fn=capture('升级前需要保留配置备份。'))
    assert source['status']=='complete' and source['vault_sync']['status']=='failed'
    analysis=Processor(store).analyze(source['source_id'],Analyzer(),infer_common=False)
    assert analysis['status']=='complete' and analysis['vault_sync']['status']=='failed'
    topic=Processor(store).interests()[0]['id']
    wiki=Wiki(store).build(topic,Synthesizer(lambda r:r.update(differences=[])),protocol='quote-v1')
    assert wiki['status']=='complete' and wiki['vault_sync']['status']=='failed'
    records=KnowledgeRecords(store)
    record=records.create('笔记','已经提交的知识。',idempotency_key='record')
    assert records.create('笔记','已经提交的知识。',idempotency_key='record')==record
    assert records.read(record['record_id'])['markdown']=='已经提交的知识。'
    assert len(calls)==5
    assert json.loads((store.root/'.vault-sync-status.json').read_text())['status']=='failed'
