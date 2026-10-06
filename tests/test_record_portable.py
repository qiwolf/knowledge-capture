"""Maintenance records survive a real move, independently of the old directory."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import zipfile
import pytest

from knowledge_capture.store import Store
from knowledge_capture.knowledge_records import KnowledgeRecords
from knowledge_capture.portable import export_library, restore_library, PortableError, MANIFEST
from knowledge_capture.workbench_exports import prepare_export
from knowledge_capture.vault_export import sync_vault
from test_portable import library


def maintained(store,source):
    records=KnowledgeRecords(store)
    overlay=records.revise(source['source_id'],source['version_id'],markdown='维护修订保留原始依据。\n\n![图片](assets/image.png)',idempotency_key='overlay')
    expired=records.annotate(source['source_id'],overlay['version_id'],status='expired',tags=['过期','待核实'],idempotency_key='expire')
    note=records.create('独立知识笔记','根据来源形成的维护笔记。',references=[{'source_id':source['source_id'],'version_id':source['version_id']}],tags=['新增'],idempotency_key='note')
    revised=records.revise(note['record_id'],note['version_id'],markdown='补充了修订记录的维护笔记。',idempotency_key='note-revision')
    return overlay,expired,note,revised


def test_full_library_records_delete_original_restore_and_regenerate_vault(tmp_path):
    store,source,*_=library(tmp_path/'original')
    overlay,expired,note,revised=maintained(store,source)
    # Regenerable material and credentials must never become backup authority.
    (store.root/'.cache/vector').mkdir(parents=True)
    (store.root/'.cache/vector/index.bin').write_bytes(b'cache')
    with prepare_export(store,'library') as download:
        archive=tmp_path/'full.zip'
        shutil.copyfile(download.path,archive)
        with zipfile.ZipFile(archive) as z:
            assert any(name.startswith('records/') for name in z.namelist())
            assert not any(name.startswith(('obsidian/','.cache/')) for name in z.namelist())
    shutil.rmtree(store.root)
    restored=Store(restore_library(archive,tmp_path/'restored'))
    records=KnowledgeRecords(restored)
    assert records.read(source['source_id'])['metadata']['status']=='expired'
    assert records.read(source['source_id'],overlay['version_id'])['markdown']==overlay['markdown']
    assert records.read(note['record_id'])['markdown']==revised['markdown']
    assert records.create('独立知识笔记','根据来源形成的维护笔记。',references=[{'source_id':source['source_id'],'version_id':source['version_id']}],tags=['新增'],idempotency_key='note')==note
    assert [event['event'] for event in records.history(note['record_id'])['events']]==['created','revised']
    assert source['source_id'] not in [r['record_id'] for r in records.list()['results']]
    assert restored.read(source['source_id'])['metadata']['version_id']==source['version_id']
    mirror=sync_vault(restored)
    assert mirror['status']=='complete' and not mirror['broken_links']
    assert Path(mirror['home']).is_file()


def test_record_document_download_includes_history_original_images(tmp_path):
    store,source,*_=library(tmp_path/'original')
    overlay,expired,*_=maintained(store,source)
    with prepare_export(store,'record',source['source_id']) as download:
        with zipfile.ZipFile(download.path) as z:
            manifest=json.loads(z.read('record-export.json'))
            assert manifest['version_id']==expired['version_id']
            assert manifest['versions']==[expired['version_id'],overlay['version_id']]
            assert '知识记录.md' in z.namelist()
            assert f'records/{source["source_id"]}/versions/{expired["version_id"]}/assets/image.png' in z.namelist()
            assert f'sources/{source["source_id"]}/versions/{source["version_id"]}/assets/image.png' in z.namelist()
            for name,digest in manifest['files'].items():
                assert hashlib.sha256(z.read(name)).hexdigest()==digest
    with prepare_export(store,'record',source['source_id'],version=overlay['version_id']) as download:
        with zipfile.ZipFile(download.path) as z:
            assert json.loads(z.read('record-export.json'))['versions']==[overlay['version_id']]


def test_record_references_keep_engine_raw_after_old_library_removed(tmp_path,monkeypatch):
    from test_engine_integration import configuration,SelectionClient
    from knowledge_capture.providers import CaptureRouter
    store=Store(tmp_path/'original')
    response={'heading':'原始标题','content':'保存原始响应、字符定位和版本出处，恢复后需要核对所有原始证据。'*8}
    config,_=configuration(response)
    monkeypatch.setattr('knowledge_capture.providers._target',lambda _:None)
    source=store.ingest('https://example.org/raw',capture_fn=CaptureRouter(config,store=store,client=SelectionClient()).capture)
    record=KnowledgeRecords(store).revise(source['source_id'],source['version_id'],markdown='维护后的知识正文。')
    archive=export_library(store,tmp_path/'raw-library.zip')
    shutil.rmtree(store.root)
    restored=Store(restore_library(archive,tmp_path/'restored'))
    with prepare_export(restored,'record',source['source_id']) as download:
        with zipfile.ZipFile(download.path) as z:
            prefix=f'sources/{source["source_id"]}/versions/{source["version_id"]}/raw/'
            assert json.loads(z.read(prefix+'response.txt'))==response
            assert prefix+'inventory.json' in z.namelist() and prefix+'selection.json' in z.namelist()
    assert KnowledgeRecords(restored).read(source['source_id'])['version_id']==record['version_id']


@pytest.mark.parametrize('damage',['content','identity','head','cycle','idempotency'])
def test_semantic_record_corruption_prevents_backup(tmp_path,damage):
    store=Store(tmp_path/'original')
    record=KnowledgeRecords(store).create('标题','真实内容',idempotency_key='once')
    directory=store.root/'records'/record['record_id']/'versions'/record['version_id']
    with closing(store._connect()) as db,db:
        if damage=='content': (directory/'content.md').write_text('被篡改的内容')
        elif damage=='head': db.execute("UPDATE knowledge_records SET status='expired'")
        elif damage=='idempotency':
            value=json.loads(db.execute('SELECT response_json FROM knowledge_record_idempotency').fetchone()[0])
            value['markdown']='伪造返回'
            db.execute('UPDATE knowledge_record_idempotency SET response_json=?',(json.dumps(value),))
        else:
            path=directory/'metadata.json'
            metadata=json.loads(path.read_text())
            if damage=='identity': metadata['record_id']='a'*32
            else:
                metadata['parent_version']=record['version_id']
                db.execute('UPDATE knowledge_record_versions SET parent_version=?',(record['version_id'],))
            data=json.dumps(metadata,ensure_ascii=False).encode()
            path.write_bytes(data)
            db.execute('UPDATE knowledge_record_versions SET metadata_sha256=?',(hashlib.sha256(data).hexdigest(),))
    with pytest.raises(PortableError,match='知识维护'):
        export_library(store,tmp_path/'bad.zip')
    assert not (tmp_path/'bad.zip').exists()


def test_restore_checks_record_hash_even_if_zip_manifest_was_rewritten(tmp_path):
    store=Store(tmp_path/'original')
    KnowledgeRecords(store).create('标题','权威记录')
    archive=export_library(store,tmp_path/'good.zip')
    with zipfile.ZipFile(archive) as z: content={name:z.read(name) for name in z.namelist()}
    name=next(name for name in content if name.startswith('records/') and name.endswith('content.md'))
    content[name]='伪造正文'.encode()
    manifest=json.loads(content[MANIFEST])
    manifest['files'][name]={'size':len(content[name]),'sha256':hashlib.sha256(content[name]).hexdigest()}
    content[MANIFEST]=json.dumps(manifest).encode()
    bad=tmp_path/'bad.zip'
    with zipfile.ZipFile(bad,'w') as z:
        for name,data in content.items():z.writestr(name,data)
    with pytest.raises(PortableError,match='知识维护'):restore_library(bad,tmp_path/'restore-bad')
    assert not (tmp_path/'restore-bad').exists()


def test_vector_cache_inside_main_db_is_regenerated_not_backup_authority(tmp_path):
    store=Store(tmp_path/'original')
    record=KnowledgeRecords(store).create('权威知识','向量只是可再生缓存。')
    with closing(store._connect()) as db,db:
        db.execute('CREATE TABLE retrieval_runs (id TEXT, status TEXT)')
        db.execute('CREATE TABLE retrieval_vectors (run_id TEXT, vector_json TEXT)')
        db.execute("INSERT INTO retrieval_runs VALUES ('run','complete')")
        db.execute("INSERT INTO retrieval_vectors VALUES ('run','[0.2,0.8]')")
    archive=export_library(store,tmp_path/'cache-free.zip')
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM retrieval_vectors').fetchone()[0]==1
    restored=Store(restore_library(archive,tmp_path/'restored'))
    with closing(restored._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM retrieval_vectors').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM retrieval_runs').fetchone()[0]==0
    assert KnowledgeRecords(restored).read(record['record_id'])['markdown']=='向量只是可再生缓存。'
